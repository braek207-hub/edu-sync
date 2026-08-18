# -*- coding: utf-8 -*-
"""Probe атрибуции приложения LIME: чем можно привязать устройство к кампании.

Витрина разделов атрибутирует app-кампании через installations + deeplinks
(lime_sections_app.Attribution), и покрытие выходит ~0.3% от трафика кампаний
витрины PROCONTEXT. Probe отвечает на два вопроса:

1. Какие publisher_name реально приходят в installations/deeplinks — ловит ли
   их фильтр app_campaign_of (только "Yandex.Direct Auto-Tracking" + числовой
   tracker_name)?
2. Есть ли в выгрузке clicks появляется appmetrica_device_id (можно ли строить
   last-touch по кликам, которых на порядки больше диплинков)?

Read-only: только Logs API AppMetrica, база не трогается. Запуск — workflow
probe-appmetrica-attr.yml.
"""
import os
import time
from collections import Counter
from datetime import date, timedelta

import requests

from sync.lime_sections_common import APPMETRICA_APP

BASE = "https://api.appmetrica.yandex.ru/logs/v1/export"
TOKEN = os.environ["APPMETRICA_TOKEN"]


def export_json(endpoint: str, day_from: str, day_to: str, fields: str):
    params = {
        "application_id": APPMETRICA_APP,
        "date_since": f"{day_from} 00:00:00",
        "date_until": f"{day_to} 23:59:59",
        "date_dimension": "default",
        "fields": fields,
    }
    for attempt in range(240):
        r = requests.get(f"{BASE}/{endpoint}.json", params=params,
                         headers={"Authorization": f"OAuth {TOKEN}"}, timeout=900)
        if r.status_code == 200:
            return r.json().get("data", [])
        if r.status_code not in (202, 429):
            print(f"  {endpoint}: HTTP {r.status_code}: {r.text[:300]}")
            return None
        r.close()
        if attempt and attempt % 20 == 0:
            print(f"  {endpoint}: ждём выгрузку, попытка {attempt}")
        time.sleep(15)
    print(f"  {endpoint}: не дождались")
    return None


def digit_share(rows, pub_prefix: str) -> str:
    """Доля числовых tracker_name среди строк паблишеров с данным префиксом."""
    sub = [r for r in rows if (r.get("publisher_name") or "").startswith(pub_prefix)]
    if not sub:
        return "строк нет"
    ok = sum(1 for r in sub if (r.get("tracker_name") or "").strip().isdigit())
    return f"{ok}/{len(sub)} ({ok / len(sub):.0%})"


def report(name: str, rows) -> None:
    if rows is None:
        return
    print(f"\n== {name}: {len(rows):,} строк ==")
    pubs = Counter((r.get("publisher_name") or "<пусто>") for r in rows)
    for pub, n in pubs.most_common(15):
        print(f"  {pub}: {n:,}")
    print(f"  числовой tracker_name у 'Yandex.Direct': {digit_share(rows, 'Yandex.Direct')}")
    trackers = Counter(
        (r.get("tracker_name") or "<пусто>")
        for r in rows
        if (r.get("publisher_name") or "").startswith("Yandex.Direct")
    )
    print(f"  топ трекеров Директа: {trackers.most_common(10)}")


def main() -> None:
    to = date.today() - timedelta(days=1)
    frm = to - timedelta(days=6)
    day = to.isoformat()
    print(f"окно {frm}..{to}, день кликов {day}")

    report("installations 7д",
           export_json("installations", frm.isoformat(), to.isoformat(),
                       "appmetrica_device_id,publisher_name,tracker_name"))
    report("deeplinks 7д",
           export_json("deeplinks", frm.isoformat(), to.isoformat(),
                       "appmetrica_device_id,publisher_name,tracker_name"))

    # Клики: сначала пробуем с device id — если Logs API поле не знает (400),
    # повторяем без него, чтобы хотя бы увидеть объём и паблишеров.
    clicks = export_json("clicks", day, day,
                         "appmetrica_device_id,publisher_name,tracker_name,click_datetime")
    if clicks is None:
        clicks = export_json("clicks", day, day,
                             "publisher_name,tracker_name,click_datetime")
    report(f"clicks {day}", clicks)
    if clicks:
        with_dev = sum(1 for r in clicks if (r.get("appmetrica_device_id") or "").strip())
        print(f"  clicks с appmetrica_device_id: {with_dev:,}/{len(clicks):,}"
              f" ({with_dev / len(clicks):.1%})")


if __name__ == "__main__":
    main()
