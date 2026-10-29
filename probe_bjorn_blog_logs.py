# Проба BJORN: путь посетителей блога (/blog/) до каталога, карточки, корзины и покупки.
#
# Stat API не отвечает на вопрос «куда человек пошёл после статьи и купил ли потом» —
# нужна последовательность хитов по одному clientID. Поэтому качаем Logs API:
#   - визиты целиком (база для сравнения: как покупают те, кто блог не видел);
#   - хиты целиком во временный файл, затем оставляем только хиты людей, которые хоть раз
#     открывали /blog/ — иначе артефакт раздувается в разы.
# Репозиторий публичный: результат шифруется в workflow открытым ключом, сырые clientID
# и заказы наружу не выходят. Здесь только выгрузка; разбор — локально.
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import sys
import time
from datetime import date, timedelta

import requests

API = "https://api-metrika.yandex.net/management/v1/counter/{c}"
TOKEN = os.environ["METRICA_TOKEN"].strip()
COUNTER = os.environ["METRICA_COUNTER_ID"].strip()
H = {"Authorization": f"OAuth {TOKEN}"}
D1 = date.fromisoformat(os.environ.get("PROBE_DATE1") or "2025-10-01")
D2 = date.fromisoformat(os.environ.get("PROBE_DATE2") or "2026-10-08")
OUT = "out"
BLOG_MARK = "/blog/"

VISIT_FIELDS = [
    "ym:s:visitID", "ym:s:clientID", "ym:s:dateTime", "ym:s:startURL", "ym:s:endURL",
    "ym:s:pageViews", "ym:s:visitDuration", "ym:s:bounce", "ym:s:isNewUser",
    "ym:s:lastsignTrafficSource", "ym:s:lastsignSourceEngine", "ym:s:lastsignUTMSource",
    "ym:s:lastsignUTMCampaign", "ym:s:lastTrafficSource", "ym:s:lastSourceEngine",
    "ym:s:referer", "ym:s:deviceCategory", "ym:s:regionCity",
    "ym:s:goalsID", "ym:s:goalsDateTime", "ym:s:purchaseID", "ym:s:purchaseRevenue",
    "ym:s:purchaseDateTime", "ym:s:productsName", "ym:s:productsPurchaseID",
    "ym:s:productsPrice", "ym:s:impressionsURL", "ym:s:impressionsProductName",
    "ym:s:impressionsDateTime", "ym:s:productsDateTime", "ym:s:productsEvent",
]
HIT_FIELDS = [
    "ym:pv:watchID", "ym:pv:clientID", "ym:pv:dateTime", "ym:pv:URL", "ym:pv:title",
    "ym:pv:referer", "ym:pv:isPageView", "ym:pv:goalsID", "ym:pv:link", "ym:pv:download",
    "ym:pv:notBounce", "ym:pv:ecommerce",
]


