"""Что есть в кабинете BJORN для мини-кампании РСЯ на ретаргет.

Собирает перед сборкой:
  1. Цели и сегменты счётчика Метрики — из чего строить условие «был 7 дней, не отказ, не купил».
  2. Существующие условия ретаргетинга Директа и их правила.
  3. Расширения для копирования: наборы быстрых ссылок, уточнения, картинки, визитка.
  4. Настройки действующей РСЯ-кампании как образец: регионы, минус-фразы, счётчик, цели.

Read-only.
"""

from __future__ import annotations

import json
import os

import requests

API = "https://api.direct.yandex.com/json/v5/"
METRIKA = "https://api-metrika.yandex.net/management/v1"
SAMPLE_CAMPAIGNS = [713525080, 713525055, 714000003]


def call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    resp = requests.post(
        API + service,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Client-Login": login,
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=120,
    )
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
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    login = str(item.get("login") or item.get("client_login") or "").strip()
                    token = str(item.get("token") or "").strip() or default_token
                    if login:
                        out.append((login, token))
                elif isinstance(item, str):
                    out.append((item.strip(), default_token))
    return out


def metrika() -> None:
    token = os.environ.get("METRICA_TOKEN", "").strip()
    counter = os.environ.get("METRICA_COUNTER_ID", "").strip()
    print("#" * 70)
    print(f"# 1. МЕТРИКА, счётчик {counter}")
    print("#" * 70)
    if not token or not counter:
        print("нет METRICA_TOKEN/METRICA_COUNTER_ID")
        return
    h = {"Authorization": f"OAuth {token}"}

    r = requests.get(f"{METRIKA}/counter/{counter}/goals", headers=h,
                     params={"useDeleted": "false"}, timeout=60)
    if r.status_code != 200:
        print(f"goals: HTTP {r.status_code} {r.text[:200]}")
    else:
        goals = r.json().get("goals", [])
        print(f"\n--- Цели ({len(goals)}) ---")
        for g in goals:
            print(f"  {g['id']} | {g.get('type')} | {g.get('name')}")

    r = requests.get(f"{METRIKA}/counter/{counter}/segments", headers=h, timeout=60)
    if r.status_code != 200:
        print(f"\nsegments: HTTP {r.status_code} {r.text[:300]}")
    else:
        segs = r.json().get("segments", [])
        print(f"\n--- Сегменты ({len(segs)}) ---")
        for s in segs:
            print(f"  {s.get('segment_id')} | {s.get('name')} | {str(s.get('expression'))[:200]}")


