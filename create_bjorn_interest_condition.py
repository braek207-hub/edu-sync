"""Условие подбора аудитории на сегмент «интерес к каталогу».

Сегмент Метрики 1008116803 (каталог или карточка, не отказ, 30 дней, без купивших, охват
12 070 человек) заводим в кабинет как условие ретаргетинга — тогда его можно ставить в любую
кампанию без Яндекс Аудиторий. Аудитории нужны только под look-alike и медийку.

Вторым правилом отсекаем купивших за 30 дней: в сегменте вычет сделан на уровне визита,
здесь — на уровне человека.

APPLY=1 — писать.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.direct.yandex.com/json/v5/"
SEGMENT_ID = 1008116803
GOAL_PURCHASE = 341172800
PURCHASE_DAYS = 30
SEGMENT_LIFESPAN = 540        # для сегментов Директ срок игнорирует, шлём максимум
COND_NAME = "RT: интерес к каталогу 30д, без покупки"
EXPECTED_LOGIN_PART = "bjorn"


def call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API + service, data=body, headers={
        "Authorization": "Bearer " + token, "Client-Login": login,
        "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode())


def main() -> None:
    apply = os.environ.get("APPLY", "").strip() == "1"
    token = os.environ.get("DIRECT_TOKEN", "").strip()
    login = ""
    for item in json.loads(os.environ.get("DIRECT_CLIENTS_JSON") or "[]"):
        cand = item.get("login") or item.get("client_login") if isinstance(item, dict) else item
        if cand and EXPECTED_LOGIN_PART in str(cand).lower():
            login = str(cand).strip()
            if isinstance(item, dict) and item.get("token"):
                token = str(item["token"]).strip()
            break
    if not login:
        sys.exit("в DIRECT_CLIENTS_JSON нет логина BJORN")
    print(f"кабинет {login} | режим: {'ЗАПИСЬ' if apply else 'план'}")

    res = call("retargetinglists", {
        "SelectionCriteria": {},
        "FieldNames": ["Id", "Name", "IsAvailable"],
        "Page": {"Limit": 1000}}, login, token)
    for row in (res.get("result") or {}).get("RetargetingLists", []):
        if row.get("Name") == COND_NAME:
            print(f"условие уже есть: {row['Id']} | IsAvailable={row.get('IsAvailable')}")
            return

    payload = {
        "Name": COND_NAME,
        "Type": "RETARGETING",
        "Rules": [
            {"Operator": "ALL", "Arguments": [
                {"MembershipLifeSpan": SEGMENT_LIFESPAN, "ExternalId": SEGMENT_ID}]},
            {"Operator": "NONE", "Arguments": [
                {"MembershipLifeSpan": PURCHASE_DAYS, "ExternalId": GOAL_PURCHASE}]},
        ],
    }
    print(f"создаю «{COND_NAME}»: ALL(сегмент {SEGMENT_ID}) + "
          f"NONE(покупка {PURCHASE_DAYS}д)")
    if not apply:
        return

    add = call("retargetinglists", {"RetargetingLists": [payload]}, login, token, "add")
    if add.get("error"):
        sys.exit(f"ошибка: {add['error'].get('error_string')} | "
                 f"{add['error'].get('error_detail')}")
    r0 = (add.get("result") or {}).get("AddResults", [{}])[0]
    if r0.get("Errors"):
        sys.exit(f"ошибка элемента: {r0['Errors']}")
    rid = r0.get("Id")
    print(f"создано условие {rid}")

    chk = call("retargetinglists", {
        "SelectionCriteria": {"Ids": [rid]},
        "FieldNames": ["Id", "Name", "Scope", "IsAvailable"]}, login, token)
    for row in (chk.get("result") or {}).get("RetargetingLists", []):
        print(f"проверка: {row['Id']} «{row['Name']}» Scope={row.get('Scope')} "
              f"IsAvailable={row.get('IsAvailable')}")


if __name__ == "__main__":
    main()
