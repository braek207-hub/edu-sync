# -*- coding: utf-8 -*-
"""Разовый probe: какие поля AppMetrica несут VK-кампанию/группу для установок.
Запуск через GH Actions (APPMETRICA_TOKEN в secrets). Печатает sample VK-установок
со ВСЕМИ campaign-related полями — чтобы понять, чем связать установки с ad_plan кабинета.
"""
import os
import time
from collections import Counter
from urllib.parse import parse_qs

import requests

BASE = "https://api.appmetrica.yandex.ru/logs/v1/export"
# Валидные поля трекера AppMetrica (campaign_id/campaign_name НЕ существуют → 400).
FIELDS = (
    "publisher_name,tracker_name,tracking_id,"
    "click_url_parameters,install_datetime"
)


def export(app_id, token, since, until):
    params = {
        "application_id": app_id,
        "date_since": f"{since} 00:00:00",
        "date_until": f"{until} 23:59:59",
        "date_dimension": "default",
        "fields": FIELDS,
    }
    headers = {"Authorization": f"OAuth {token}"}
    for _ in range(60):
        r = requests.get(f"{BASE}/installations.json", params=params, headers=headers, timeout=120)
        if r.status_code == 200:
            return r.json().get("data", [])
        if r.status_code == 202:
            time.sleep(20); continue
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    raise TimeoutError("файл не готов")


def main():
    token = os.environ["APPMETRICA_TOKEN"]
    app_id = os.environ.get("APPMETRICA_APP_ID") or "4415407"
    rows = export(app_id, token, "2026-07-01", "2026-07-14")
    vk = [r for r in rows if "vk" in (r.get("publisher_name") or "").lower()
          or "mytarget" in (r.get("publisher_name") or "").lower()]
    print(f"всего установок {len(rows)}, VK {len(vk)}")
    for field in ("tracker_name", "tracking_id", "click_url_parameters"):
        filled = sum(1 for r in vk if (r.get(field) or "").strip())
        print(f"  {field}: непусто у {filled}/{len(vk)} VK-установок")
    # ВСЕ ключи click_url_parameters у VK — под каким ключом лежит кампания/группа?
    key_counter = Counter()
    for r in vk:
        for k in parse_qs(r.get("click_url_parameters") or "").keys():
            key_counter[k] += 1
    print("--- ключи click_url_parameters (VK) ---")
    for k, cnt in key_counter.most_common(20):
        print(f"  {cnt:5d}  {k}")
    print("--- sample VK (10) ---")
    for r in vk[:10]:
        print(f"tracker={r.get('tracker_name')!r} tracking_id={r.get('tracking_id')!r}")
        print(f"    click_url={ (r.get('click_url_parameters') or '')[:200] }")
    print("--- топ tracker_name (VK) ---")
    for name, cnt in Counter((r.get("tracker_name") or "") for r in vk).most_common(10):
        print(f"  {cnt:5d}  {name!r}")


if __name__ == "__main__":
    main()
