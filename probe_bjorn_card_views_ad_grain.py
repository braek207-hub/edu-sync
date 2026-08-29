"""Можно ли класть просмотры карточек на грань «источник × объявление × товар»?

Старый замер (probe_bjorn_card_views_join) дал 0,0% на ЛЮБОЙ грани с источником, включая
«источник + товар». Причина там была одна на всех — источник в логах приходит машинным кодом
(ad/organic), а в витрине фразой Метрики. Её починили таблицей SOURCE_LABELS в синке, но
вывод «объявление не ложится» остался с тех цифр и потому недоказан: провал источника
маскировал вопрос об объявлении.

Здесь источник переводится словарём синка, и грань объявления замеряется отдельно:
  1. сколько просмотров карточек вообще пришло с рекламных визитов;
  2. у скольких из них визит несёт id объявления (lastSignDirectClickBanner);
  3. сколько ложится на ключ витрины (источник × объявление × товар).

День и модель — те же, что в старом замере, чтобы числа были сравнимы.
"""

from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from typing import Any

import requests

try:  # локально ключи лежат в .env, в Actions приходят из секретов
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from sync.bjorn.common import SupabaseRest  # noqa: E402

BASE = "https://api-metrika.yandex.net/management/v1/counter/{counter}"
DAY = os.environ.get("PROBE_LOG_DAY") or os.environ.get("PROBE_DATE1") or "2026-08-20"
TOKEN = os.environ["METRICA_TOKEN"]
COUNTER = os.environ["METRICA_COUNTER_ID"]
HEADERS = {"Authorization": f"OAuth {TOKEN}"}

HIT_FIELDS = "ym:pv:visitID,ym:pv:dateTime,ym:pv:URL,ym:pv:title"
VISIT_FIELDS = ",".join(
    [
        "ym:s:visitID",
        "ym:s:lastSignTrafficSource",
        "ym:s:lastSignDirectClickOrder",
        "ym:s:lastSignDirectBannerGroup",
        "ym:s:lastSignDirectClickBanner",
        "ym:s:lastSignUTMCampaign",
        "ym:s:lastSignUTMContent",
    ]
)

# Копия таблицы из sync/bjorn/sync_card_views.py — тот же перевод, что делает синк.
SOURCE_LABELS = {
    "organic": "Переходы из поисковых систем",
    "ad": "Переходы по рекламе",
    "direct": "Прямые заходы",
    "internal": "Внутренние переходы",
    "referral": "Переходы по ссылкам на сайтах",
    "social": "Переходы из социальных сетей",
    "email": "Переходы с почтовых рассылок",
    "recommend": "Переходы из рекомендательных систем",
    "messenger": "Переходы из мессенджеров",
    "saved": "Переходы с сохранённых страниц",
    "undefined": "Не определено",
}

BUY_TAIL = re.compile(r"\s+[-–—]\s+купить", re.IGNORECASE)


def norm_product(name: str) -> str:
    return re.sub(r"\s+", " ", BUY_TAIL.split(name)[0].replace("ё", "е").lower()).strip()


def logs_create(source: str, fields: str) -> int:
    resp = requests.post(
        BASE.format(counter=COUNTER) + "/logrequests",
        params={"date1": DAY, "date2": DAY, "fields": fields, "source": source},
        headers=HEADERS,
        timeout=120,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"create {source}: {resp.status_code} {resp.text[:300]}")
    return resp.json()["log_request"]["request_id"]


def logs_wait(request_id: int) -> dict:
    for _ in range(40):
        resp = requests.get(
            BASE.format(counter=COUNTER) + f"/logrequest/{request_id}", headers=HEADERS, timeout=120
        )
        info = resp.json()["log_request"]
        if info["status"] == "processed":
            return info
        if info["status"] in {"processing_failed", "canceled"}:
            raise RuntimeError(f"request {request_id}: {info['status']}")
        time.sleep(15)
    raise RuntimeError(f"request {request_id} не подготовился")


def logs_text(request_id: int, info: dict) -> str:
    out = []
    for part in info.get("parts", []):
        resp = requests.get(
            BASE.format(counter=COUNTER)
            + f"/logrequest/{request_id}/part/{part['part_number']}/download",
            headers=HEADERS,
            timeout=600,
        )
        out.append(resp.text)
    return "".join(out)


def logs_clean(request_id: int) -> None:
    requests.post(
        BASE.format(counter=COUNTER) + f"/logrequest/{request_id}/clean",
        headers=HEADERS,
        timeout=120,
    )


