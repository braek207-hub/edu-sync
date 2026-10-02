"""Дособрать клон 714996447: перенести товарные объявления образца и сразу остановить их.

Сборщик кампании фильтровал Types=["TEXT_AD"] и забрал 33 ТГО из 66 объявлений образца —
33 товарных (SHOPPING_AD), которые и дают цену и карусель, остались в 714000003. Здесь
читаем их целиком, создаём в соответствующих группах клона по совпадению имени группы и
приостанавливаем: Павел хочет пока их не показывать.

Поля чтения и записи у Директа расходятся, поэтому модерационные и вычисляемые
(*Moderation, FeedProcessingStatus) выбрасываем, а остальное переносим как есть.
Идемпотентность: если в группе клона товарное объявление уже есть, пропускаем.

APPLY=1 — писать. Без него печатаем структуру и план.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.direct.yandex.com/json/v5/"
SOURCE = 714000003
CLONE = 714996447
EXPECTED_LOGIN_PART = "bjorn"

# BodySources и DefaultBodies Директ на чтении не принимает — проверено полем за полем
SHOPPING_FIELDS = ["SitelinkSetId", "AdExtensions", "BusinessId", "TrackingPhoneId",
                   "FeedId", "FeedFilterConditions", "TitleSources", "TextSources",
                   "DefaultTexts", "GenerationScopes", "ListingFeedFilterConditions",
                   "ListingTitleSources", "ListingTextSources"]
# Модерация и статус обработки фида вычисляются Директом, а уточнения (AdExtensions)
# на записи объявления не принимаются — привязываются отдельно
DROP = {"FeedProcessingStatus", "SitelinksModeration", "AdExtensions"}


def call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API + service, data=body, headers={
        "Authorization": "Bearer " + token, "Client-Login": login,
        "Accept-Language": "ru", "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode())


def clean(block: dict) -> dict:
    """Чтение отдаёт {"Items": [...]}, запись ждёт голый массив — разворачиваем обёртку."""
    out = {}
    for k, v in block.items():
        if v is None or k in DROP:
            continue
        if isinstance(v, dict) and set(v) == {"Items"}:
            v = v["Items"]
        out[k] = v
    return out


def groups(camp: int, login: str, token: str) -> dict:
    res = call("adgroups", {"SelectionCriteria": {"CampaignIds": [camp]},
                            "FieldNames": ["Id", "Name"],
                            "Page": {"Limit": 1000}}, login, token)
    return {g["Name"]: g["Id"] for g in (res.get("result") or {}).get("AdGroups", [])}


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
    print(f"кабинет {login} | режим: {'ЗАПИСЬ' if apply else 'план'}")

    src_groups = {v: k for k, v in groups(SOURCE, login, token).items()}
    dst_groups = groups(CLONE, login, token)
    print(f"групп: образец {len(src_groups)}, клон {len(dst_groups)}")

    res = call("ads", {
        "SelectionCriteria": {"CampaignIds": [SOURCE], "Types": ["SHOPPING_AD"]},
        "FieldNames": ["Id", "AdGroupId", "Type"],
        "ShoppingAdFieldNames": SHOPPING_FIELDS,
        "Page": {"Limit": 1000}}, login, token)
    if res.get("error"):
        sys.exit(f"чтение товарных: {res['error'].get('error_string')} | "
                 f"{res['error'].get('error_detail')}")
    src_ads = (res.get("result") or {}).get("Ads", [])
    print(f"товарных объявлений в образце: {len(src_ads)}")
    if src_ads:
        print("\nструктура первого:")
        print(json.dumps(src_ads[0], ensure_ascii=False, indent=2)[:2000])

    have = call("ads", {
        "SelectionCriteria": {"CampaignIds": [CLONE], "Types": ["SHOPPING_AD"]},
        "FieldNames": ["Id", "AdGroupId"], "Page": {"Limit": 1000}}, login, token)
    have_groups = {a["AdGroupId"] for a in (have.get("result") or {}).get("Ads", [])}
    print(f"\nв клоне товарных уже есть: {len(have_groups)}")

    payload, skipped = [], 0
    for a in src_ads:
        name = src_groups.get(a["AdGroupId"])
        gid = dst_groups.get(name)
        if gid is None:
            print(f"  ! группа «{name}» в клоне не найдена — пропуск")
            skipped += 1
            continue
        if gid in have_groups:
            skipped += 1
            continue
        payload.append({"AdGroupId": gid, "ShoppingAd": clean(a.get("ShoppingAd") or {})})
    print(f"к созданию {len(payload)}, пропущено {skipped}")
    if not apply or not payload:
        return

    created = []
    for i in range(0, len(payload), 10):
        chunk = payload[i:i + 10]
        add = call("ads", {"Ads": chunk}, login, token, "add")
        if add.get("error"):
            sys.exit(f"ошибка add: {add['error'].get('error_string')} | "
                     f"{add['error'].get('error_detail')}")
        for r in (add.get("result") or {}).get("AddResults", []):
            if r.get("Errors"):
                print(f"  ошибка элемента: {r['Errors']}")
            if r.get("Warnings"):
                print(f"  предупреждение: {r['Warnings']}")
            if r.get("Id"):
                created.append(r["Id"])
    print(f"создано товарных: {len(created)}")

    if created:
        susp = call("ads", {"SelectionCriteria": {"Ids": created}}, login, token, "suspend")
        if susp.get("error"):
            print(f"остановка: {susp['error'].get('error_string')} | "
                  f"{susp['error'].get('error_detail')}")
        else:
            ok = sum(1 for r in (susp.get("result") or {}).get("SuspendResults", [])
                     if r.get("Id"))
            print(f"остановлено: {ok} из {len(created)}")

    chk = call("ads", {
        "SelectionCriteria": {"CampaignIds": [CLONE]},
        "FieldNames": ["Id", "Type", "State", "Status"], "Page": {"Limit": 1000}}, login, token)
    acc = {}
    for a in (chk.get("result") or {}).get("Ads", []):
        key = (a.get("Type"), a.get("State"), a.get("Status"))
        acc[key] = acc.get(key, 0) + 1
    print("\nитог по клону:")
    for k, v in sorted(acc.items(), key=lambda x: str(x[0])):
        print(f"  Type={k[0]} State={k[1]} Status={k[2]} -> {v}")


if __name__ == "__main__":
    main()
