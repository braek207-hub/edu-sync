"""Ручной персонализированный офферный ретаргетинг BJORN: клон 714000003 на сегментах по модели.

Замысел: та же проработанная структура (33 группы, по куртке на группу, свои быстрые ссылки,
уточнения и картинка), но вместо фраз, автотаргетинга и «Интересов и привычек» — одно условие
на группу: «смотрел страницы этой модели за последние N дней, визит не отказ, не покупал 30 дней».

Почему сегмент Метрики, а не цель: цель начинает копить аудиторию только с момента создания,
а сегмент ретроспективен — люди в нём есть сразу. Срок в правилах Директа применяется только
к целям (у сегментов всегда 540 дней), поэтому окно закладывается в само выражение сегмента
через ym:s:date, а скользит оно кроном slide_bjorn_rt_segments.py.

Окно 10 дней у всех моделей, кроме пяти слабых по охвату (Котлин 81, Монферрана 88, Аскер 98,
Кареджи 58, Котлас 21 человек за 10 дней) — им 30 дней, иначе сегмент не дотянет до порога
показа в 100 пользователей.

Порядок этапов важен: сначала Метрика, потом условия, потом кампания. Всё создаётся
идемпотентно — повторный прогон переиспользует найденное по имени, а не плодит копии.

APPLY=1 — писать. Без него только план.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta

import requests

API = "https://api.direct.yandex.com/json/v5/"
METRIKA = "https://api-metrika.yandex.net/management/v1"

SOURCE_CAMPAIGN = 714000003
CAMPAIGN_NAME = "РСЯ. Ретаргет по модели. Смотрел куртку"
GOAL_PURCHASE = 341172800          # Ecommerce: покупка
PURCHASE_DAYS = 30
WEEKLY_LIMIT_RUB = 3000
BID_CEILING_RUB = 40
SEGMENT_LIFESPAN = 540             # для сегментов Директ срок игнорирует, шлём максимум
EXPECTED_LOGIN_PART = "bjorn"

# slug в URL карточки -> (имя модели, окно в днях)
MODELS: dict[str, tuple[str, int]] = {
    "narvik": ("Нарвик", 10),
    "stavanger": ("Ставангер", 10),
    "troms": ("Тромсо", 10),
    "ruskeala": ("Рускеала", 10),
    "kotlin": ("Котлин", 30),
    "tuutari": ("Туутари", 10),
    "drammen": ("Драммен", 10),
    "skien": ("Скиен", 10),
    "vuoksa": ("Вуокса", 10),
    "monferrana": ("Монферрана", 30),
    "igora": ("Игора", 10),
    "kareliya": ("Карелия", 10),
    "hamar": ("Хамар", 10),
    "kronshtadt": ("Кронштадт", 10),
    "tonsberg": ("Тонсберг", 10),
    "-alta-": ("Альта", 10),
    "monrepo": ("Монрепо", 10),
    "olanga": ("Оланга", 10),
    "asker": ("Аскер", 30),
    "larvik": ("Ларвик", 10),
    "karedzhi": ("Кареджи", 30),
    "lillestrom": ("Лиллестром", 10),
    "akkala": ("Аккала", 10),
    "komarovo": ("Комарово", 10),
    "repino": ("Репино", 10),
    "mielisi": ("Миэлиси", 10),
    "okhta": ("Охта", 10),
    "vyborg": ("Выборг", 10),
    "kotlas": ("Котлас", 30),
    "ladoga": ("Ладога", 10),
}

CREATED: list[str] = []


def fail(msg: str) -> None:
    print(f"\n!!! СТОП: {msg}")
    if CREATED:
        print("\nуспело создаться (чистить вручную при необходимости):")
        for line in CREATED:
            print(f"  {line}")
    sys.exit(1)


def call(service: str, params: dict, login: str, token: str, method: str = "get") -> dict:
    body = json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    resp = requests.post(API + service, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }, timeout=180)
    try:
        return resp.json()
    except Exception:
        return {"error": {"error_string": f"HTTP {resp.status_code}",
                          "error_detail": resp.text[:300]}}


def need(body: dict, what: str) -> dict:
    e = body.get("error")
    if e:
        fail(f"{what}: {e.get('error_string')} | {e.get('error_detail')}")
    return body.get("result", {})


def warnings(result: dict, what: str) -> None:
    for item in result.get("AddResults") or result.get("UpdateResults") or []:
        for w in item.get("Warnings", []):
            print(f"     ! {what} предупреждение {w.get('Code')}: {w.get('Message')} "
                  f"{w.get('Details') or ''}")
        for er in item.get("Errors", []):
            fail(f"{what}: {er.get('Code')} {er.get('Message')} {er.get('Details') or ''}")


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


# ---------------------------------------------------------------- чтение источника

def read_source(login: str, token: str) -> dict:
    print(f"### ЧИТАЮ ИСТОЧНИК {SOURCE_CAMPAIGN}")
    camp = need(call("campaigns", {
        "SelectionCriteria": {"Ids": [SOURCE_CAMPAIGN]},
        "FieldNames": ["Id", "Name", "TimeZone", "ExcludedSites", "TimeTargeting"],
        "TextCampaignFieldNames": ["CounterIds", "TrackingParams"],
        "Page": {"Limit": 1},
    }, login, token), "campaigns.get")["Campaigns"][0]

    groups = need(call("adgroups", {
        "SelectionCriteria": {"CampaignIds": [SOURCE_CAMPAIGN]},
        "FieldNames": ["Id", "Name", "RegionIds"],
        "Page": {"Limit": 500},
    }, login, token), "adgroups.get")["AdGroups"]

    ads = need(call("ads", {
        "SelectionCriteria": {"CampaignIds": [SOURCE_CAMPAIGN], "Types": ["TEXT_AD"]},
        "FieldNames": ["Id", "AdGroupId", "Type"],
        "TextAdFieldNames": ["Title", "Title2", "Text", "Href", "Mobile", "DisplayUrlPath",
                             "AdImageHash", "SitelinkSetId", "VCardId", "AdExtensions",
                             "VideoExtension", "TurboPageId", "BusinessId",
                             "PreferVCardOverBusiness"],
        "Page": {"Limit": 500},
    }, login, token), "ads.get")

    mods = need(call("bidmodifiers", {
        "SelectionCriteria": {"CampaignIds": [SOURCE_CAMPAIGN], "Levels": ["AD_GROUP"]},
        "FieldNames": ["Id", "AdGroupId", "Level", "Type"],
        "WeatherAdjustmentFieldNames": ["Temperature", "Precipitation", "CloudCover",
                                        "BidModifier", "Enabled"],
        "DemographicsAdjustmentFieldNames": ["Gender", "Age", "BidModifier"],
        "Page": {"Limit": 1000},
    }, login, token), "bidmodifiers.get")

    ad_by_group = {a["AdGroupId"]: a for a in ads.get("Ads", [])}
    mods_by_group: dict[int, list[dict]] = {}
    for m in mods.get("BidModifiers", []):
        mods_by_group.setdefault(m["AdGroupId"], []).append(m)

    print(f"  кампания «{camp['Name']}» | групп {len(groups)} | ТГО {len(ad_by_group)} | "
          f"минус-площадок {len((camp.get('ExcludedSites') or {}).get('Items') or [])} | "
          f"корректировок на группах {len(mods.get('BidModifiers', []))}")

    plan = []
    for g in groups:
        ad = ad_by_group.get(g["Id"])
        if not ad:
            fail(f"в группе {g['Id']} «{g['Name']}» нет ТГО — клон вышел бы неполным")
        href = (ad.get("TextAd") or {}).get("Href") or ""
        slug = next((s for s in MODELS if s in href), None)
        if not slug:
            fail(f"не опознана модель по ссылке группы {g['Id']}: {href}")
        plan.append({"group": g, "ad": ad, "slug": slug, "mods": mods_by_group.get(g["Id"], [])})

    print("\n  группа -> модель:")
    for p in plan:
        name, window = MODELS[p["slug"]]
        print(f"    {p['group']['Id']} «{p['group']['Name']}» -> {name} ({p['slug']}, {window}д)")
    return {"campaign": camp, "plan": plan}


# ---------------------------------------------------------------- этап 1: сегменты

def ensure_segments(apply: bool) -> dict[str, int]:
    token = os.environ["METRICA_TOKEN"].strip()
    counter = os.environ["METRICA_COUNTER_ID"].strip()
    h = {"Authorization": f"OAuth {token}"}
    print("\n### ЭТАП 1 — СЕГМЕНТЫ МЕТРИКИ")

    r = requests.get(f"{METRIKA}/counter/{counter}/segments", headers=h, timeout=60)
    if r.status_code != 200:
        fail(f"чтение сегментов: HTTP {r.status_code} {r.text[:200]}")
    existing = {s.get("name"): s.get("segment_id") for s in r.json().get("segments", [])}

    out: dict[str, int] = {}
    for slug, (name, window) in MODELS.items():
        since = (date.today() - timedelta(days=window)).isoformat()
        seg_name = f"RT: {name} · {window}д · не отказ"
        expr = f"ym:pv:URL=@'{slug}' AND ym:s:bounce=='No' AND ym:s:date>='{since}'"
        if seg_name in existing:
            out[slug] = existing[seg_name]
            print(f"  есть  {seg_name} -> {out[slug]}")
            continue
        if not apply:
            print(f"  план  {seg_name} | {expr}")
            continue
        body = json.dumps({"segment": {"name": seg_name, "expression": expr}},
                          ensure_ascii=False).encode("utf-8")
        resp = requests.post(f"{METRIKA}/counter/{counter}/segments", headers={
            **h, "Content-Type": "application/json; charset=utf-8"}, data=body, timeout=60)
        if resp.status_code == 403:
            fail("нет прав на запись в Метрику (403). Нужен OAuth-токен со scope записи "
                 "в секрете BJORN_METRICA_TOKEN — без него сегменты не создать")
        if resp.status_code not in (200, 201):
            fail(f"создание сегмента «{seg_name}»: HTTP {resp.status_code} {resp.text[:300]}")
        sid = resp.json().get("segment", {}).get("segment_id")
        out[slug] = sid
        CREATED.append(f"сегмент Метрики {sid} «{seg_name}»")
        print(f"  создан {seg_name} -> {sid}")
    return out


# ---------------------------------------------------------------- этап 2: условия

def ensure_conditions(login: str, token: str, segments: dict[str, int],
                      apply: bool) -> dict[str, int]:
    print("\n### ЭТАП 2 — УСЛОВИЯ ПОДБОРА АУДИТОРИИ")
    existing: dict[str, int] = {}
    offset = 0
    while True:
        res = need(call("retargetinglists", {
            "FieldNames": ["Id", "Name", "Type", "Scope"],
            "Page": {"Limit": 500, "Offset": offset},
        }, login, token), "retargetinglists.get")
        rows = res.get("RetargetingLists", [])
        for l in rows:
            existing[l.get("Name")] = l["Id"]
        lim = res.get("LimitedBy")
        if not lim or not rows:
            break
        offset = lim

    out: dict[str, int] = {}
    for slug, (name, window) in MODELS.items():
        cond_name = f"RT: смотрел {name} {window}д, без покупки"
        if cond_name in existing:
            out[slug] = existing[cond_name]
            print(f"  есть  {cond_name} -> {out[slug]}")
            continue
        seg_id = segments.get(slug)
        if not seg_id:
            if not apply:
                print(f"  план  {cond_name} (сегмент ещё не создан)")
                continue
            fail(f"нет сегмента под модель {name}")
        payload = {
            "Name": cond_name,
            # Scope в add не передаётся — API считает его неизвестным параметром; на чтении
            # поле есть и выставляется само по составу правил
            "Type": "RETARGETING",
            "Rules": [
                {"Operator": "ALL", "Arguments": [
                    {"MembershipLifeSpan": SEGMENT_LIFESPAN, "ExternalId": seg_id}]},
                {"Operator": "NONE", "Arguments": [
                    {"MembershipLifeSpan": PURCHASE_DAYS, "ExternalId": GOAL_PURCHASE}]},
            ],
        }
        if not apply:
            print(f"  план  {cond_name}: ALL(сегмент {seg_id}) + NONE(покупка {PURCHASE_DAYS}д)")
            continue
        res = need(call("retargetinglists", {"RetargetingLists": [payload]},
                        login, token, "add"), f"retargetinglists.add «{cond_name}»")
        warnings(res, "условие")
        rid = res["AddResults"][0].get("Id")
        out[slug] = rid
        CREATED.append(f"условие Директа {rid} «{cond_name}»")
        print(f"  создано {cond_name} -> {rid}")
    return out


# ---------------------------------------------------------------- этап 3: кампания

def create_campaign(login: str, token: str, src: dict, apply: bool) -> int | None:
    print("\n### ЭТАП 3 — КАМПАНИЯ")
    found = call("campaigns", {
        "SelectionCriteria": {},
        "FieldNames": ["Id", "Name"],
        "Page": {"Limit": 1000}}, login, token)
    for row in (found.get("result") or {}).get("Campaigns", []):
        if row.get("Name") == CAMPAIGN_NAME:
            print(f"  есть {row['Id']} «{CAMPAIGN_NAME}» — переиспользую")
            return row["Id"]

    camp = src["campaign"]
    tc = camp.get("TextCampaign", {})
    payload = {
        "Name": CAMPAIGN_NAME,
        "StartDate": date.today().isoformat(),
        "TimeZone": camp.get("TimeZone") or "Europe/Moscow",
        "ExcludedSites": camp.get("ExcludedSites") or None,
        # Директ отдаёт TimeTargeting с null в незаполненных полях (HolidaysSchedule),
        # а на add то же null не принимает — чистим перед отправкой
        "TimeTargeting": {k: v for k, v in (camp.get("TimeTargeting") or {}).items()
                          if v is not None} or None,
        "TextCampaign": {
            "BiddingStrategy": {
                "Search": {"BiddingStrategyType": "SERVING_OFF"},
                "Network": {
                    "BiddingStrategyType": "WB_MAXIMUM_CLICKS",
                    "WbMaximumClicks": {
                        "WeeklySpendLimit": WEEKLY_LIMIT_RUB * 1_000_000,
                        "BidCeiling": BID_CEILING_RUB * 1_000_000,
                    },
                },
            },
            "CounterIds": tc.get("CounterIds"),
            "TrackingParams": tc.get("TrackingParams"),
            "Settings": [
                {"Option": "ADD_METRICA_TAG", "Value": "NO"},
                {"Option": "ENABLE_AREA_OF_INTEREST_TARGETING", "Value": "YES"},
                {"Option": "ENABLE_COMPANY_INFO", "Value": "YES"},
                {"Option": "ENABLE_SITE_MONITORING", "Value": "YES"},
            ],
        },
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    print(f"  «{CAMPAIGN_NAME}» | сеть: макс. кликов, {WEEKLY_LIMIT_RUB} ₽/нед, "
          f"потолок {BID_CEILING_RUB} ₽ | поиск выключен")
    print(f"  счётчики {json.dumps(tc.get('CounterIds'), ensure_ascii=False)} | "
          f"разметка {tc.get('TrackingParams')}")
    if not apply:
        return None
    res = need(call("campaigns", {"Campaigns": [payload]}, login, token, "add"), "campaigns.add")
    warnings(res, "кампания")
    cid = res["AddResults"][0].get("Id")
    CREATED.append(f"кампания {cid} «{CAMPAIGN_NAME}»")
    print(f"  создана {cid}")
    return cid


# ------------------------------------------- этапы 4-7: группы, ТГО, таргетинг, корректировки

def build_groups(login: str, token: str, src: dict, cid: int | None,
                 conditions: dict[str, int], apply: bool) -> None:
    print("\n### ЭТАПЫ 4-7 — ГРУППЫ, ОБЪЯВЛЕНИЯ, ТАРГЕТИНГИ, КОРРЕКТИРОВКИ")
    if not apply:
        for p in src["plan"]:
            name, window = MODELS[p["slug"]]
            ta = p["ad"]["TextAd"]
            print(f"  «{p['group']['Name']}» -> условие «смотрел {name} {window}д» | "
                  f"ТГО «{ta.get('Title')}» | картинка {ta.get('AdImageHash')} | "
                  f"ссылки {ta.get('SitelinkSetId')} | уточнений "
                  f"{len(ta.get('AdExtensions') or [])} | корректировок {len(p['mods'])}")
        return

    # Что в кампании уже есть: прогон добивает недостающее, а не плодит дубли
    have_groups: dict[str, int] = {}
    have_ads: dict[int, int] = {}
    have_targets: set[int] = set()
    have_mods: set[int] = set()
    gr = call("adgroups", {"SelectionCriteria": {"CampaignIds": [cid]},
                           "FieldNames": ["Id", "Name"],
                           "Page": {"Limit": 1000}}, login, token)
    for row in (gr.get("result") or {}).get("AdGroups", []):
        have_groups[row["Name"]] = row["Id"]
    gids = list(have_groups.values())
    if gids:
        ar = call("ads", {"SelectionCriteria": {"AdGroupIds": gids},
                          "FieldNames": ["Id", "AdGroupId"],
                          "Page": {"Limit": 1000}}, login, token)
        for row in (ar.get("result") or {}).get("Ads", []):
            have_ads.setdefault(row["AdGroupId"], row["Id"])
        tr = call("audiencetargets", {"SelectionCriteria": {"AdGroupIds": gids},
                                      "FieldNames": ["Id", "AdGroupId"],
                                      "Page": {"Limit": 1000}}, login, token)
        for row in (tr.get("result") or {}).get("AudienceTargets", []):
            have_targets.add(row["AdGroupId"])
        mr = call("bidmodifiers", {"SelectionCriteria": {"AdGroupIds": gids},
                                   "FieldNames": ["Id", "AdGroupId"],
                                   "Page": {"Limit": 1000}}, login, token)
        for row in (mr.get("result") or {}).get("BidModifiers", []):
            have_mods.add(row["AdGroupId"])
    if have_groups:
        print(f"  уже в кампании: групп {len(have_groups)}, объявлений {len(have_ads)}, "
              f"таргетингов {len(have_targets)}, групп с корректировками {len(have_mods)}")

    for p in src["plan"]:
        g = p["group"]
        name, window = MODELS[p["slug"]]
        gid = have_groups.get(g["Name"])
        if not gid:
            gres = need(call("adgroups", {"AdGroups": [{
                "Name": g["Name"],
                "CampaignId": cid,
                "RegionIds": g.get("RegionIds") or [225],
            }]}, login, token, "add"), f"adgroups.add «{g['Name']}»")
            warnings(gres, "группа")
            gid = gres["AddResults"][0].get("Id")
            CREATED.append(f"группа {gid} «{g['Name']}»")

        ta = dict(p["ad"]["TextAd"])
        ext_ids = [e["AdExtensionId"] for e in (ta.pop("AdExtensions", None) or [])]
        video = ta.pop("VideoExtension", None)
        text_ad = {k: v for k, v in ta.items() if v is not None}
        text_ad.setdefault("Mobile", "NO")
        if ext_ids:
            text_ad["AdExtensionIds"] = ext_ids
        if video and video.get("CreativeId"):
            text_ad["VideoExtension"] = {"CreativeId": video["CreativeId"]}
        aid = have_ads.get(gid)
        if not aid:
            ares = need(call("ads", {"Ads": [{"AdGroupId": gid, "TextAd": text_ad}]},
                             login, token, "add"), f"ads.add в группу {gid}")
            warnings(ares, "объявление")
            aid = ares["AddResults"][0].get("Id")
            CREATED.append(f"объявление {aid} в группе {gid}")

        if gid not in have_targets:
            tres = need(call("audiencetargets", {"AudienceTargets": [{
                "AdGroupId": gid,
                "RetargetingListId": conditions[p["slug"]],
            }]}, login, token, "add"), f"audiencetargets.add в группу {gid}")
            warnings(tres, "таргетинг")

        moved = 0
        for m in (() if gid in have_mods else p["mods"]):
            # Type в add не передаётся (как и Scope у условий) — вид корректировки Директ
            # определяет по тому, какой блок пришёл в теле
            item = {"AdGroupId": gid}
            if m["Type"] == "DEMOGRAPHICS_ADJUSTMENT":
                item["DemographicsAdjustment"] = {
                    k: v for k, v in (m.get("DemographicsAdjustment") or {}).items()
                    if v is not None}
            elif m["Type"] == "WEATHER_ADJUSTMENT":
                item["WeatherAdjustment"] = {
                    k: v for k, v in (m.get("WeatherAdjustment") or {}).items()
                    if v is not None}
            else:
                continue
            mres = call("bidmodifiers", {"BidModifiers": [item]}, login, token, "add")
            if mres.get("error"):
                print(f"     ! корректировка {m['Type']} не перенеслась: "
                      f"{mres['error'].get('error_detail') or mres['error'].get('error_string')}")
                continue
            for r0 in mres.get("result", {}).get("AddResults", []):
                if r0.get("Errors"):
                    print(f"     ! корректировка {m['Type']}: {r0['Errors']}")
                else:
                    moved += 1

        print(f"  группа {gid} «{g['Name']}» | ТГО {aid} | условие "
              f"{conditions[p['slug']]} ({name} {window}д) | корректировок перенесено {moved}")


def verify(login: str, token: str, cid: int) -> None:
    print(f"\n### ПРОВЕРКА КАМПАНИИ {cid}")
    kw = call("keywords", {
        "SelectionCriteria": {"CampaignIds": [cid]},
        "FieldNames": ["Id", "AdGroupId", "Keyword", "State"],
        "Page": {"Limit": 100},
    }, login, token)
    rows = kw.get("result", {}).get("Keywords", [])
    print(f"  фраз и автотаргетинга: {len(rows)}")
    for k in rows[:10]:
        print(f"    {k.get('AdGroupId')}: {k.get('Keyword')} [{k.get('State')}]")
    if any("autotargeting" in str(k.get("Keyword")) for k in rows):
        print("  ВНИМАНИЕ: автотаргетинг включился сам — снимать в интерфейсе, "
              "в API тумблера нет")

    at = need(call("audiencetargets", {
        "SelectionCriteria": {"CampaignIds": [cid]},
        "FieldNames": ["Id", "AdGroupId", "RetargetingListId", "State"],
        "Page": {"Limit": 200},
    }, login, token), "audiencetargets.get")
    print(f"  условий подбора в группах: {len(at.get('AudienceTargets', []))}")

    ads = need(call("ads", {
        "SelectionCriteria": {"CampaignIds": [cid]},
        "FieldNames": ["Id", "AdGroupId", "State", "Status"],
        "Page": {"Limit": 200},
    }, login, token), "ads.get")
    by_status: dict[str, int] = {}
    for a in ads.get("Ads", []):
        by_status[a.get("Status")] = by_status.get(a.get("Status"), 0) + 1
    print(f"  объявлений: {len(ads.get('Ads', []))} | по статусам {by_status}")

    c = need(call("campaigns", {
        "SelectionCriteria": {"Ids": [cid]},
        "FieldNames": ["Id", "Name", "State", "Status", "StatusClarification"],
        "Page": {"Limit": 1},
    }, login, token), "campaigns.get")["Campaigns"][0]
    print(f"  кампания: {c.get('State')} / {c.get('Status')} — {c.get('StatusClarification')}")


def main() -> None:
    apply = os.environ.get("APPLY", "").strip() == "1"
    print(f"режим: {'ЗАПИСЬ' if apply else 'план, ничего не пишем'}\n")

    for login, token in clients():
        if not token:
            continue
        if EXPECTED_LOGIN_PART not in login.lower():
            fail(f"логин «{login}» не похож на BJORN")
        print(f"аккаунт {login}")

        src = read_source(login, token)
        segments = ensure_segments(apply)
        conditions = ensure_conditions(login, token, segments, apply)
        cid = create_campaign(login, token, src, apply)
        build_groups(login, token, src, cid, conditions, apply)
        if apply and cid:
            verify(login, token, cid)
            print("\nготово. Кампания создана в состоянии, в котором её отдал Директ — "
                  "проверить статусы выше перед включением.")
        return
    fail("нет доступа к аккаунту")


if __name__ == "__main__":
    main()
