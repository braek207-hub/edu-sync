"""Под каким логином созданы RT-сегменты и доступны ли они Директу.

В интерфейсе Метрики под audit-bjornlarsen сегментов не видно, а API их читает: сегменты
Метрики приватны для своего создателя. Нужно знать, под каким логином заходить в Метрику
и Аудитории, чтобы их увидеть. Плюс проверяем, что условия ретаргетинга Директа,
сославшиеся на эти сегменты, живые. Только чтение, значение токена не печатаем.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import requests

MGMT = "https://api-metrika.yandex.net/management/v1"
DIRECT = "https://api.direct.yandex.com/json/v5/"
EXPECTED_LOGIN_PART = "bjorn"


def direct(service: str, params: dict, login: str, token: str) -> dict:
    body = json.dumps({"method": "get", "params": params}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(DIRECT + service, data=body, headers={
        "Authorization": "Bearer " + token, "Client-Login": login,
        "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode())


def main() -> None:
    token = os.environ["METRICA_TOKEN"].strip()
    counter = os.environ["METRICA_COUNTER_ID"].strip()
    h = {"Authorization": f"OAuth {token}"}

    ri = requests.get("https://login.yandex.ru/info", params={"format": "json"},
                      headers=h, timeout=30)
    if ri.status_code == 200:
        j = ri.json()
        print(f"METRICA_TOKEN принадлежит логину: {j.get('login')} (id {j.get('id')})")
    else:
        print(f"login.yandex.ru/info -> HTTP {ri.status_code}")

    rc = requests.get(f"{MGMT}/counter/{counter}", headers=h, timeout=60)
    if rc.status_code == 200:
        c = rc.json().get("counter", {})
        print(f"счётчик: владелец {c.get('owner_login')}, наше право {c.get('permission')}")
        for gr in (c.get("grants") or []):
            print(f"  доступ: {gr.get('user_login')} | {gr.get('perm')}")

    dtoken = os.environ.get("DIRECT_TOKEN", "").strip()
    login = ""
    for item in json.loads(os.environ.get("DIRECT_CLIENTS_JSON") or "[]"):
        cand = item.get("login") or item.get("client_login") if isinstance(item, dict) else item
        if cand and EXPECTED_LOGIN_PART in str(cand).lower():
            login = str(cand).strip()
            if isinstance(item, dict) and item.get("token"):
                dtoken = str(item["token"]).strip()
            break
    if not login:
        print("\nлогин Директа не найден — проверку условий пропускаю")
        return

    print(f"\nусловия ретаргетинга в кабинете {login}:")
    res = direct("retargetinglists", {
        "SelectionCriteria": {},
        "FieldNames": ["Id", "Name", "Type", "Scope", "IsAvailable"],
        "Page": {"Limit": 1000}}, login, dtoken)
    rows = (res.get("result") or {}).get("RetargetingLists", [])
    rt = [l for l in rows if str(l.get("Name", "")).startswith("RT: смотрел")]
    print(f"  всего условий {len(rows)}, наших «RT: смотрел…» {len(rt)}")
    bad = [l for l in rt if l.get("IsAvailable") is False]
    print(f"  недоступных (IsAvailable=false): {len(bad)}")
    for l in rt[:3]:
        print(f"    {l['Id']} | {l['Name']} | Scope={l.get('Scope')} "
              f"| IsAvailable={l.get('IsAvailable')}")
    for l in bad[:5]:
        print(f"    ! {l['Id']} «{l['Name']}» недоступно")
    if res.get("error"):
        print(f"  ошибка: {res['error']}")


if __name__ == "__main__":
    main()
