# Проба BJORN, раунд 15. Решает архитектуру ступени «просмотр карточки».
#
# Раунд 14 показал: Logs API принимает наборы полей first* и lastSign*, то есть две из трёх
# моделей витрины можно заполнить честно (automatic в логах нет вовсе — останется NULL).
#
# Но остаётся вопрос, от которого зависит, куда вообще писать просмотры. План Task 6
# предлагает класть их в те же строки bjorn_product_daily по полному ключу
# (день, модель, источник, кампания, объявление, товар). Ключ собирается из ДВУХ разных
# источников: измерения stat API против полей логов. Если они расходятся, просмотры лягут
# мимо строк, ступень воронки окажется занижена, и увидим мы это не сразу.
#
# Меряем долю просмотров карточек, попадающих в существующие ключи витрины, на трёх
# гранулярностях. По результату решаем: полный ключ, укороченный или отдельная таблица.
from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from typing import Any

import requests

from sync.bjorn.common import SupabaseRest, normalize_campaign_id, normalize_source

BASE = "https://api-metrika.yandex.net/management/v1/counter/{counter}"
DAY = os.environ.get("PROBE_LOG_DAY", "2026-08-20")

TOKEN = os.environ["METRICA_TOKEN"]
COUNTER = os.environ["METRICA_COUNTER_ID"]
HEADERS = {"Authorization": f"OAuth {TOKEN}"}

HIT_FIELDS = "ym:pv:visitID,ym:pv:dateTime,ym:pv:URL,ym:pv:title"
VISIT_FIELDS = ",".join(
    [
        "ym:s:visitID",
        "ym:s:lastSignTrafficSource",
        "ym:s:lastSignUTMCampaign",
        "ym:s:lastSignUTMContent",
        "ym:s:lastSignDirectClickBanner",
    ]
)

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
        BASE.format(counter=COUNTER) + f"/logrequest/{request_id}/clean", headers=HEADERS, timeout=120
    )


def strip_banner(value: str) -> str:
    value = value.strip()
    if value in {"", "0", "None"}:
        return ""
    return value[2:] if value.startswith("M-") else value


def main() -> None:
    print("=" * 78)
    print(f"Сшивка просмотров карточек с витриной · день {DAY} · модель lastsign")
    print("=" * 78)

    hits_id = logs_create("hits", HIT_FIELDS)
    visits_id = logs_create("visits", VISIT_FIELDS)
    try:
        hits = logs_text(hits_id, logs_wait(hits_id))
        visits = logs_text(visits_id, logs_wait(visits_id))
    finally:
        logs_clean(hits_id)
        logs_clean(visits_id)

    visit_src: dict[str, tuple[str, str, str]] = {}
    for i, line in enumerate(visits.splitlines()):
        if i == 0:
            continue
        p = line.split("\t")
        if len(p) < 5:
            continue
        ad_id = strip_banner(p[4]) or strip_banner(p[3])
        visit_src[p[0]] = (normalize_source(p[1].strip()), normalize_campaign_id(p[2].strip()), ad_id)

    # Ключи логов на трёх гранулярностях: чем короче ключ, тем выше шанс совпасть с витриной,
    # но тем грубее детализация вкладки.
    full: dict[tuple[str, ...], int] = defaultdict(int)
    no_ad: dict[tuple[str, ...], int] = defaultdict(int)
    src_only: dict[tuple[str, ...], int] = defaultdict(int)
    total_views = 0
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
        source, campaign, ad_id = visit_src.get(p[0], ("", "", ""))
        full[(source, campaign, ad_id, product)] += 1
        no_ad[(source, campaign, product)] += 1
        src_only[(source, product)] += 1

    print(f"\nПросмотров карточек за день: {total_views}, без своего визита: {no_visit}")

    supabase = SupabaseRest()
    all_rows: list[dict[str, Any]] = supabase.select_date_range(
        "bjorn_product_daily", DAY, DAY, select="attribution,source,campaign_id,ad_id,product_name"
    )
    # Сравниваем с той же моделью, из которой брали поля логов, иначе сравнение бессмысленно.
    rows = [r for r in all_rows if r["attribution"] == "lastsign"]
    print(f"Строк витрины за день на модели lastsign: {len(rows)} (всего {len(all_rows)})")

    mart_full = {
        (r["source"], r["campaign_id"], r["ad_id"], norm_product(r["product_name"])) for r in rows
    }
    mart_no_ad = {(r["source"], r["campaign_id"], norm_product(r["product_name"])) for r in rows}
    mart_src = {(r["source"], norm_product(r["product_name"])) for r in rows}
    mart_products = {norm_product(r["product_name"]) for r in rows}

    def report(label: str, log_keys: dict[tuple[str, ...], int], mart: set) -> None:
        hit = sum(n for k, n in log_keys.items() if k in mart)
        total = sum(log_keys.values())
        share = 100 * hit / total if total else 0
        print(f"  {label}: {hit} из {total} просмотров легли на витрину ({share:.1f}%)")

    print("\nДоля просмотров, находящих свой ключ в витрине:")
    report("полный ключ (источник+кампания+объявление+товар)", full, mart_full)
    report("без объявления (источник+кампания+товар)", no_ad, mart_no_ad)
    report("только источник+товар", src_only, mart_src)

    only_product = sum(n for (_, prod), n in src_only.items() if prod in mart_products)
    total = sum(src_only.values())
    print(f"  только товар: {only_product} из {total} ({100 * only_product / total:.1f}%)")

    print("\nТоп-10 ключей логов, не нашедших витрину (полный ключ):")
    misses = sorted(((n, k) for k, n in full.items() if k not in mart_full), reverse=True)[:10]
    for n, k in misses:
        print(f"  {n:>5}  источник={k[0]!r} кампания={k[1]!r} объявление={k[2]!r} товар={k[3][:40]!r}")


if __name__ == "__main__":
    main()
