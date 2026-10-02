"""Заменить таргетинги в группах дубля 715011487 на сегменты конкретных курток.

Кампанию Павел склонировал в интерфейсе — структура, объявления, цены и карусель уже на
месте. Здесь меняются ТОЛЬКО условия показа, как и договаривались:

  1. модель группы определяется по slug в Href её текстового объявления (как в сборщике);
  2. в группу добавляется условие ретаргетинга «RT: смотрел <модель> <окно>д, без покупки»
     — все 30 условий уже существуют в кабинете, создавать нечего;
  3. фразы и автотаргетинг группы останавливаются (suspend, а не удаление: обратимо);
  4. прежние условия подбора аудитории группы («Интересы и привычки») останавливаются;
  5. товарные объявления останавливаются — Павел пока не хочет их показывать.

Идемпотентно: условие, уже привязанное к группе, повторно не добавляется.
APPLY=1 — писать. Без него только план.
"""

from __future__ import annotations

import json
import os
import sys

import requests

API = "https://api.direct.yandex.com/json/v5/"
CAMPAIGN = 715011487
EXPECTED_LOGIN_PART = "bjorn"

MODELS: dict[str, tuple[str, int]] = {
    "narvik": ("Нарвик", 10), "stavanger": ("Ставангер", 10), "troms": ("Тромсо", 10),
    "ruskeala": ("Рускеала", 10), "kotlin": ("Котлин", 30), "tuutari": ("Туутари", 10),
    "drammen": ("Драммен", 10), "skien": ("Скиен", 10), "vuoksa": ("Вуокса", 10),
    "monferrana": ("Монферрана", 30), "igora": ("Игора", 10), "kareliya": ("Карелия", 10),
    "hamar": ("Хамар", 10), "kronshtadt": ("Кронштадт", 10), "tonsberg": ("Тонсберг", 10),
    "-alta-": ("Альта", 10), "monrepo": ("Монрепо", 10), "olanga": ("Оланга", 10),
    "asker": ("Аскер", 30), "larvik": ("Ларвик", 10), "karedzhi": ("Кареджи", 30),
    "lillestrom": ("Лиллестром", 10), "akkala": ("Аккала", 10), "komarovo": ("Комарово", 10),
    "repino": ("Репино", 10), "mielisi": ("Миэлиси", 10), "okhta": ("Охта", 10),
    "vyborg": ("Выборг", 10), "kotlas": ("Котлас", 30), "ladoga": ("Ладога", 10),
}


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


