# -*- coding: utf-8 -*-
"""Зонд: можно ли свести рассылки Mindbox между Метрикой и Triple Whale.

После моста рекламных кампаний 5.6% визитов с меткой остались неопознанными — это
рассылки (26.07.10_sale70_email, 26.06.23_BlossomSeason). Вопрос Павла: у нас есть
данные с обеих сторон, нельзя ли склеить и их.

Смотрим, ЧЕМ каждая сторона идентифицирует рассылку:
  Метрика — utm_campaign визитов с traffic_source=email
  TW      — source и campaignId тачпоинта у заказов с mindbox-источником
Если ключи одного вида — мост строится тем же приёмом, что и для рекламы.

Только чтение. Запуск: python -m scripts.probe_gcc_crm_bridge
"""
import io
import os
import sys
from collections import Counter
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sync.gcc_triplewhale import fetch_tw_orders, order_touchpoint  # noqa: E402

COUNTER = "98232701"


def metrika_email_campaigns(token: str, d1: str, d2: str) -> Counter:
    """utm-метки визитов, которые Метрика относит к почте."""
    resp = requests.get(
        "https://api-metrika.yandex.net/stat/v1/data",
        headers={"Authorization": f"OAuth {token}"},
        params={
            "ids": COUNTER, "date1": d1, "date2": d2,
            "metrics": "ym:s:visits",
            "dimensions": "ym:s:UTMCampaign,ym:s:UTMSource",
            "filters": "ym:s:lastsignTrafficSource=='email'",
            "accuracy": "full", "limit": 500,
        },
        timeout=60,
    )
    resp.raise_for_status()
    out: Counter = Counter()
    for row in resp.json().get("data", []):
        campaign = row["dimensions"][0].get("name") or "(пусто)"
        source = row["dimensions"][1].get("name") or "(пусто)"
        out[(campaign, source)] += row["metrics"][0]
    return out


def main():
    token = os.environ["GCC_METRICA_TOKEN"]
    tw_key = os.environ["GCC_TRIPLEWHALE_API_KEY"]
    shop = os.environ["GCC_TW_SHOP_DOMAIN"]
    to = date.today() - timedelta(days=1)
    frm = to - timedelta(days=29)
    d1, d2 = frm.isoformat(), to.isoformat()
    print(f"[зонд CRM] {d1}…{d2}\n")

    print("=== Метрика: метки почтовых визитов ===")
    for (campaign, source), visits in metrika_email_campaigns(token, d1, d2).most_common(15):
        print(f"    utm_campaign={campaign[:40]:<42} utm_source={source[:16]:<18} {visits:>7,.0f}")

    print("\n=== Triple Whale: чем помечены заказы из рассылок ===")
    orders = fetch_tw_orders(tw_key, shop, d1, d2)
    pairs: Counter = Counter()
    for order in orders:
        tp = order_touchpoint(order)
        src = (tp.get("source") or "").lower()
        if "mindbox" in src or "klaviyo" in src or src == "email":
            pairs[(tp.get("source"), tp.get("campaignId") or "(пусто)")] += 1
    if not pairs:
        print("    заказов из рассылок за период нет")
        return
    for (src, cid), n in pairs.most_common(20):
        print(f"    source={str(src)[:28]:<30} campaignId={str(cid)[:34]:<36} заказов {n}")


if __name__ == "__main__":
    main()