def direct(login: str, token: str) -> None:
    print("\n" + "#" * 70)
    print(f"# 2. ДИРЕКТ, {login}")
    print("#" * 70)

    rl = call("retargetinglists", {
        "SelectionCriteria": {},
        "FieldNames": ["Id", "Name", "Type", "Scope", "IsAvailable", "Rules"],
        "Page": {"Limit": 200},
    }, login, token)
    e = err(rl)
    if e:
        print(f"retargetinglists.get: {e}")
    else:
        lists = rl.get("result", {}).get("RetargetingLists", [])
        print(f"\n--- Условия ретаргетинга ({len(lists)}) ---")
        for l in lists:
            print(f"  {l['Id']} | {l.get('Type')} | {l.get('Scope')} | "
                  f"доступно={l.get('IsAvailable')} | {l.get('Name')}")
            print(f"      {json.dumps(l.get('Rules'), ensure_ascii=False)[:400]}")

    sl = call("sitelinks", {
        "SelectionCriteria": {},
        "FieldNames": ["Id"],
        "SitelinkFieldNames": ["Title", "Href", "Description", "TurboPageId"],
        "Page": {"Limit": 100},
    }, login, token)
    e = err(sl)
    if e:
        print(f"\nsitelinks.get: {e}")
    else:
        sets = sl.get("result", {}).get("SitelinksSets", [])
        print(f"\n--- Наборы быстрых ссылок ({len(sets)}) ---")
        for s in sets[:12]:
            print(f"  набор {s['Id']}:")
            for link in s.get("Sitelinks", []):
                print(f"      «{link.get('Title')}» → {link.get('Href')} | "
                      f"{link.get('Description')}")

    ae = call("adextensions", {
        "SelectionCriteria": {"Types": ["CALLOUT"]},
        "FieldNames": ["Id", "Type", "Status", "State"],
        "CalloutFieldNames": ["CalloutText"],
        "Page": {"Limit": 200},
    }, login, token)
    e = err(ae)
    if e:
        print(f"\nadextensions.get: {e}")
    else:
        exts = ae.get("result", {}).get("AdExtensions", [])
        print(f"\n--- Уточнения ({len(exts)}) ---")
        for x in exts:
            print(f"  {x['Id']} | {x.get('State')}/{x.get('Status')} | "
                  f"{(x.get('Callout') or {}).get('CalloutText')}")

    ads = call("ads", {
        "SelectionCriteria": {"CampaignIds": SAMPLE_CAMPAIGNS, "States": ["ON"],
                              "Types": ["TEXT_AD"]},
        "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type"],
        "TextAdFieldNames": ["Title", "Title2", "Text", "Href", "DisplayUrlPath",
                             "AdImageHash", "SitelinkSetId", "VCardId", "AdExtensionIds",
                             "DisplayUrlPathModeration", "VideoExtension", "TurboPageId"],
        "Page": {"Limit": 100},
    }, login, token)
    e = err(ads)
    if e:
        print(f"\nads.get образцов: {e}")
    else:
        al = ads.get("result", {}).get("Ads", [])
        print(f"\n--- Образцы ТГО из действующих кампаний ({len(al)}) ---")
        for a in al[:8]:
            print(f"  {json.dumps(a, ensure_ascii=False)[:800]}")

    gr = call("adgroups", {
        "SelectionCriteria": {"CampaignIds": SAMPLE_CAMPAIGNS},
        "FieldNames": ["Id", "Name", "CampaignId", "RegionIds", "NegativeKeywords",
                       "TrackingParams"],
        "Page": {"Limit": 100},
    }, login, token)
    e = err(gr)
    if e:
        print(f"\nadgroups.get: {e}")
    else:
        gl = gr.get("result", {}).get("AdGroups", [])
        print(f"\n--- Группы-образцы: регионы и минус-фразы ---")
        for g in gl[:6]:
            print(f"  {g['CampaignId']}/{g['Id']} | регионы={g.get('RegionIds')} | "
                  f"минус={(g.get('NegativeKeywords') or {}).get('Items')} | {g.get('Name')}")

    cm = call("campaigns", {
        "SelectionCriteria": {"Ids": SAMPLE_CAMPAIGNS},
        "FieldNames": ["Id", "Name", "TimeZone", "NegativeKeywords", "ExcludedSites",
                       "DailyBudget"],
        "TextCampaignFieldNames": ["CounterIds", "PriorityGoals", "Settings",
                                   "RelevantKeywords", "AttributionModel"],
        "Page": {"Limit": 10},
    }, login, token)
    e = err(cm)
    if e:
        print(f"\ncampaigns.get настроек: {e}")
    else:
        for c in cm.get("result", {}).get("Campaigns", []):
            print(f"\n--- Настройки кампании {c['Id']} «{c.get('Name')}» ---")
            print(json.dumps(c, ensure_ascii=False, indent=1)[:2500])

    vc = call("vcards", {
        "SelectionCriteria": {},
        "FieldNames": ["Id", "CampaignId", "Phone", "CompanyName", "Country", "City"],
        "Page": {"Limit": 50},
    }, login, token)
    e = err(vc)
    if e:
        print(f"\nvcards.get: {e}")
    else:
        vl = vc.get("result", {}).get("VCards", [])
        print(f"\n--- Визитки ({len(vl)}) ---")
        for v in vl[:5]:
            print(f"  {v['Id']} | камп={v.get('CampaignId')} | {v.get('CompanyName')} | "
                  f"{v.get('City')}")

    im = call("adimages", {
        "SelectionCriteria": {"AssociatedOnly": "YES"},
        "FieldNames": ["AdImageHash", "Name", "Type", "Subtype", "Associated"],
        "Page": {"Limit": 50},
    }, login, token)
    e = err(im)
    if e:
        print(f"\nadimages.get: {e}")
    else:
        il = im.get("result", {}).get("AdImages", [])
        print(f"\n--- Картинки, привязанные к объявлениям ({len(il)}) ---")
        for i in il[:15]:
            print(f"  {i.get('AdImageHash')} | {i.get('Type')}/{i.get('Subtype')} | {i.get('Name')}")


def main() -> None:
    metrika()
    for login, token in clients():
        if not token:
            continue
        direct(login, token)
        return


if __name__ == "__main__":
    main()
