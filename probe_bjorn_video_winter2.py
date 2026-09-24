"""Проба BJORN №2: корректировки ставок, состав групп и схема фильтров фида.

Отвечает на три вопроса перед правками:
  1. Есть ли в API корректировка на видеодополнения и какие корректировки уже стоят.
  2. Как устроены группы активных кампаний: где ТГО соседствует с фидовыми объявлениями
     (SHOPPING_AD/LISTING_AD) и у каких ТГО уже есть видеодополнение.
  3. По каким полям фида можно отфильтровать зимний ассортимент (FilterSchema).

Read-only.
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
    return f"{e.get('error_string')} | {e.get('error_detail')}"[:400]


def clients() -> list[tuple[str, str]]:
    default_token = os.environ.get("DIRECT_TOKEN", "").strip()
    raw = os.environ.get("DIRECT_CLIENTS_JSON", "").strip()
    out: list[tuple[str, str]] = []
    if raw:
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str):
                    out.append((item.strip(), default_token))
                elif isinstance(item, dict):
                    login = str(item.get("login") or item.get("client_login") or "").strip()
                    token = str(item.get("token") or "").strip() or default_token
                    if login:
                        out.append((login, token))
    if not out:
        out.append((os.environ.get("DIRECT_CLIENT_LOGIN", "").strip(), default_token))
    return out


def probe(login: str, token: str) -> None:
    print("=" * 78)
    print(f"АККАУНТ: {login}")
    print("=" * 78)

    camps = call("campaigns", {
        "SelectionCriteria": {"States": ["ON", "SUSPENDED"]},
        "FieldNames": ["Id", "Name", "Type", "State"],
        "Page": {"Limit": 200},
    }, login, token)
    if err(camps):
        print(f"campaigns.get ОШИБКА: {err(camps)}")
        return
    campaigns = camps.get("result", {}).get("Campaigns", [])
    names = {c["Id"]: c.get("Name") for c in campaigns}
    ids = list(names)
    print(f"Включённые кампании: {[(i, names[i]) for i in ids]}")

    # ── 1. Корректировки ставок ───────────────────────────────────────────────
    print("\n" + "#" * 70)
    print("# 1. КОРРЕКТИРОВКИ СТАВОК")
    print("#" * 70)

    base = call("bidmodifiers", {
        "SelectionCriteria": {"CampaignIds": ids, "Levels": ["CAMPAIGN", "AD_GROUP"]},
        "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "Level"],
        "Page": {"Limit": 1000},
    }, login, token)
    e = err(base)
    if e:
        print(f"bidmodifiers.get (базовый) ОШИБКА: {e}")
    else:
        mods = base.get("result", {}).get("BidModifiers", [])
        print(f"\n--- Уже настроенные корректировки ({len(mods)}) ---")
        for k, v in Counter(f"{m.get('Type')} · {m.get('Level')}" for m in mods).most_common():
            print(f"  {v:4} × {k}")
        per_camp = defaultdict(Counter)
        for m in mods:
            per_camp[m.get("CampaignId")][m.get("Type")] += 1
        for cid, cnt in per_camp.items():
            print(f"  {cid} {names.get(cid)}: {dict(cnt)}")

    # Какой блок отвечает за видео — спрашиваем API, а не угадываем.
    for block in ("VideoBidModifierFieldNames", "VideoAdjustmentFieldNames",
                  "VideoBidAdjustmentFieldNames"):
        r = call("bidmodifiers", {
            "SelectionCriteria": {"CampaignIds": ids, "Levels": ["CAMPAIGN", "AD_GROUP"]},
            "FieldNames": ["Id", "CampaignId", "Type", "Level"],
            block: ["BidModifier"],
            "Page": {"Limit": 100},
        }, login, token)
        e = err(r)
        print(f"\nблок {block}: {'OK' if not e else e}")
        if not e:
            rows = r.get("result", {}).get("BidModifiers", [])
            for m in rows:
                if "Video" in json.dumps(m):
                    print(f"  {m}")
            for t in ("VIDEO_ADJUSTMENT", "AD_GROUP_ADJUSTMENT"):
                rr = call("bidmodifiers", {
                    "SelectionCriteria": {"CampaignIds": ids,
                                          "Levels": ["CAMPAIGN", "AD_GROUP"],
                                          "Types": [t]},
                    "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "Level"],
                    "AdGroupAdjustmentFieldNames": ["AdGroupAdjustment"],
                    "Page": {"Limit": 200},
                }, login, token)
                ee = err(rr)
                got = [] if ee else rr.get("result", {}).get("BidModifiers", [])
                print(f"  тип {t}: {ee if ee else str(len(got)) + ' шт'}")
                for m in got[:20]:
                    print(f"     {m}")
            break

    # Что вообще принимает bidmodifiers.add — вытаскиваем список типов из ошибки.
    # ── 2. Состав групп активных кампаний ─────────────────────────────────────
    print("\n" + "#" * 70)
    print("# 2. СОСТАВ ГРУПП: ТГО рядом с фидовыми объявлениями")
    print("#" * 70)

    groups = call("adgroups", {
        "SelectionCriteria": {"CampaignIds": ids},
        "FieldNames": ["Id", "Name", "CampaignId", "Type", "Subtype", "Status"],
        "Page": {"Limit": 1000},
    }, login, token)
    gl = [] if err(groups) else groups.get("result", {}).get("AdGroups", [])
    gname = {g["Id"]: g.get("Name") for g in gl}

    ads = call("ads", {
        "SelectionCriteria": {"CampaignIds": ids, "States": ["ON", "OFF"]},
        "FieldNames": ["Id", "CampaignId", "AdGroupId", "Type", "Subtype", "State", "Status"],
        "TextAdFieldNames": ["VideoExtension"],
        "Page": {"Limit": 3000},
    }, login, token)
    e = err(ads)
    if e:
        print(f"ads.get ОШИБКА: {e}")
        return
    al = ads.get("result", {}).get("Ads", [])

    rows = defaultdict(lambda: Counter())
    for a in al:
        g = a["AdGroupId"]
        rows[g][a.get("Type")] += 1
        if a.get("Type") == "TEXT_AD":
            rows[g]["TEXT_AD_ON" if a.get("State") == "ON" else "TEXT_AD_OFF"] += 1
            if (a.get("TextAd") or {}).get("VideoExtension"):
                rows[g]["с_видео"] += 1

    print("\n--- Группа: сколько каких объявлений, сколько ТГО без видео ---")
    for cid in ids:
        gs = [g for g in gl if g["CampaignId"] == cid]
        print(f"\n### {cid} {names.get(cid)} — {len(gs)} групп")
        need_total = 0
        for g in gs:
            c = rows[g["Id"]]
            tgo_on = c.get("TEXT_AD_ON", 0)
            with_video = c.get("с_видео", 0)
            need = max(tgo_on - with_video, 0)
            need_total += need
            kinds = ", ".join(f"{k}={v}" for k, v in c.items()
                              if k in ("TEXT_AD", "SHOPPING_AD", "LISTING_AD", "IMAGE_AD"))
            print(f"  {g['Id']} | {kinds} | ТГО вкл={tgo_on} с видео={with_video} "
                  f"→ добавить={need} | {g.get('Name')}")
        print(f"  ИТОГО по кампании: добавить видео в {need_total} объявлений")

    # ── 3. Фиды: по чему можно отфильтровать зиму ────────────────────────────
    print("\n" + "#" * 70)
    print("# 3. ФИДЫ: схема фильтров")
    print("#" * 70)

    feeds = call("feeds", {
        "FieldNames": ["Id", "Name", "BusinessType", "SourceType", "Status",
                       "NumberOfItems", "NumberOfListings", "CampaignIds", "FilterSchema",
                       "Fields", "TitleAndTextSources"],
        "Page": {"Limit": 100},
    }, login, token)
    e = err(feeds)
    if e:
        print(f"feeds.get ОШИБКА: {e}")
        return
    fl = feeds.get("result", {}).get("Feeds", [])
    live = [f for f in fl if f.get("CampaignIds") or f.get("Status") == "DONE"]
    for f in live:
        if not f.get("CampaignIds") and f.get("Id") not in (1504478, 2532503, 1539648, 1646756):
            continue
        print(f"\n--- фид {f['Id']} «{f.get('Name')}» | {f.get('Status')} | "
              f"items={f.get('NumberOfItems')} listings={f.get('NumberOfListings')} | "
              f"камп={f.get('CampaignIds')}")
        print(f"    Fields: {f.get('Fields')}")
        fs = f.get("FilterSchema")
        if fs:
            print(f"    FilterSchema: {json.dumps(fs, ensure_ascii=False)[:1500]}")


def main() -> None:
    for login, token in clients():
        if not token:
            print(f"{login}: нет токена — пропуск")
            continue
        try:
            probe(login, token)
        except Exception as exc:  # noqa: BLE001
            print(f"{login}: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
