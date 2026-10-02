"""Кто такой METRICA_TOKEN и что ему разрешено в счётчике BJORN.

Сегменты читаются, а POST отдаёт 403 — надо отличить «приехал не тот токен» от «у токена
нет scope записи в Метрику». Печатаем только длину и владельца, значение токена — никогда.
"""

from __future__ import annotations

import json
import os

import requests

METRIKA = "https://api-metrika.yandex.net/management/v1"


def main() -> None:
    token = os.environ["METRICA_TOKEN"].strip()
    counter = os.environ["METRICA_COUNTER_ID"].strip()
    print(f"METRICA_TOKEN: длина {len(token)}")

    r = requests.get("https://login.yandex.ru/info", params={"format": "json"},
                     headers={"Authorization": f"OAuth {token}"}, timeout=30)
    print(f"login.yandex.ru/info -> HTTP {r.status_code}")
    if r.status_code == 200:
        j = r.json()
        print(f"  владелец токена: {j.get('login')} | id {j.get('id')}")

    h = {"Authorization": f"OAuth {token}"}
    rc = requests.get(f"{METRIKA}/counter/{counter}", headers=h, timeout=60)
    print(f"GET /counter/{counter} -> HTTP {rc.status_code}")
    if rc.status_code == 200:
        c = rc.json().get("counter", {})
        print(f"  permission={c.get('permission')} владелец={c.get('owner_login')} "
              f"сайт={c.get('site')}")

    rs = requests.get(f"{METRIKA}/counter/{counter}/segments", headers=h, timeout=60)
    print(f"GET /segments -> HTTP {rs.status_code}")
    if rs.status_code == 200:
        for s in rs.json().get("segments", []):
            print(f"  {s.get('segment_id')} | {s.get('name')}")

    rg = requests.get(f"{METRIKA}/counter/{counter}/goals", headers=h, timeout=60)
    print(f"GET /goals -> HTTP {rg.status_code} (чтение целей — та же область, что сегменты)")

    body = json.dumps({"segment": {"name": "RT: проба записи (удалить)",
                                   "expression": "ym:pv:URL=@'narvik'"}},
                      ensure_ascii=False).encode("utf-8")
    rp = requests.post(f"{METRIKA}/counter/{counter}/segments",
                       headers={**h, "Content-Type": "application/json; charset=utf-8"},
                       data=body, timeout=60)
    print(f"POST /segments -> HTTP {rp.status_code}")
    print(f"  ответ: {rp.text[:500]}")
    if rp.status_code in (200, 201):
        sid = rp.json().get("segment", {}).get("segment_id")
        rd = requests.delete(f"{METRIKA}/counter/{counter}/segment/{sid}", headers=h, timeout=60)
        print(f"  пробу {sid} удалил: HTTP {rd.status_code}")


if __name__ == "__main__":
    main()