def call(method: str, url: str, **kw) -> requests.Response:
    for attempt in range(8):
        try:
            r = requests.request(method, url, headers=H, timeout=600, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.RequestException(f"HTTP {r.status_code}")
            return r
        except requests.RequestException as e:
            wait = 10 * (attempt + 1)
            print(f"  повтор {attempt + 1} через {wait}с: {e}", flush=True)
            time.sleep(wait)
    raise SystemExit(f"{method} {url} — не удалось")


def evaluate(fields: list[str], source: str, d1: date, d2: date) -> tuple[bool, int, str]:
    r = call("GET", API.format(c=COUNTER) + "/logrequests/evaluate",
             params={"date1": d1, "date2": d2, "fields": ",".join(fields), "source": source})
    if r.status_code != 200:
        return False, 0, r.text[:200]
    ev = r.json()["log_request_evaluation"]
    return bool(ev.get("possible")), int(ev.get("max_possible_day_quantity") or 0), ""


def accepted(fields: list[str], source: str) -> list[str]:
    """Отсеять поля, которых у счётчика нет: одно неизвестное поле роняет весь запрос."""
    day = D2 - timedelta(days=1)
    ok_fields = []
    for f in fields:
        ok, _, err = evaluate([fields[0], f] if f != fields[0] else [f], source, day, day)
        if ok or not err:
            ok_fields.append(f)
        else:
            print(f"  поле {f} не принято: {err[:120]}", flush=True)
    return ok_fields


def stream_chunk(fields: list[str], source: str, d1: date, d2: date):
    base = API.format(c=COUNTER)
    r = call("POST", base + "/logrequests",
             params={"date1": d1, "date2": d2, "fields": ",".join(fields), "source": source})
    if r.status_code != 200:
        raise SystemExit(f"создание запроса {source} {d1}..{d2}: {r.status_code} {r.text[:300]}")
    rid = r.json()["log_request"]["request_id"]
    while True:
        st = call("GET", f"{base}/logrequest/{rid}").json()["log_request"]
        if st["status"] == "processed":
            break
        if st["status"] in ("processing_failed", "canceled", "cleaned_by_user", "cleaned_automatically_as_too_old"):
            raise SystemExit(f"запрос {rid}: {st['status']}")
        time.sleep(20)
    header_done = False
    for part in st["parts"]:
        r = call("GET", f"{base}/logrequest/{rid}/part/{part['part_number']}/download", stream=True)
        r.encoding = "utf-8"
        lines = r.iter_lines(decode_unicode=True)
        head = next(lines)
        if not header_done:
            yield head.split("\t")
            header_done = True
        for line in lines:
            if line:
                yield line.split("\t")
    call("POST", f"{base}/logrequest/{rid}/clean")


def pull(source: str, fields: list[str], cap_days: int, dest: str, keep=None) -> int:
    _, max_days, _ = evaluate(fields, source, D1, D1 + timedelta(days=cap_days - 1))
    step = max(1, min(cap_days, max_days or cap_days))
    print(f"{source}: шаг {step} дн., полей {len(fields)}", flush=True)
    n = 0
    with gzip.open(dest, "wt", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        wrote_head = False
        d = D1
        while d <= D2:
            e = min(D2, d + timedelta(days=step - 1))
            rows = stream_chunk(fields, source, d, e)
            head = next(rows)
            if not wrote_head:
                w.writerow(head)
                wrote_head = True
            c = 0
            for row in rows:
                if keep is None or keep(head, row):
                    w.writerow(row)
                    c += 1
            n += c
            print(f"  {d}..{e}: {c} строк", flush=True)
            d = e + timedelta(days=1)
    return n


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    goals = call("GET", API.format(c=COUNTER) + "/goals").json()
    with open(f"{OUT}/goals.json", "w", encoding="utf-8") as fh:
        json.dump(goals, fh, ensure_ascii=False)
    print(f"целей: {len(goals.get('goals', []))}", flush=True)

    vf = accepted(VISIT_FIELDS, "visits")
    hf = accepted(HIT_FIELDS, "hits")
    print(f"визиты: {vf}\nхиты: {hf}", flush=True)

    nv = pull("visits", vf, 31, f"{OUT}/visits.tsv.gz")
    print(f"визитов всего: {nv}", flush=True)

    # Хиты: первый проход — всё во временный файл и множество людей блога.
    blog_clients: set[str] = set()
    tmp = "hits_all.tsv.gz"
    nh = pull("hits", hf, 10, tmp)
    with gzip.open(tmp, "rt", encoding="utf-8") as fh:
        r = csv.reader(fh, delimiter="\t")
        head = next(r)
        ci, ui = head.index("ym:pv:clientID"), head.index("ym:pv:URL")
        for row in r:
            if BLOG_MARK in row[ui] and row[ci] not in ("", "0"):
                blog_clients.add(row[ci])
    print(f"хитов всего: {nh}, людей блога: {len(blog_clients)}", flush=True)

    kept = 0
    with gzip.open(tmp, "rt", encoding="utf-8") as src, \
            gzip.open(f"{OUT}/blog_hits.tsv.gz", "wt", encoding="utf-8", newline="") as dst:
        r = csv.reader(src, delimiter="\t")
        w = csv.writer(dst, delimiter="\t", lineterminator="\n")
        head = next(r)
        w.writerow(head)
        ci = head.index("ym:pv:clientID")
        for row in r:
            if row[ci] in blog_clients:
                w.writerow(row)
                kept += 1
    os.remove(tmp)
    print(f"хитов людей блога: {kept}", flush=True)

    # Помесячный объём хитов всего сайта — знаменатель для доли блога.
    with open(f"{OUT}/meta.json", "w", encoding="utf-8") as fh:
        json.dump({"date1": str(D1), "date2": str(D2), "visits": nv, "hits": nh,
                   "blog_clients": len(blog_clients), "blog_hits": kept,
                   "visit_fields": vf, "hit_fields": hf}, fh, ensure_ascii=False)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
