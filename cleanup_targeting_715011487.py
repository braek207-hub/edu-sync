"""Довести таргетинги 715011487: снять чужие условия подбора и остановить товарные.

После первого прохода в группах стало по три условия: наше «RT: смотрел …» плюс два
унаследованных от образца (в Директе «Интересы и привычки» и прочие аудитории живут как
audiencetargets с RetargetingListId, а не с InterestId — поэтому первый проход их не тронул).
Замысел кампании — один сегмент на группу, значит чужие условия останавливаем.

Товарные объявления в первом проходе остановить не удалось, причина в ответе не печаталась:
здесь показываем Errors и Warnings поэлементно и их статусы.

APPLY=1 — писать. Без него только план.
"""

from __future__ import annotations

import json
import os
import sys

import requests

API = "https://api.direct.yandex.com/json/v5/"
CAMPAIGN = 715011487
OUR_PREFIX = "RT: смотрел"
EXPECTED_LOGIN_PART = "bjorn"


def call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    resp = requests.post(API + service, data=body, headers={
        "Authorization": f"Bearer {token}", "Client-Login": login,
        "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"},
        timeout=180)
    try:
        return resp.json()
    except Exception:
        return {"error": {"error_string": f"HTTP {resp.status_code}",
                          "error_detail": resp.text[:300]}}


def need(body: dict, what: str) -> dict:
    e = body.get("error")
    if e:
        sys.exit(f"СТОП {what}: {e.get('error_string')} | {e.get('error_detail')}")
    return body.get("result", {})


def report(res: dict, key: str, what: str) -> None:
    rows = res.get(key) or []
    ok = sum(1 for r in rows if r.get("Id"))
    print(f"{what}: {ok} из {len(rows)}")
    for r in rows:
        for er in r.get("Errors", []):
            print(f"    ошибка {er.get('Code')}: {er.get('Message')} "
                  f"{er.get('Details') or ''}")
        for w in r.get("Warnings", []):
            print(f"    предупреждение {w.get('Code')}: {w.get('Message')} "
                  f"{w.get('Details') or ''}")


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
        sys.exit("логин BJORN не найден")
    print(f"кабинет {login} | кампания {CAMPAIGN} | режим: {'ЗАПИСЬ' if apply else 'план'}")

    conds = need(call("retargetinglists", {
        "SelectionCriteria": {}, "FieldNames": ["Id", "Name", "Type", "Scope"],
        "Page": {"Limit": 1000}}, login, token), "условия")
    cond_by_id = {c["Id"]: c for c in conds.get("RetargetingLists", [])}
    ours = {cid for cid, c in cond_by_id.items()
            if str(c.get("Name", "")).startswith(OUR_PREFIX)}
    print(f"условий в кабинете {len(cond_by_id)}, наших «{OUR_PREFIX}…» {len(ours)}")

    targets = need(call("audiencetargets", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["Id", "AdGroupId", "RetargetingListId", "InterestId", "State"],
        "Page": {"Limit": 1000}}, login, token), "аудитории")
    targets = targets.get("AudienceTargets", [])

    alien, kept = [], 0
    kinds: dict = {}
    for t in targets:
        rid = t.get("RetargetingListId")
        name = (cond_by_id.get(rid) or {}).get("Name", f"интерес {t.get('InterestId')}")
        ctype = (cond_by_id.get(rid) or {}).get("Type", "INTEREST")
        if rid in ours:
            kept += 1
            continue
        kinds[(ctype, name)] = kinds.get((ctype, name), 0) + 1
        if t.get("State") != "SUSPENDED":
            alien.append(t["Id"])
    print(f"\nусловий в кампании {len(targets)}: наших {kept}, чужих {len(targets) - kept} "
          f"(активных к остановке {len(alien)})")
    for (ctype, name), cnt in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"  {ctype} | {name} -> {cnt} групп")

    ads = need(call("ads", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN], "Types": ["SHOPPING_AD"]},
        "FieldNames": ["Id", "AdGroupId", "State", "Status", "StatusClarification"],
        "Page": {"Limit": 1000}}, login, token), "товарные")
    ads = ads.get("Ads", [])
    acc: dict = {}
    for a in ads:
        acc[(a.get("State"), a.get("Status"))] = acc.get((a.get("State"), a.get("Status")), 0) + 1
    print(f"\nтоварных объявлений {len(ads)}:")
    for k, v in sorted(acc.items(), key=lambda x: str(x[0])):
        print(f"  State={k[0]} Status={k[1]} -> {v}")
    if ads:
        print(f"  пример: {json.dumps(ads[0], ensure_ascii=False)}")
    to_stop = [a["Id"] for a in ads if a.get("State") != "SUSPENDED"]

    if not apply:
        print(f"\nплан: остановить {len(alien)} чужих условий и {len(to_stop)} товарных")
        return

    if alien:
        res = need(call("audiencetargets", {"SelectionCriteria": {"Ids": alien}},
                        login, token, "suspend"), "остановка чужих условий")
        report(res, "SuspendResults", "остановлено чужих условий")
    if to_stop:
        res = need(call("ads", {"SelectionCriteria": {"Ids": to_stop}},
                        login, token, "suspend"), "остановка товарных")
        report(res, "SuspendResults", "остановлено товарных")

    print("\n### итог")
    chk = need(call("audiencetargets", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["RetargetingListId", "State"],
        "Page": {"Limit": 1000}}, login, token), "сверка условий")
    acc2: dict = {}
    for t in chk.get("AudienceTargets", []):
        kind = "наше" if t.get("RetargetingListId") in ours else "чужое"
        acc2[(kind, t.get("State"))] = acc2.get((kind, t.get("State")), 0) + 1
    for k, v in sorted(acc2.items(), key=lambda x: str(x[0])):
        print(f"  условие {k[0]} State={k[1]} -> {v}")

    chk3 = need(call("ads", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["Type", "State", "Status"], "Page": {"Limit": 1000}}, login, token),
        "сверка объявлений")
    acc3: dict = {}
    for a in chk3.get("Ads", []):
        key = (a.get("Type"), a.get("State"), a.get("Status"))
        acc3[key] = acc3.get(key, 0) + 1
    for k, v in sorted(acc3.items(), key=lambda x: str(x[0])):
        print(f"  {k[0]} State={k[1]} Status={k[2]} -> {v}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
