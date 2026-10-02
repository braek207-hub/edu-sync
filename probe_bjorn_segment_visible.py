"""Можно ли через API получить сегмент, видимый в Аудиториях.

У созданных через API сегментов стоит segment_source=api, и Аудитории их не предлагают.
Доки полей записи не описывают, поэтому проверяем три вещи фактом:
  1. POST с segment_source="interface" — примет ли и что запишет;
  2. POST с retargeting/is_retargeting — в счётчике есть 3 сегмента с is_retargeting=1;
  3. PUT тех же полей на уже существующий 1008116803.
Пробные сегменты создаются с префиксом ZZPROBE и удаляются в конце — мусор не оставляем.
"""

from __future__ import annotations

import json
import os

import requests

MGMT = "https://api-metrika.yandex.net/management/v1"
TARGET = 1008116803
EXPR = "ym:pv:URL=@'/product' AND ym:s:bounce=='No' AND ym:s:pageViews>=2"


def main() -> None:
    token = os.environ["METRICA_TOKEN"].strip()
    counter = os.environ["METRICA_COUNTER_ID"].strip()
    h = {"Authorization": f"OAuth {token}"}
    hj = {**h, "Content-Type": "application/json; charset=utf-8"}
    created = []

    def post(label: str, seg: dict) -> None:
        body = json.dumps({"segment": seg}, ensure_ascii=False).encode("utf-8")
        r = requests.post(f"{MGMT}/counter/{counter}/segments", headers=hj,
                          data=body, timeout=60)
        print(f"\n{label} -> HTTP {r.status_code}")
        if r.status_code not in (200, 201):
            print(f"  {r.text[:300]}")
            return
        s = r.json().get("segment", {})
        created.append(s.get("segment_id"))
        print(f"  id={s.get('segment_id')} segment_source={s.get('segment_source')} "
              f"is_retargeting={s.get('is_retargeting')} retargeting={s.get('retargeting')}")

    post("1. segment_source=interface",
         {"name": "ZZPROBE source", "expression": EXPR, "segment_source": "interface"})
    post("2. retargeting=true + is_retargeting=1",
         {"name": "ZZPROBE retarget", "expression": EXPR,
          "retargeting": True, "is_retargeting": 1})
    post("3. оба признака вместе",
         {"name": "ZZPROBE both", "expression": EXPR, "segment_source": "interface",
          "retargeting": True, "is_retargeting": 1})

    body = json.dumps({"segment": {
        "name": "RT: интерес к каталогу · 30д · не отказ",
        "segment_source": "interface", "retargeting": True, "is_retargeting": 1}},
        ensure_ascii=False).encode("utf-8")
    ru = requests.put(f"{MGMT}/counter/{counter}/segment/{TARGET}", headers=hj,
                      data=body, timeout=60)
    print(f"\n4. PUT признаков на {TARGET} -> HTTP {ru.status_code}")
    if ru.status_code == 200:
        s = ru.json().get("segment", {})
        print(f"  segment_source={s.get('segment_source')} "
              f"is_retargeting={s.get('is_retargeting')}")
    else:
        print(f"  {ru.text[:300]}")

    print("\n### кто такие 3 сегмента с is_retargeting=1")
    rl = requests.get(f"{MGMT}/counter/{counter}/segments", headers=h, timeout=60)
    if rl.status_code == 200:
        for s in rl.json().get("segments", []):
            if s.get("is_retargeting"):
                print(f"  {s.get('segment_id')} | {s.get('name')} | "
                      f"source={s.get('segment_source')} | create={s.get('create_time')}")

    print("\n### уборка")
    for sid in created:
        rd = requests.delete(f"{MGMT}/counter/{counter}/segment/{sid}", headers=h, timeout=60)
        print(f"  удаление {sid} -> HTTP {rd.status_code}")


if __name__ == "__main__":
    main()
