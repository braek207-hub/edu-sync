"""BJORN: мини-кампания РСЯ на ретаргет «был 7 дней, не отказ, не купил».

Что создаёт (по шагам, каждый шаг проверяется чтением):
  1. Условие ретаргетинга: ALL цель «Посетил сайт» 7 дней · NONE сегмент «Отказы» 7 дней
     · NONE цель «Ecommerce: покупка» 30 дней.
  2. Набор быстрых ссылок (8 штук) на разделы сайта и страницу акции.
  3. Кампанию: только сети (поиск SERVING_OFF), недельный бюджет, потолок цены клика,
     список запрещённых площадок скопирован с действующей РСЯ-кампании 714000003.
  4. Группу на всю Россию с условием ретаргетинга.
  5. Одно ТГО: оффер «скидка до −20% на зиму» + картинка + видеодополнение + уточнения.
  6. Отправляет объявление на модерацию и включает кампанию.

Запуск: APPLY=1 — иначе только печатает, что собирается сделать.
"""

from __future__ import annotations

import json
import os
import sys

import requests

API = "https://api.direct.yandex.com/json/v5/"
APPLY = os.environ.get("APPLY", "").strip() == "1"

# Кампания-образец: у неё берём список запрещённых площадок (чистильщик РСЯ их накопил)
SAMPLE_CAMPAIGN = 714000003

# Цели и сегменты счётчика (проверено пробой probe_bjorn_retarget_assets.py)
GOAL_VISITED = 462902408        # «Посетил сайт»
SEGMENT_BOUNCE = 1007677140     # сегмент Метрики «Отказы»
GOAL_PURCHASE = 341172800       # «Ecommerce: покупка»

CAMPAIGN_NAME = "РСЯ. Ретаргет. Был 7д без покупки. Скидка −20%"
GROUP_NAME = "Ретаргет · 7 дней · не отказ · не купил"

WEEKLY_LIMIT_RUB = 3000
BID_CEILING_RUB = 40

PROMO_URL = "https://bjornlarsen.ru/catalog/sezonnaya-aktsiya/skidka-do-20-na-zimu/"
UTM = ("utm_source=yandex&utm_medium=cpc"
       "&utm_campaign={campaign_id}-retarget-7d&utm_content={ad_id}")

# Уточнения — существующие, все ACCEPTED (probe_bjorn_retarget_assets.py)
CALLOUT_IDS = [44241920, 41345224, 41345225, 44241925, 44312727, 44241922, 44241923, 44241926]

# Картинка действующего каталожного ТГО смарт-кампании 713525055 (прошла модерацию)
AD_IMAGE_HASH = "7ovkyj4xhZQsvHB5XM2kSg"

SITELINKS = [
    ("Скидка до −20% на зиму", PROMO_URL,
     "21 модель зимней коллекции по акции. Успейте до конца акции"),
    ("Женские куртки и парки", "https://bjornlarsen.ru/catalog/zhenskie/",
     "Пуховики, парки и утеплённые куртки женской коллекции"),
    ("Мужские куртки и парки", "https://bjornlarsen.ru/catalog/muzhskie/",
     "Аляски, парки и зимние куртки мужской коллекции"),
    ("Подобрать по погоде", "https://bjornlarsen.ru/catalog/po-pogode/",
     "Куртки под температуру: от +10 до −50°C"),
    ("Новинки сезона", "https://bjornlarsen.ru/catalog/new/",
     "Свежие модели зимней коллекции этого сезона"),
    ("Хиты продаж", "https://bjornlarsen.ru/catalog/bestsellers/",
     "Модели, которые выбирают чаще всего"),
    ("Как выбрать размер", "https://bjornlarsen.ru/info/kak-vybrat-razmer-kurtki/",
     "Таблица размеров и советы по посадке"),
    ("Обмен и возврат", "https://bjornlarsen.ru/info/exchange-and-refund/",
     "Примерка при курьере, простой обмен и возврат"),
]

AD_TITLE = "Зимние куртки BJORN LARSEN — скидка до −20%"
AD_TITLE2 = "Акция на зимнюю коллекцию"
AD_TEXT = "Вы смотрели наши куртки. Сейчас на зиму скидка до −20%. Примерка до оплаты."
AD_DISPLAY_PATH = "Скидка-до-20"


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
        return {"error": {"error_string": f"HTTP {resp.status_code}",
                          "error_detail": resp.text[:300]}}


def fail(step: str, body: dict) -> None:
    print(f"\n!!! ШАГ «{step}» НЕ ВЫПОЛНЕН")
    print(json.dumps(body, ensure_ascii=False, indent=1)[:2000])
    sys.exit(1)


