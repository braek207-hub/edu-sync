"""Проба BJORN: чем крутим сейчас и почему видео не показывается.

Два вопроса:
  A. Видео. Какие видеоактивы есть (creatives.get), в каких объявлениях они стоят,
     какой у них статус модерации и State — витрина видит 4 VIDEO-объявления с 6 показами,
     и надо понять, это отклонено/остановлено или просто проигрывает аукцион.
     Отдельно — видеодополнения (VideoExtension) у текстовых объявлений.
  B. Зима. Структура кампаний, групп, фидов и фильтров: где вообще можно управлять долей
     показов зимнего ассортимента (отдельная группа/кампания на зимние товары, фильтры фида).

Read-only: только get-методы Директа. Печатаются структура и статусы, не тексты объявлений.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict

import requests

API = "https://api.direct.yandex.com/json/v5/"


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
    if not e:
        return None
    return f"{e.get('error_string')} | {e.get('error_detail')}"[:300]


def logins(token: str) -> list[str]:
    raw = os.environ.get("DIRECT_CLIENTS_JSON", "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                out = []
                for item in data:
                    if isinstance(item, str):
                        out.append(item)
                    elif isinstance(item, dict):
                        v = item.get("login") or item.get("Login") or item.get("client_login")
                        if v:
                            out.append(v)
                if out:
                    return out
            if isinstance(data, dict):
                return list(data.keys())
        except Exception as exc:  # noqa: BLE001
            print(f"  DIRECT_CLIENTS_JSON не разобран: {exc}")
    single = os.environ.get("DIRECT_LOGIN", "").strip()
    return [single] if single else [""]


def dump(title: str, rows: list[dict], keys: list[str], limit: int = 200) -> None:
    print(f"\n--- {title} ({len(rows)}) ---")
    for r in rows[:limit]:
        print("  " + " | ".join(f"{k}={r.get(k)}" for k in keys))
    if len(rows) > limit:
        print(f"  ... ещё {len(rows) - limit}")


def probe_account(login: str, token: str) -> None:
    print("\n" + "=" * 78)
    print(f"АККАУНТ: {login or '(основной)'}")
    print("=" * 78)

    # ── A. Кампании ───────────────────────────────────────────────────────────
    camps = call("campaigns", {
        "SelectionCriteria": {"States": ["ON", "OFF", "SUSPENDED"]},
        "FieldNames": ["Id", "Name", "Type", "Status", "State", "StatusPayment", "DailyBudget"],
        "TextCampaignFieldNames": ["BiddingStrategy"],
        "SmartCampaignFieldNames": ["BiddingStrategy"],
        "DynamicTextCampaignFieldNames": ["BiddingStrategy"],
        "UnifiedCampaignFieldNames": ["BiddingStrategy"],
        "Page": {"Limit": 200},
    }, login, token)
    e = err(camps)
    if e:
        print(f"campaigns.get ОШИБКА: {e}")
        return
    campaigns = camps.get("result", {}).get("Campaigns", [])
    by_id = {c["Id"]: c for c in campaigns}
    print(f"\n--- Кампании ({len(campaigns)}) ---")
    for c in campaigns:
        strat = None
        for block in ("TextCampaign", "SmartCampaign", "DynamicTextCampaign", "UnifiedCampaign"):
            if block in c and isinstance(c[block], dict):
                bs = c[block].get("BiddingStrategy") or {}
                search = (bs.get("Search") or {}).get("BiddingStrategyType")
                net = (bs.get("Network") or {}).get("BiddingStrategyType")
                extra = {}
                for side in ("Search", "Network"):
                    side_obj = bs.get(side) or {}
                    for k, v in side_obj.items():
                        if isinstance(v, dict):
                            extra[f"{side}.{k}"] = {kk: vv for kk, vv in v.items()
                                                    if kk in ("WeeklySpendLimit", "AverageCpa",
                                                              "RoiCoef", "GoalId", "BidCeiling",
                                                              "AverageCpc", "AverageCrr")}
                strat = f"search={search} net={net} {extra if extra else ''}"
                break
        print(f"  {c['Id']} | {c.get('Type')} | {c.get('State')}/{c.get('Status')} | "
              f"budget={c.get('DailyBudget')} | {c.get('Name')}")
        if strat:
            print(f"      стратегия: {strat}")

    active_ids = [c["Id"] for c in campaigns if c.get("State") in ("ON", "SUSPENDED")]
    if not active_ids:
        print("нет активных кампаний")
        return

    # ── B. Группы ─────────────────────────────────────────────────────────────
    groups = call("adgroups", {
        "SelectionCriteria": {"CampaignIds": active_ids},
        "FieldNames": ["Id", "Name", "CampaignId", "Type", "Subtype", "Status"],
        "Page": {"Limit": 1000},
    }, login, token)
    e = err(groups)
    if e:
        print(f"adgroups.get ОШИБКА: {e}")
        groups_list = []
    else:
        groups_list = groups.get("result", {}).get("AdGroups", [])
    per_camp = defaultdict(list)
    for g in groups_list:
        per_camp[g["CampaignId"]].append(g)
    print(f"\n--- Группы по кампаниям ({len(groups_list)}) ---")
    for cid, gs in sorted(per_camp.items(), key=lambda kv: -len(kv[1])):
        types = Counter(f"{g.get('Type')}/{g.get('Subtype')}" for g in gs)
        print(f"  {cid} {by_id.get(cid, {}).get('Name')} → {len(gs)} групп: {dict(types)}")
        for g in gs[:12]:
            print(f"      {g['Id']} | {g.get('Status')} | {g.get('Name')}")
        if len(gs) > 12:
            print(f"      ... ещё {len(gs) - 12}")

    # ── C. Объявления: типы, статусы, видео ───────────────────────────────────
    ads = call("ads", {
        "SelectionCriteria": {"CampaignIds": active_ids},
        "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "Subtype", "State", "Status",
                       "StatusClarification"],
        "TextAdFieldNames": ["AdImageHash", "VideoExtension"],
        "TextAdBuilderAdFieldNames": ["Creative"],
        "CpcVideoAdBuilderAdFieldNames": ["Creative", "Href"],
        "SmartAdBuilderAdFieldNames": ["Creative"],
        "TextImageAdFieldNames": ["AdImageHash"],
        "Page": {"Limit": 2000},
    }, login, token)
    e = err(ads)
    ads_list = [] if e else ads.get("result", {}).get("Ads", [])
    if e:
        print(f"\nads.get ОШИБКА (полный набор блоков): {e}")
        ads = call("ads", {
            "SelectionCriteria": {"CampaignIds": active_ids},
            "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "Subtype", "State", "Status",
                           "StatusClarification"],
            "Page": {"Limit": 2000},
        }, login, token)
        e2 = err(ads)
        if e2:
            print(f"ads.get ОШИБКА (минимум): {e2}")
        else:
            ads_list = ads.get("result", {}).get("Ads", [])

    print(f"\n--- Объявления: тип × статус ({len(ads_list)}) ---")
    cnt = Counter(f"{a.get('Type')}/{a.get('Subtype')} · {a.get('State')}/{a.get('Status')}"
                  for a in ads_list)
    for k, v in cnt.most_common():
        print(f"  {v:5} × {k}")

    print("\n--- Объявления с ВИДЕО (CPC_VIDEO / видеодополнение) ---")
    video_rows = []
    for a in ads_list:
        has_video_ext = bool((a.get("TextAd") or {}).get("VideoExtension"))
        is_cpc_video = "CpcVideoAdBuilderAd" in a or a.get("Subtype") == "CPC_VIDEO_AD_BUILDER_AD"
        if has_video_ext or is_cpc_video:
            video_rows.append({
                "Id": a["Id"], "CampaignId": a.get("CampaignId"), "AdGroupId": a.get("AdGroupId"),
                "kind": "video_extension" if has_video_ext else "cpc_video",
                "State": a.get("State"), "Status": a.get("Status"),
                "why": (a.get("StatusClarification") or "")[:120],
            })
    if video_rows:
        dump("видеообъявления/видеодополнения", video_rows,
             ["Id", "CampaignId", "AdGroupId", "kind", "State", "Status", "why"])
    else:
        print("  нет ни одного")

    rejected = [a for a in ads_list if a.get("Status") == "REJECTED"]
    if rejected:
        dump("ОТКЛОНЁННЫЕ модерацией", [{
            "Id": a["Id"], "CampaignId": a.get("CampaignId"), "Type": a.get("Type"),
            "why": (a.get("StatusClarification") or "")[:160]} for a in rejected],
            ["Id", "CampaignId", "Type", "why"], limit=40)

    # ── D. Креативы аккаунта ──────────────────────────────────────────────────
    cre = call("creatives", {
        "SelectionCriteria": {},
        "FieldNames": ["Id", "Type", "Name", "PreviewUrl"],
        "Page": {"Limit": 500},
    }, login, token)
    e = err(cre)
    if e:
        print(f"\ncreatives.get ОШИБКА: {e}")
    else:
        cl = cre.get("result", {}).get("Creatives", [])
        print(f"\n--- Креативы в аккаунте ({len(cl)}) ---")
        for k, v in Counter(c.get("Type") for c in cl).most_common():
            print(f"  {v:5} × {k}")
        vids = [c for c in cl if "VIDEO" in str(c.get("Type", "")).upper()]
        dump("видеокреативы", vids, ["Id", "Type", "Name"], limit=40)

    # ── E. Фиды ───────────────────────────────────────────────────────────────
    feeds = call("feeds", {
        "FieldNames": ["Id", "Name", "BusinessType", "SourceType", "UpdateStatus",
                       "FilenameSource", "UrlSource"],
        "Page": {"Limit": 100},
    }, login, token)
    e = err(feeds)
    if e:
        print(f"\nfeeds.get ОШИБКА: {e}")
    else:
        fl = feeds.get("result", {}).get("Feeds", [])
        print(f"\n--- Фиды ({len(fl)}) ---")
        for f in fl:
            src = (f.get("UrlSource") or {}).get("Url") or (f.get("FilenameSource") or {}).get("Filename")
            print(f"  {f['Id']} | {f.get('BusinessType')} | {f.get('SourceType')} | "
                  f"{f.get('UpdateStatus')} | {f.get('Name')} | {str(src)[:90]}")

    # ── F. Фильтры смарт-групп (где режется ассортимент) ─────────────────────
    smart_group_ids = [g["Id"] for g in groups_list
                       if str(g.get("Type")) in ("SMART_ADGROUP", "DYNAMIC_FEED_ADGROUP",
                                                 "DYNAMIC_TEXT_FEED_ADGROUP", "CPM_BANNER_ADGROUP")]
    if smart_group_ids:
        for service, field in (("smartadtargets", "SmartAdTargets"),
                               ("dynamictextadtargets", "Webpages")):
            res = call(service, {
                "SelectionCriteria": {"AdGroupIds": smart_group_ids[:100]},
                "FieldNames": ["Id", "AdGroupId", "CampaignId", "Name", "State"],
                "Page": {"Limit": 500},
            }, login, token)
            e = err(res)
            if e:
                print(f"\n{service}.get ОШИБКА: {e}")
                continue
            rows = res.get("result", {}).get(field, [])
            dump(f"фильтры {service}", rows, ["Id", "AdGroupId", "CampaignId", "State", "Name"],
                 limit=60)
    else:
        print("\nсмарт/динамических групп не найдено — фильтры не запрашивались")


def main() -> None:
    token = os.environ["DIRECT_TOKEN"]
    for login in logins(token):
        try:
            probe_account(login, token)
        except Exception as exc:  # noqa: BLE001
            print(f"аккаунт {login}: исключение {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
