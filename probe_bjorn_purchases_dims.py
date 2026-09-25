# Проба: какое из измерений обнуляет выборку purchaseID×lastsign на счётчике BJORN.
# Вариант A — 6 измерений старой пробы (работала), дальше добавляем по одному.
from __future__ import annotations

import os
import time

import requests

STAT = "https://api-metrika.yandex.net/stat/v1/data"
DATE1 = os.environ.get("PROBE_DATE1", "2026-09-01")
DATE2 = os.environ.get("PROBE_DATE2", "2026-09-14")

BASE = [
    "ym:s:purchaseID",
    "ym:s:date",
    "ym:s:lastsignTrafficSource",
    "ym:s:lastsignSourceEngine",
    "ym:s:lastsignUTMSource",
    "ym:s:lastsignUTMCampaign",
]
VARIANTS = {
    "A_base6": BASE,
    "B_plus_clientID": BASE + ["ym:s:clientID"],
    "C_plus_utmMedium": BASE + ["ym:s:lastsignUTMMedium"],
    "D_all8": [
        "ym:s:purchaseID",
        "ym:s:date",
        "ym:s:clientID",
        "ym:s:lastsignTrafficSource",
        "ym:s:lastsignSourceEngine",
        "ym:s:lastsignUTMSource",
        "ym:s:lastsignUTMMedium",
        "ym:s:lastsignUTMCampaign",
    ],
}


def main() -> None:
    headers = {"Authorization": f"OAuth {os.environ['METRICA_TOKEN']}"}
    counter = os.environ["METRICA_COUNTER_ID"]
    for name, dims in VARIANTS.items():
        resp = requests.get(
            STAT,
            params={
                "ids": counter,
                "metrics": "ym:s:ecommercePurchases,ym:s:ecommerceRevenue",
                "dimensions": ",".join(dims),
                "date1": DATE1,
                "date2": DATE2,
                "attribution": "lastsign",
                "accuracy": "full",
                "proposed_accuracy": "false",
                "lang": "ru",
                "limit": 5,
            },
            headers=headers,
            timeout=180,
        )
        if resp.status_code != 200:
            print(f"{name}: HTTP {resp.status_code} {resp.text[:200]}")
        else:
            body = resp.json()
            sample = [
                [str(d.get("name"))[:20] for d in row.get("dimensions", [])]
                for row in (body.get("data") or [])[:2]
            ]
            print(f"{name}: total_rows={body.get('total_rows')} sampled={body.get('sampled')} {sample}")
        time.sleep(1)


if __name__ == "__main__":
    main()
