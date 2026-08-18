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


def params_report(name: str, rows) -> None:
    """Какие ключи лежат в click_url_parameters у Директа и есть ли там кампания.
    Печатаются только имена параметров и пример числового значения-кампании —
    не сырые ссылки (там yclid и прочие идентификаторы кликов)."""
    if rows is None:
        return
    import re
    from urllib.parse import parse_qsl
    direct = [r for r in rows if (r.get("publisher_name") or "").startswith("Yandex.Direct")]
    print(f"\n== {name}: Директ-строк {len(direct):,} из {len(rows):,} ==")
    keys = Counter()
    camp_vals = Counter()
    with_params = 0
    for r in direct:
        raw = (r.get("click_url_parameters") or "").strip()
        if not raw:
            continue
        with_params += 1
        pairs = dict(parse_qsl(raw)) if "=" in raw else {}
        for k in pairs:
            keys[k] += 1
        for key in ("campaign_id", "campaignid", "cid", "campaign"):
            v = (pairs.get(key) or "").strip()
            if re.fullmatch(r"\d{6,12}", v):
                camp_vals[key] += 1
    print(f"  строк с click_url_parameters: {with_params:,}/{len(direct):,}")
    print(f"  ключи параметров: {keys.most_common(20)}")
    print(f"  числовая кампания по ключам: {dict(camp_vals)}")


def main() -> None:
    to = date.today() - timedelta(days=1)
    frm = to - timedelta(days=2)
    print(f"окно {frm}..{to}")

    inst = export_json("installations", frm.isoformat(), to.isoformat(),
                       "appmetrica_device_id,publisher_name,tracker_name,click_url_parameters")
    report("installations 3д", inst)
    params_report("installations 3д, click_url_parameters", inst)

    # По коду диплинки click_url_parameters не отдают (HTTP 400) — перепроверяем:
    # если API поменялся, кампанию можно доставать и из них.
    dl = export_json("deeplinks", frm.isoformat(), to.isoformat(),
                     "appmetrica_device_id,publisher_name,tracker_name,click_url_parameters")
    if dl is None:
        print("deeplinks: click_url_parameters всё ещё не отдаётся (см. HTTP выше)")
    else:
        report("deeplinks 3д", dl)
        params_report("deeplinks 3д, click_url_parameters", dl)


if __name__ == "__main__":
    main()
