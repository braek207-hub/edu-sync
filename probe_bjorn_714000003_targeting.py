"""Что нацеливает кампанию 714000003 сейчас и можно ли заменить это ретаргетом по товару.

  1. Права на счётчик Метрики: можем ли вообще создавать сегменты/цели (нужен permission=edit).
  2. Группы 714000003: фразы (включая автотаргетинг), условия подбора аудитории,
     корректировки, посадочные каждого объявления — из них получатся URL для сегментов.
  3. Сколько сегментов придётся завести и есть ли уже похожие.

Read-only.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict

import requests

API = "https://api.direct.yandex.com/json/v5/"
METRIKA = "https://api-metrika.yandex.net/management/v1"
CAMPAIGN = 714000003


def call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    resp = requests.post(API + service, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }, timeout=120)
    try:
        return resp.json()
    except Exception:
        return {"error": {"error_string": f"HTTP {resp.status_code}", "error_detail": resp.text[:300]}}


def err(body: dict) -> str | None:
    e = body.get("error")
    return None if not e else f"{e.get('error_string')} | {e.get('error_detail')}"[:400]


def clients() -> list[tuple[str, str]]:
    default_token = os.environ.get("DIRECT_TOKEN", "").strip()
    raw = os.environ.get("DIRECT_CLIENTS_JSON", "").strip()
    out: list[tuple[str, str]] = []
    if raw:
        for item in json.loads(raw):
            if isinstance(item, dict):
                login = str(item.get("login") or item.get("client_login") or "").strip()
                token = str(item.get("token") or "").strip() or default_token
                if login:
                    out.append((login, token))
            elif isinstance(item, str):
                out.append((item.strip(), default_token))
    return out


def metrika_rights() -> None:
    token = os.environ.get("METRICA_TOKEN", "").strip()
    counter = os.environ.get("METRICA_COUNTER_ID", "").strip()
    h = {"Authorization": f"OAuth {token}"}
    print("### 1. ПРАВА НА СЧЁТЧИК")
    r = requests.get(f"{METRIKA}/counter/{counter}", headers=h,
                     params={"field": "permission,owner_login,features,grants"}, timeout=60)
    if r.status_code != 200:
        print(f"  counter: HTTP {r.status_code} {r.text[:300]}")
        return
    c = r.json().get("counter", {})
    print(f"  доступ: permission={c.get('permission')} | владелец={c.get('owner_login')}")
    print(f"  выданные права: {json.dumps(c.get('grants'), ensure_ascii=False)[:600]}")
    print(f"  тип счётчика: {c.get('type')} | {c.get('site')}")


def targeting(login: str, token: str) -> None:
    print(f"\n### 2. ТАРГЕТИНГИ КАМПАНИИ {CAMPAIGN}")

    gr = call("adgroups", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["Id", "Name", "Status", "Type", "Subtype", "RegionIds"],
        "Page": {"Limit": 500},
    }, login, token)
    if err(gr):
        print(f"  adgroups.get: {err(gr)}")
        return
    groups = gr.get("result", {}).get("AdGroups", [])
    gname = {g["Id"]: g.get("Name") for g in groups}
    print(f"  групп: {len(groups)}")

    kw = call("keywords", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["Id", "AdGroupId", "Keyword", "State", "Status", "UserParam1"],
        "Page": {"Limit": 3000},
    }, login, token)
    kw_by_group: dict[int, list[str]] = defaultdict(list)
    if err(kw):
        print(f"  keywords.get: {err(kw)}")
    else:
        for k in kw.get("result", {}).get("Keywords", []):
            kw_by_group[k["AdGroupId"]].append(f"{k.get('Keyword')} [{k.get('State')}]")
        print(f"  фраз всего: {sum(len(v) for v in kw_by_group.values())}")

    at = call("audiencetargets", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["Id", "AdGroupId", "RetargetingListId", "InterestId", "State",
                       "ContextBid", "StrategyPriority"],
        "Page": {"Limit": 1000},
    }, login, token)
    at_by_group: dict[int, list[dict]] = defaultdict(list)
    if err(at):
        print(f"  audiencetargets.get: {err(at)}")
    else:
        for a in at.get("result", {}).get("AudienceTargets", []):
            at_by_group[a["AdGroupId"]].append(a)
        print(f"  условий подбора аудитории: {sum(len(v) for v in at_by_group.values())}")

    ads = call("ads", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN]},
        "FieldNames": ["Id", "AdGroupId", "Type", "State"],
        "TextAdFieldNames": ["Href", "Title"],
        "Page": {"Limit": 2000},
    }, login, token)
    href_by_group: dict[int, set[str]] = defaultdict(set)
    kinds_by_group: dict[int, Counter] = defaultdict(Counter)
    if err(ads):
        print(f"  ads.get: {err(ads)}")
    else:
        for a in ads.get("result", {}).get("Ads", []):
            kinds_by_group[a["AdGroupId"]][a.get("Type")] += 1
            href = (a.get("TextAd") or {}).get("Href")
            if href:
                href_by_group[a["AdGroupId"]].add(href.split("?")[0])

    bm = call("bidmodifiers", {
        "SelectionCriteria": {"CampaignIds": [CAMPAIGN], "Levels": ["CAMPAIGN", "AD_GROUP"]},
        "FieldNames": ["Id", "AdGroupId", "Type", "Level"],
        "Page": {"Limit": 1000},
    }, login, token)
    bm_by_group: dict[int, Counter] = defaultdict(Counter)
    if err(bm):
        print(f"  bidmodifiers.get: {err(bm)}")
    else:
        for m in bm.get("result", {}).get("BidModifiers", []):
            bm_by_group[m.get("AdGroupId") or 0][m.get("Type")] += 1

    print("\n  --- по группам ---")
    for g in groups:
        gid = g["Id"]
        ks = kw_by_group.get(gid, [])
        ats = at_by_group.get(gid, [])
        at_txt = ", ".join(
            (f"ретаргет {a['RetargetingListId']}" if a.get("RetargetingListId")
             else f"интерес {a.get('InterestId')}") + f"[{a.get('State')}]"
            for a in ats) or "нет"
        print(f"\n  {gid} «{gname[gid]}» {g.get('Status')}")
        print(f"     объявления: {dict(kinds_by_group.get(gid, {}))}")
        print(f"     посадочные: {sorted(href_by_group.get(gid, []))}")
        print(f"     фразы ({len(ks)}): {ks[:8]}{' …' if len(ks) > 8 else ''}")
        print(f"     аудитории: {at_txt}")
        print(f"     корректировки: {dict(bm_by_group.get(gid, {}))}")
    print(f"\n  корректировки на кампании: {dict(bm_by_group.get(0, {}))}")


def main() -> None:
    metrika_rights()
    for login, token in clients():
        if token:
            print(f"\naккаунт {login}")
            targeting(login, token)
            return


if __name__ == "__main__":
    main()
