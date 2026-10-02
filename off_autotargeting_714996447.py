"""Снимает автотаргетинг в кампании ретаргета по модели.

Директ включает автотаргетинг в новых группах сам. Для этой кампании он вреден: замысел —
показывать только тем, кто смотрел конкретную куртку, а автотаргетинг добирает аудиторию
сам и размывает посыл. В API автотаргетинг живёт как фраза вида «---autotargeting»,
поэтому пробуем keywords.suspend, а если не выйдет — keywords.delete.

APPLY=1 — писать. Без него только показываем, что нашли.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.direct.yandex.com/json/v5/"
CAMPAIGN = 714996447
EXPECTED_LOGIN_PART = "bjorn"


def call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API + service, data=body, headers={
        "Authorization": "Bearer " + token,
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode())


def main() -> None:
    apply = os.environ.get("APPLY", "").strip() == "1"
    default_token = os.environ.get("DIRECT_TOKEN", "").strip()
    login, token = "", default_token
    for item in json.loads(os.environ["DIRECT_CLIENTS_JSON"] or "[]"):
        cand = item.get("login") or item.get("client_login") if isinstance(item, dict) else item
        if cand and EXPECTED_LOGIN_PART in str(cand).lower():
            login = str(cand).strip()
            if isinstance(item, dict) and item.get("token"):
                token = str(item["token"]).strip()
            break
    if not login:
        sys.exit("в DIRECT_CLIENTS_JSON нет логина, похожего на BJORN")
    print(f"кабинет {login} | режим: {'ЗАПИСЬ' if apply else 'только смотрим'}")

    res = call("keywords", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["Id", "AdGroupId", "Keyword", "State", "Status"],
        "Page": {"Limit": 1000}}, login, token)
    rows = (res.get("result") or {}).get("Keywords", [])
    auto = [k for k in rows if "autotargeting" in str(k.get("Keyword")).lower()]
    print(f"фраз всего {len(rows)}, из них автотаргетинг {len(auto)}")
    for k in auto[:5]:
        print(f"  {k['Id']} группа {k['AdGroupId']} | {k.get('Keyword')} "
              f"[{k.get('State')}/{k.get('Status')}]")
    if not auto or not apply:
        return

    ids = [k["Id"] for k in auto]
    sres = call("keywords", {"SelectionCriteria": {"Ids": ids}}, login, token, "suspend")
    ok = sum(1 for r in (sres.get("result") or {}).get("SuspendResults", [])
             if not r.get("Errors"))
    print(f"suspend: остановлено {ok} из {len(ids)}")
    for r in (sres.get("result") or {}).get("SuspendResults", [])[:3]:
        if r.get("Errors"):
            print(f"  ошибка: {r['Errors']}")
    if sres.get("error"):
        print(f"  ошибка запроса: {sres['error']}")

    check = call("keywords", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["Id", "Keyword", "State"],
        "Page": {"Limit": 1000}}, login, token)
    after = [k for k in (check.get("result") or {}).get("Keywords", [])
             if "autotargeting" in str(k.get("Keyword")).lower()]
    states: dict[str, int] = {}
    for k in after:
        states[str(k.get("State"))] = states.get(str(k.get("State")), 0) + 1
    print(f"после: автотаргетингов {len(after)} по состояниям {states}")


if __name__ == "__main__":
    main()