def added_id(step: str, body: dict, key: str) -> int:
    """Достаёт Id из ответа *.add, падая на первой же ошибке — частичный результат хуже отказа."""
    if body.get("error"):
        fail(step, body)
    results = body.get("result", {}).get(key, [])
    if not results:
        fail(step, body)
    item = results[0]
    if item.get("Errors"):
        fail(step, body)
    if item.get("Warnings"):
        print(f"   предупреждения: {json.dumps(item['Warnings'], ensure_ascii=False)}")
    return item["Id"]


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


def build(login: str, token: str) -> None:
    counter_id = int(os.environ.get("METRICA_COUNTER_ID", "0").strip() or 0)
    if not counter_id:
        print("нет METRICA_COUNTER_ID — счётчик в кампанию не поставить")
        sys.exit(1)

    # ── подготовка: что берём у действующей кампании ──────────────────────────
    sample = call("campaigns", {
        "SelectionCriteria": {"Ids": [SAMPLE_CAMPAIGN]},
        "FieldNames": ["Id", "ExcludedSites", "TimeZone"],
        "Page": {"Limit": 1},
    }, login, token)
    if sample.get("error"):
        fail("чтение кампании-образца", sample)
    sample_c = sample["result"]["Campaigns"][0]
    excluded = (sample_c.get("ExcludedSites") or {}).get("Items") or []
    print(f"запрещённых площадок скопируем: {len(excluded)}")

    # видеодополнение: берём любой готовый видеокреатив аккаунта
    cre = call("creatives", {
        "SelectionCriteria": {"Types": ["VIDEO_EXTENSION"]},
        "FieldNames": ["Id", "Type", "Name"],
        "Page": {"Limit": 20},
    }, login, token)
    video_id = None
    if cre.get("error"):
        print(f"видеокреативы не прочитались: {cre['error'].get('error_string')} "
              f"| {cre['error'].get('error_detail')}")
    else:
        rows = cre.get("result", {}).get("Creatives", [])
        if rows:
            video_id = rows[0]["Id"]
            print(f"видеодополнение: {video_id} «{rows[0].get('Name')}»")
    if not video_id:
        print("видеодополнение не подобралось — объявление будет без видео")

    print("\nЧТО БУДЕТ СОЗДАНО")
    print(f"  условие ретаргетинга: ALL цель {GOAL_VISITED} 7д · "
          f"NONE сегмент {SEGMENT_BOUNCE} 7д · NONE цель {GOAL_PURCHASE} 30д")
    print(f"  кампания: «{CAMPAIGN_NAME}», только сети, {WEEKLY_LIMIT_RUB} ₽/нед, "
          f"потолок клика {BID_CEILING_RUB} ₽, счётчик {counter_id}")
    print(f"  группа: «{GROUP_NAME}», регион 225 (Россия)")
    print(f"  объявление: {AD_TITLE} | {AD_TITLE2} | {AD_TEXT}")
    print(f"  посадочная: {PROMO_URL}?{UTM}")
    if not APPLY:
        print("\nAPPLY не выставлен — ничего не записано.")
        return

    # ── 1. условие ретаргетинга ───────────────────────────────────────────────
    rl = call("retargetinglists", {"RetargetingLists": [{
        "Name": "BJORN Был 7д · не отказ · без покупки",
        "Description": "Посетил сайт за 7 дней, визит не отказной, покупки за 30 дней нет",
        "Type": "RETARGETING",
        "Rules": [
            {"Operator": "ALL", "Arguments": [
                {"MembershipLifeSpan": 7, "ExternalId": GOAL_VISITED}]},
            {"Operator": "NONE", "Arguments": [
                {"MembershipLifeSpan": 7, "ExternalId": SEGMENT_BOUNCE}]},
            {"Operator": "NONE", "Arguments": [
                {"MembershipLifeSpan": 30, "ExternalId": GOAL_PURCHASE}]},
        ],
    }]}, login, token, "add")
    rl_id = added_id("условие ретаргетинга", rl, "AddResults")
    print(f"\n1/6 условие ретаргетинга: {rl_id}")

    # ── 2. быстрые ссылки ─────────────────────────────────────────────────────
    sl = call("sitelinks", {"SitelinksSets": [{"Sitelinks": [
        {"Title": t, "Href": h + ("&" if "?" in h else "?") + UTM, "Description": d}
        for t, h, d in SITELINKS
    ]}]}, login, token, "add")
    sl_id = added_id("быстрые ссылки", sl, "AddResults")
    print(f"2/6 набор быстрых ссылок: {sl_id}")

    # ── 3. кампания ───────────────────────────────────────────────────────────
    cmp_body = {
        "Name": CAMPAIGN_NAME,
        "StartDate": os.environ.get("START_DATE", "").strip() or None,
        "ClientInfo": None,
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
            "CounterIds": {"Items": [counter_id]},
            "Settings": [
                {"Option": "ADD_METRICA_TAG", "Value": "NO"},
                {"Option": "ADD_OPENSTAT_TAG", "Value": "NO"},
                {"Option": "ENABLE_SITE_MONITORING", "Value": "YES"},
                {"Option": "ENABLE_EXTENDED_AD_TITLE", "Value": "YES"},
                {"Option": "ENABLE_COMPANY_INFO", "Value": "YES"},
            ],
        },
        "TimeZone": sample_c.get("TimeZone") or "Europe/Moscow",
    }
    if excluded:
        cmp_body["ExcludedSites"] = {"Items": excluded}
    cmp_body = {k: v for k, v in cmp_body.items() if v is not None}
    cm = call("campaigns", {"Campaigns": [cmp_body]}, login, token, "add")
    cmp_id = added_id("кампания", cm, "AddResults")
    print(f"3/6 кампания: {cmp_id}")

    # ── 4. группа + условие показа ────────────────────────────────────────────
    gr = call("adgroups", {"AdGroups": [{
        "Name": GROUP_NAME,
        "CampaignId": cmp_id,
        "RegionIds": [225],
    }]}, login, token, "add")
    gr_id = added_id("группа", gr, "AddResults")
    print(f"4/6 группа: {gr_id}")

    at = call("audiencetargets", {"AudienceTargets": [{
        "AdGroupId": gr_id,
        "RetargetingListId": rl_id,
    }]}, login, token, "add")
    at_id = added_id("условие показа группы", at, "AddResults")
    print(f"    условие показа привязано: {at_id}")

    # ── 5. объявление ─────────────────────────────────────────────────────────
    text_ad = {
        "Title": AD_TITLE,
        "Title2": AD_TITLE2,
        "Text": AD_TEXT,
        "Href": PROMO_URL + "?" + UTM,
        "DisplayUrlPath": AD_DISPLAY_PATH,
        "AdImageHash": AD_IMAGE_HASH,
        "SitelinkSetId": sl_id,
        "AdExtensionIds": CALLOUT_IDS,
    }
    if video_id:
        text_ad["VideoExtension"] = {"CreativeId": video_id}
    ad = call("ads", {"Ads": [{"AdGroupId": gr_id, "TextAd": text_ad}]}, login, token, "add")
    ad_id = added_id("объявление", ad, "AddResults")
    print(f"5/6 объявление: {ad_id}")

    # ── 6. модерация и запуск ─────────────────────────────────────────────────
    mod = call("ads", {"SelectionCriteria": {"Ids": [ad_id]}}, login, token, "moderate")
    if mod.get("error"):
        fail("отправка на модерацию", mod)
    print(f"6/6 на модерацию: {json.dumps(mod.get('result'), ensure_ascii=False)[:300]}")

    res = call("campaigns", {"SelectionCriteria": {"Ids": [cmp_id]}}, login, token, "resume")
    print(f"    запуск кампании: {json.dumps(res.get('result') or res.get('error'), ensure_ascii=False)[:300]}")

    # ── проверка чтением ──────────────────────────────────────────────────────
    print("\nПРОВЕРКА ЧТЕНИЕМ")
    chk = call("campaigns", {
        "SelectionCriteria": {"Ids": [cmp_id]},
        "FieldNames": ["Id", "Name", "State", "Status", "StatusClarification", "StartDate"],
        "TextCampaignFieldNames": ["BiddingStrategy", "CounterIds"],
        "Page": {"Limit": 1},
    }, login, token)
    print(json.dumps(chk.get("result") or chk, ensure_ascii=False, indent=1)[:1500])

    chk_ad = call("ads", {
        "SelectionCriteria": {"Ids": [ad_id]},
        "FieldNames": ["Id", "AdGroupId", "CampaignId", "State", "Status", "StatusClarification"],
        "TextAdFieldNames": ["Title", "Title2", "Text", "Href", "AdImageHash",
                             "SitelinkSetId", "AdExtensions", "VideoExtension"],
        "Page": {"Limit": 1},
    }, login, token)
    print(json.dumps(chk_ad.get("result") or chk_ad, ensure_ascii=False, indent=1)[:2000])

    chk_at = call("audiencetargets", {
        "SelectionCriteria": {"AdGroupIds": [gr_id]},
        "FieldNames": ["Id", "AdGroupId", "RetargetingListId", "State", "ContextBid"],
        "Page": {"Limit": 10},
    }, login, token)
    print(json.dumps(chk_at.get("result") or chk_at, ensure_ascii=False, indent=1)[:800])

    print(f"\nГОТОВО: кампания {cmp_id}, группа {gr_id}, объявление {ad_id}, "
          f"условие {rl_id}, быстрые ссылки {sl_id}")


def main() -> None:
    for login, token in clients():
        if token:
            print(f"аккаунт {login}")
            build(login, token)
            return
    print("нет доступа: ни одного логина с токеном")
    sys.exit(1)


if __name__ == "__main__":
    main()
