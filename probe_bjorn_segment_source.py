"""Почему сегмент 1008116803 не виден ни в интерфейсе Метрики, ни в Аудиториях.

Гипотеза: у сегмента, созданного через Management API, стоит признак источника (api), и
такие сегменты Метрика показывает только Директу. Печатаем полный JSON наших сегментов
и для сравнения — список /apisegment/segments. Только чтение.
"""

from __future__ import annotations

import json
import os

import requests

MGMT = "https://api-metrika.yandex.net/management/v1"


def main() -> None:
    token = os.environ["METRICA_TOKEN"].strip()
    counter = os.environ["METRICA_COUNTER_ID"].strip()
    h = {"Authorization": f"OAuth {token}"}

    r = requests.get(f"{MGMT}/counter/{counter}/segments", headers=h, timeout=60)
    print(f"GET /segments -> HTTP {r.status_code}")
    segs = r.json().get("segments", []) if r.status_code == 200 else []
    print(f"всего сегментов: {len(segs)}\n")
    for s in segs:
        if str(s.get("segment_id")) == "1008116803" or "интерес" in str(s.get("name", "")):
            print("ПОЛНЫЙ JSON целевого сегмента:")
            print(json.dumps(s, ensure_ascii=False, indent=2))
            break
    if segs:
        print("\nключи объекта сегмента:", sorted(segs[0].keys()))
        print("\nсводка по источникам:")
        acc = {}
        for s in segs:
            key = (s.get("segment_source"), s.get("is_retargeting"))
            acc[key] = acc.get(key, 0) + 1
        for k, v in acc.items():
            print(f"  segment_source={k[0]} is_retargeting={k[1]} -> {v}")

    ra = requests.get(f"{MGMT}/counter/{counter}/apisegment/segments", headers=h, timeout=60)
    print(f"\nGET /apisegment/segments -> HTTP {ra.status_code}")
    if ra.status_code == 200:
        asegs = ra.json().get("segments", [])
        print(f"  apisegment-сегментов: {len(asegs)}")
        for s in asegs[:3]:
            print(f"  {s.get('segment_id')} | {s.get('name')}")
    else:
        print(f"  {ra.text[:200]}")


if __name__ == "__main__":
    main()
