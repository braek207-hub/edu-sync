"""Принимает ли Logs API поля объявления для ОБЕИХ моделей витрины?

Перемер грани делался только на lastSign. Синк заполняет ещё и модель first, и если
ym:s:firstDirectClickBanner не существует, ночной прогон упадёт на создании запроса —
причём молча для дашборда, потому что витрина просто перестанет обновляться.

Проверка идёт через /logrequests/evaluate: он не создаёт выгрузку и не тратит квоту.
"""

from __future__ import annotations

import os

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BASE = "https://api-metrika.yandex.net/management/v1/counter/{counter}"
DAY = os.environ.get("PROBE_DATE1") or "2026-08-20"
TOKEN = os.environ["METRICA_TOKEN"]
COUNTER = os.environ["METRICA_COUNTER_ID"]
HEADERS = {"Authorization": f"OAuth {TOKEN}"}


def visit_fields(prefix: str) -> str:
    return (
        f"ym:s:visitID,ym:s:{prefix}TrafficSource,"
        f"ym:s:{prefix}UTMCampaign,ym:s:{prefix}DirectClickOrder,ym:s:{prefix}DirectClickBanner"
    )


def main() -> None:
    for model, prefix in {"first": "first", "lastsign": "lastSign"}.items():
        resp = requests.get(
            BASE.format(counter=COUNTER) + "/logrequests/evaluate",
            params={"date1": DAY, "date2": DAY, "fields": visit_fields(prefix), "source": "visits"},
            headers=HEADERS,
            timeout=120,
        )
        body = resp.text[:300]
        print(f"{model:9s} HTTP {resp.status_code}: {body}")


if __name__ == "__main__":
    main()
