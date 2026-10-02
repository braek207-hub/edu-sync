"""В каком счётчике лежат RT-сегменты и виден ли там счётчик «Bjorn larsen NEW» (98014193).

В Аудиториях выбран счётчик 98014193, а сегмент 1008116803 в списке не нашёлся: либо
поле ищет по названию, либо сегмент создан в другом счётчике BJORN. Проверяем оба.
Значение METRICA_COUNTER_ID не печатаем — только совпадает оно с 98014193 или нет.
"""

from __future__ import annotations

import os

import requests

MGMT = "https://api-metrika.yandex.net/management/v1"
UI_COUNTER = "98014193"


def main() -> None:
    token = os.environ["METRICA_TOKEN"].strip()
    env_counter = os.environ["METRICA_COUNTER_ID"].strip()
    h = {"Authorization": f"OAuth {token}"}
    print(f"METRICA_COUNTER_ID совпадает с {UI_COUNTER}: "
          f"{'да' if env_counter == UI_COUNTER else 'НЕТ'}")

    r = requests.get(f"{MGMT}/counters", headers=h,
                     params={"per_page": 1000, "field": "site,name"}, timeout=60)
    print(f"\nGET /counters -> HTTP {r.status_code}")
    if r.status_code == 200:
        for c in r.json().get("counters", []):
            if "bjorn" in f"{c.get('name')} {c.get('site')}".lower():
                print(f"  {c.get('id')} | {c.get('name')} | {c.get('site')}")

    for cid in {env_counter, UI_COUNTER}:
        rs = requests.get(f"{MGMT}/counter/{cid}/segments", headers=h, timeout=60)
        label = cid if cid == UI_COUNTER else "(счётчик из секрета)"
        print(f"\nсегменты счётчика {label}: HTTP {rs.status_code}")
        if rs.status_code != 200:
            print(f"  {rs.text[:200]}")
            continue
        for s in rs.json().get("segments", []):
            print(f"  {s.get('segment_id')} | {s.get('name')}")


if __name__ == "__main__":
    main()