def suspended(res: dict) -> int:
    return sum(1 for r in res.get("SuspendResults", []) if r.get("Id"))


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

    camp = need(call("campaigns", {
        "SelectionCriteria": {"Ids": [CAMPAIGN]},
        "FieldNames": ["Id", "Name", "State", "Status", "Type"]}, login, token), "кампания")
    if not camp.get("Campaigns"):
        sys.exit("кампания не найдена в кабинете")
    for c in camp["Campaigns"]:
        print(f"  «{c['Name']}» | {c.get('Type')} | State={c.get('State')} "
              f"Status={c.get('Status')}")

    conds = need(call("retargetinglists", {
        "SelectionCriteria": {}, "FieldNames": ["Id", "Name", "IsAvailable"],
        "Page": {"Limit": 1000}}, login, token), "условия")
    by_name = {c["Name"]: c for c in conds.get("RetargetingLists", [])}

    groups = need(call("adgroups", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["Id", "Name"], "Page": {"Limit": 1000}}, login, token), "группы")
    groups = groups.get("AdGroups", [])
    print(f"\nгрупп в кампании: {len(groups)}")

    ads = need(call("ads", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["Id", "AdGroupId", "Type", "State"],
        "TextAdFieldNames": ["Href"], "Page": {"Limit": 1000}}, login, token), "объявления")
    ads = ads.get("Ads", [])
    href_by_group: dict[int, str] = {}
    shopping: list[int] = []
    for a in ads:
        if a.get("Type") == "SHOPPING_AD":
            if a.get("State") != "SUSPENDED":
                shopping.append(a["Id"])
            continue
        href = (a.get("TextAd") or {}).get("Href") or ""
        if href and a["AdGroupId"] not in href_by_group:
            href_by_group[a["AdGroupId"]] = href
    print(f"объявлений: {len(ads)} (товарных к остановке {len(shopping)})")

    have = need(call("audiencetargets", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["Id", "AdGroupId", "RetargetingListId", "InterestId", "State"],
        "Page": {"Limit": 1000}}, login, token), "аудитории")
    have = have.get("AudienceTargets", [])
    have_rt: dict[int, set] = {}
    old_targets: list[int] = []
    for t in have:
        if t.get("RetargetingListId"):
            have_rt.setdefault(t["AdGroupId"], set()).add(t["RetargetingListId"])
        if t.get("InterestId") and t.get("State") != "SUSPENDED":
            old_targets.append(t["Id"])
    print(f"условий подбора в кампании: {len(have)} "
          f"(по интересам к остановке {len(old_targets)})")

    kw = need(call("keywords", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["Id", "AdGroupId", "Keyword", "State"],
        "Page": {"Limit": 10000}}, login, token), "фразы")
    kw = [k for k in kw.get("Keywords", []) if k.get("State") != "SUSPENDED"]
    auto = [k["Id"] for k in kw if k.get("Keyword") == "---autotargeting"]
    phrases = [k["Id"] for k in kw if k.get("Keyword") != "---autotargeting"]
    print(f"активных фраз {len(phrases)}, автотаргетинг в {len(auto)} группах")

    plan, missing = [], []
    for g in groups:
        href = href_by_group.get(g["Id"], "")
        slug = next((s for s in MODELS if s in href), None)
        if not slug:
            missing.append((g["Id"], g["Name"], href[:70] or "нет Href"))
            continue
        name, window = MODELS[slug]
        cond_name = f"RT: смотрел {name} {window}д, без покупки"
        cond = by_name.get(cond_name)
        if not cond:
            missing.append((g["Id"], g["Name"], f"нет условия «{cond_name}»"))
            continue
        if cond["Id"] in have_rt.get(g["Id"], set()):
            print(f"  уже привязано: {g['Id']} «{g['Name']}» -> {cond_name}")
            continue
        plan.append({"AdGroupId": g["Id"], "RetargetingListId": cond["Id"]})
        print(f"  {g['Id']} «{g['Name']}» -> {cond_name} ({cond['Id']}, "
              f"IsAvailable={cond.get('IsAvailable')})")
    if missing:
        print("\nне сопоставлено:")
        for gid, gname, why in missing:
            print(f"  ! {gid} «{gname}» — {why}")

    print(f"\nк привязке {len(plan)} условий")
    if not apply:
        print("без APPLY=1 ничего не меняю")
        return

    if plan:
        res = need(call("audiencetargets", {"AudienceTargets": plan}, login, token, "add"),
                   "привязка условий")
        for r in res.get("AddResults", []):
            if r.get("Errors"):
                print(f"  ошибка: {r['Errors']}")
            if r.get("Warnings"):
                print(f"  предупреждение: {r['Warnings']}")
        print(f"привязано условий: "
              f"{sum(1 for r in res.get('AddResults', []) if r.get('Id'))} из {len(plan)}")

    if phrases:
        res = need(call("keywords", {"SelectionCriteria": {"Ids": phrases}},
                        login, token, "suspend"), "остановка фраз")
        print(f"остановлено фраз: {suspended(res)}")
    if auto:
        res = need(call("keywords", {"SelectionCriteria": {"Ids": auto}},
                        login, token, "suspend"), "остановка автотаргетинга")
        print(f"остановлен автотаргетинг: {suspended(res)}")
    if old_targets:
        res = need(call("audiencetargets", {"SelectionCriteria": {"Ids": old_targets}},
                        login, token, "suspend"), "остановка интересов")
        print(f"остановлено условий по интересам: {suspended(res)}")
    if shopping:
        res = need(call("ads", {"SelectionCriteria": {"Ids": shopping}},
                        login, token, "suspend"), "остановка товарных")
        print(f"остановлено товарных объявлений: {suspended(res)}")

    print("\n### итог")
    chk = need(call("audiencetargets", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["AdGroupId", "RetargetingListId", "InterestId", "State"],
        "Page": {"Limit": 1000}}, login, token), "сверка аудиторий")
    acc: dict = {}
    for t in chk.get("AudienceTargets", []):
        kind = "ретаргет" if t.get("RetargetingListId") else "интересы"
        acc[(kind, t.get("State"))] = acc.get((kind, t.get("State")), 0) + 1
    for k, v in sorted(acc.items(), key=lambda x: str(x[0])):
        print(f"  {k[0]} State={k[1]} -> {v}")

    chk2 = need(call("keywords", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["State"], "Page": {"Limit": 10000}}, login, token), "сверка фраз")
    acc2: dict = {}
    for k in chk2.get("Keywords", []):
        acc2[k.get("State")] = acc2.get(k.get("State"), 0) + 1
    print(f"  фразы: {acc2}")

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


if __name__ == "__main__":
    main()