def clean_id(value: str) -> str:
    """id объявления/кампании из логов: пусто, ноль, шаблон UTM и «M-» — не id."""
    v = (value or "").strip()
    if v in {"", "0", "None", "--", "null", "(not set)"}:
        return ""
    if v.startswith("{") and v.endswith("}"):
        return ""
    return v[2:] if v.startswith("M-") else v


def main() -> None:
    print("=" * 78)
    print(f"Грань объявления для просмотров карточек · день {DAY} · модель lastsign")
    print("=" * 78)

    hits_id = logs_create("hits", HIT_FIELDS)
    visits_id = logs_create("visits", VISIT_FIELDS)
    try:
        hits = logs_text(hits_id, logs_wait(hits_id))
        visits = logs_text(visits_id, logs_wait(visits_id))
    finally:
        logs_clean(hits_id)
        logs_clean(visits_id)

    # visitID → (источник-фраза, кампания, объявление, откуда взялось объявление)
    visit_src: dict[str, tuple[str, str, str, str]] = {}
    for i, line in enumerate(visits.splitlines()):
        if i == 0:
            continue
        p = line.split("\t")
        if len(p) < 7:
            continue
        raw_source = p[1].strip()
        source = SOURCE_LABELS.get(raw_source.lower(), raw_source)
        campaign = clean_id(p[2]) or clean_id(p[5])
        banner = clean_id(p[4])
        content = clean_id(p[6])
        if banner:
            ad, origin = banner, "DirectClickBanner"
        elif content:
            ad, origin = content, "UTMContent"
        else:
            ad, origin = "", "нет"
        visit_src[p[0]] = (source, campaign, ad, origin)

    full: dict[tuple[str, ...], int] = defaultdict(int)
    by_campaign: dict[tuple[str, ...], int] = defaultdict(int)
    src_only: dict[tuple[str, ...], int] = defaultdict(int)
    origin_counts: dict[str, int] = defaultdict(int)
    total_views = 0
    paid_views = 0
    paid_with_ad = 0
    no_visit = 0

    for i, line in enumerate(hits.splitlines()):
        if i == 0:
            continue
        p = line.split("\t")
        if len(p) < 4 or "/product/" not in p[2]:
            continue
        total_views += 1
        product = norm_product(p[3])
        if not product:
            continue
        if p[0] not in visit_src:
            no_visit += 1
        source, campaign, ad, origin = visit_src.get(p[0], ("", "", "", "нет визита"))
        is_paid = source == SOURCE_LABELS["ad"]
        if is_paid:
            paid_views += 1
            origin_counts[origin] += 1
            if ad:
                paid_with_ad += 1
        full[(source, ad, product)] += 1
        by_campaign[(source, campaign, product)] += 1
        src_only[(source, product)] += 1

    print(f"\nПросмотров карточек за день: {total_views}; без своего визита: {no_visit}")
    print(f"Из них с рекламных визитов: {paid_views}")
    if paid_views:
        share = 100 * paid_with_ad / paid_views
        print(f"  у визита есть id объявления: {paid_with_ad} ({share:.1f}%)")
        for k, v in sorted(origin_counts.items(), key=lambda kv: -kv[1]):
            print(f"    источник id «{k}»: {v}")

    supabase = SupabaseRest()
    all_rows: list[dict[str, Any]] = supabase.select_date_range(
        "bjorn_product_daily", DAY, DAY, select="attribution,source,campaign_id,ad_id,product_name"
    )
    rows = [r for r in all_rows if r["attribution"] == "lastsign"]
    print(f"\nСтрок товарной витрины за день на модели lastsign: {len(rows)}")

    mart_full = {(r["source"], r["ad_id"], norm_product(r["product_name"])) for r in rows}
    mart_camp = {(r["source"], r["campaign_id"], norm_product(r["product_name"])) for r in rows}
    mart_src = {(r["source"], norm_product(r["product_name"])) for r in rows}

    def report(label: str, keys: dict[tuple[str, ...], int], mart: set) -> None:
        hit = sum(n for k, n in keys.items() if k in mart)
        total = sum(keys.values())
        pct = 100 * hit / total if total else 0
        print(f"  {label}: {hit} из {total} ({pct:.1f}%)")

    print("\nДоля просмотров, находящих свой ключ в товарной витрине:")
    report("источник + объявление + товар", full, mart_full)
    report("источник + кампания + товар  ", by_campaign, mart_camp)
    report("источник + товар             ", src_only, mart_src)

    print("\nТо же, но только по рекламным просмотрам (грань объявления имеет смысл только там):")
    paid_full = {k: n for k, n in full.items() if k[0] == SOURCE_LABELS["ad"]}
    report("источник + объявление + товар", paid_full, mart_full)


if __name__ == "__main__":
    main()
