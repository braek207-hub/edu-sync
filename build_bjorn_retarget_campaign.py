"""BJORN: мини-кампания РСЯ на ретаргет «был 7 дней, не отказ, не купил».

Что создаёт (каждый шаг проверяется чтением, падение — с перечнем уже созданного):
  1. Сегмент Метрики «Не отказ (визиты)» = ym:s:bounce=='No', если его ещё нет.
     Без него «не отказ» в Директе выразить нечем: исключать сегмент «Отказы» нельзя —
     это выкинет всех, у кого когда-либо был отказный визит, а срок жизни для сегментов
     Директ игнорирует.
  2. Кампанию: только сети (поиск SERVING_OFF), недельный бюджет, потолок цены клика,
     список запрещённых площадок скопирован с действующей РСЯ-кампании 714000003.
  3. Группу на всю Россию.
  4. Условие ретаргетинга: ALL (цель «Посетил сайт» 7 дней + сегмент «Не отказ»)
     · NONE (цель «Ecommerce: покупка» 30 дней) и привязку условия к группе.
  5. Набор быстрых ссылок (8 штук) на разделы сайта и страницу акции.
  6. Одно ТГО: оффер «скидка до -20% на зиму» + картинка + видеодополнение + уточнения.
  7. Отправляет объявление на модерацию и включает кампанию.

Порядок такой намеренно: общие для аккаунта объекты (условие, быстрые ссылки) создаются
после кампании и группы, чтобы падение на них не оставляло мусор в общих списках.

Запуск: APPLY=1 — иначе только печатает, что собирается сделать.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

import requests

API = "https://api.direct.yandex.com/json/v5/"
METRIKA = "https://api-metrika.yandex.net/management/v1"
APPLY = os.environ.get("APPLY", "").strip() == "1"

EXPECTED_LOGIN_PART = "bjorn"

# Кампания-образец: у неё берём список запрещённых площадок (чистильщик РСЯ их накопил)
SAMPLE_CAMPAIGN = 714000003

# Цели счётчика (проверено пробой probe_bjorn_retarget_assets.py)
GOAL_VISITED = 462902408        # «Посетил сайт»
GOAL_PURCHASE = 341172800       # «Ecommerce: покупка»
VISIT_DAYS = 7
PURCHASE_DAYS = 30

SEGMENT_NAME = "Не отказ (визиты)"
SEGMENT_EXPRESSION = "ym:s:bounce=='No'"

CAMPAIGN_NAME = "РСЯ. Ретаргет. Был 7д без покупки. Скидка -20%"
GROUP_NAME = "Ретаргет · 7 дней · не отказ · не купил"

WEEKLY_LIMIT_RUB = 3000
BID_CEILING_RUB = 40

PROMO_URL = "https://bjornlarsen.ru/catalog/sezonnaya-aktsiya/skidka-do-20-na-zimu/"
UTM = ("utm_source=yandex&utm_medium=cpc"
       "&utm_campaign={campaign_id}-retarget-7d&utm_content={ad_id}")

# Уточнения — существующие, все ACCEPTED (probe_bjorn_retarget_assets.py)
CALLOUT_IDS = [44241920, 41345224, 41345225, 44241925, 44312727, 44241922, 44241923, 44241926]

# Картинка действующего каталожного ТГО смарт-кампании 713525055.
# ТГО принимают только REGULAR и WIDE — тип проверяется перед записью.
AD_IMAGE_HASH = "7ovkyj4xhZQsvHB5XM2kSg"
AD_IMAGE_ALLOWED_TYPES = ("REGULAR", "WIDE")

SITELINKS = [
    ("Скидка до -20% на зиму", PROMO_URL,
     "21 модель зимней коллекции по акции. Успейте до конца акции"),
    ("Женские куртки и парки", "https://bjornlarsen.ru/catalog/zhenskie/",
     "Пуховики, парки и утеплённые куртки женской коллекции"),
    ("Мужские куртки и парки", "https://bjornlarsen.ru/catalog/muzhskie/",
     "Аляски, парки и зимние куртки мужской коллекции"),
    ("Подобрать по погоде", "https://bjornlarsen.ru/catalog/po-pogode/",
     "Куртки под температуру: от +10 до -50°C"),
    ("Новинки сезона", "https://bjornlarsen.ru/catalog/new/",
     "Свежие модели зимней коллекции этого сезона"),
    ("Хиты продаж", "https://bjornlarsen.ru/catalog/bestsellers/",
     "Модели, которые выбирают чаще всего"),
    ("Как выбрать размер", "https://bjornlarsen.ru/info/kak-vybrat-razmer-kurtki/",
     "Таблица размеров и советы по посадке"),
    ("Обмен и возврат", "https://bjornlarsen.ru/info/exchange-and-refund/",
     "Примерка при курьере, простой обмен и возврат"),
]

AD_TITLE = "Зимние куртки BJORN LARSEN - скидка до -20%"
AD_TITLE2 = "Акция на зимнюю коллекцию"
AD_TEXT = "Вы смотрели наши куртки. Сейчас на зиму скидка до -20%. Примерка до оплаты."
AD_DISPLAY_PATH = "Скидка-до-20"

CREATED: list[str] = []


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
    if CREATED:
        print("\nУЖЕ СОЗДАНО (убрать руками или доиграть следующим запуском):")
        for line in CREATED:
            print(f"  {line}")
    else:
        print("\nничего создать не успели")
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


def ensure_segment(counter_id: int) -> int:
    """Сегмент «визит не отказ». Если такой уже есть — берём его, второй не плодим."""
    token = os.environ.get("METRICA_TOKEN", "").strip()
    if not token:
        fail("сегмент Метрики", {"error": {"error_string": "нет METRICA_TOKEN"}})
    h = {"Authorization": f"OAuth {token}"}

    r = requests.get(f"{METRIKA}/counter/{counter_id}/segments", headers=h, timeout=60)
    if r.status_code != 200:
        fail("чтение сегментов Метрики", {"error": {"error_string": f"HTTP {r.status_code}",
                                                    "error_detail": r.text[:300]}})
    for s in r.json().get("segments", []):
        if str(s.get("expression")) == SEGMENT_EXPRESSION or s.get("name") == SEGMENT_NAME:
            print(f"   сегмент уже есть: {s.get('segment_id')} «{s.get('name')}»")
            return int(s["segment_id"])

    if not APPLY:
        print("   сегмента «не отказ» нет, будет создан при APPLY=1")
        return 0
    payload = json.dumps({"segment": {"name": SEGMENT_NAME, "expression": SEGMENT_EXPRESSION}},
                         ensure_ascii=False).encode("utf-8")
    r = requests.post(f"{METRIKA}/counter/{counter_id}/segments",
                      headers={**h, "Content-Type": "application/json; charset=utf-8"},
                      data=payload, timeout=60)
    if r.status_code not in (200, 201):
        fail("создание сегмента Метрики", {"error": {"error_string": f"HTTP {r.status_code}",
                                                     "error_detail": r.text[:400]}})
    seg_id = int(r.json()["segment"]["segment_id"])
    CREATED.append(f"сегмент Метрики {seg_id} «{SEGMENT_NAME}»")
    return seg_id


def build(login: str, token: str) -> None:
    if EXPECTED_LOGIN_PART not in login.lower():
        print(f"логин «{login}» не похож на кабинет BJORN — останавливаюсь")
        sys.exit(1)

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
    if len(excluded) > 1000 or any(len(x) > 255 for x in excluded):
        print(f"список запрещённых площадок не влезает в лимиты Директа "
              f"({len(excluded)} шт) — копировать не будем")
        excluded = []
    print(f"запрещённых площадок скопируем: {len(excluded)}")

    # картинка: ТГО принимает только REGULAR и WIDE
    img = call("adimages", {
        "SelectionCriteria": {"AdImageHashes": [AD_IMAGE_HASH]},
        "FieldNames": ["AdImageHash", "Type", "Name"],
        "Page": {"Limit": 1},
    }, login, token)
    image_hash = None
    if img.get("error"):
        print(f"картинка не прочиталась: {img['error'].get('error_string')}")
    else:
        rows = img.get("result", {}).get("AdImages", [])
        if rows and rows[0].get("Type") in AD_IMAGE_ALLOWED_TYPES:
            image_hash = AD_IMAGE_HASH
            print(f"картинка: {AD_IMAGE_HASH} ({rows[0].get('Type')}) «{rows[0].get('Name')}»")
        else:
            print(f"картинка {AD_IMAGE_HASH} не годится для ТГО: "
                  f"{json.dumps(rows, ensure_ascii=False)[:200]}")

    # видеодополнение: берём любой готовый видеокреатив аккаунта
    cre = call("creatives", {
        "SelectionCriteria": {"Types": ["VIDEO_EXTENSION_CREATIVE"]},
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

    start_date = (os.environ.get("START_DATE", "").strip()
                  or dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).date().isoformat())

    print("\nЧТО БУДЕТ СОЗДАНО")
    print(f"  сегмент Метрики «{SEGMENT_NAME}» = {SEGMENT_EXPRESSION} (если ещё нет)")
    print(f"  условие ретаргетинга: ALL (цель {GOAL_VISITED} {VISIT_DAYS}д + сегмент «не отказ») "
          f"· NONE (цель {GOAL_PURCHASE} {PURCHASE_DAYS}д)")
    print(f"  кампания: «{CAMPAIGN_NAME}», старт {start_date}, только сети, "
          f"{WEEKLY_LIMIT_RUB} ₽/нед, потолок клика {BID_CEILING_RUB} ₽, счётчик {counter_id}")
    print(f"  группа: «{GROUP_NAME}», регион 225 (Россия)")
    print(f"  объявление: {AD_TITLE} | {AD_TITLE2} | {AD_TEXT}")
    print(f"  посадочная: {PROMO_URL}?{UTM}")

    # ── 1. сегмент «не отказ» ─────────────────────────────────────────────────
    seg_id = ensure_segment(counter_id)
    if not APPLY:
        print("\nAPPLY не выставлен — ничего не записано.")
        return
    print(f"\n1/7 сегмент «не отказ»: {seg_id}")

    # ── 2. кампания ───────────────────────────────────────────────────────────
    cmp_body = {
        "Name": CAMPAIGN_NAME,
        "StartDate": start_date,
        "TimeZone": sample_c.get("TimeZone") or "Europe/Moscow",
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
    }
    if excluded:
        cmp_body["ExcludedSites"] = {"Items": excluded}
    cm = call("campaigns", {"Campaigns": [cmp_body]}, login, token, "add")
    cmp_id = added_id("кампания", cm, "AddResults")
    CREATED.append(f"кампания {cmp_id} «{CAMPAIGN_NAME}»")
    print(f"2/7 кампания: {cmp_id}")

    # ── 3. группа ─────────────────────────────────────────────────────────────
    gr = call("adgroups", {"AdGroups": [{
        "Name": GROUP_NAME,
        "CampaignId": cmp_id,
        "RegionIds": [225],
    }]}, login, token, "add")
    gr_id = added_id("группа", gr, "AddResults")
    CREATED.append(f"группа {gr_id}")
    print(f"3/7 группа: {gr_id}")

    # ── 4. условие ретаргетинга и привязка к группе ───────────────────────────
    rl = call("retargetinglists", {"RetargetingLists": [{
        "Name": "BJORN Был 7д · не отказ · без покупки",
        "Description": ("Посетил сайт за 7 дней, есть неотказный визит, "
                        "покупки за 30 дней нет"),
        "Type": "RETARGETING",
        "Rules": [
            {"Operator": "ALL", "Arguments": [
                {"MembershipLifeSpan": VISIT_DAYS, "ExternalId": GOAL_VISITED},
                {"MembershipLifeSpan": VISIT_DAYS, "ExternalId": seg_id},
            ]},
            {"Operator": "NONE", "Arguments": [
                {"MembershipLifeSpan": PURCHASE_DAYS, "ExternalId": GOAL_PURCHASE}]},
        ],
    }]}, login, token, "add")
    rl_id = added_id("условие ретаргетинга", rl, "AddResults")
    CREATED.append(f"условие ретаргетинга {rl_id}")
    print(f"4/7 условие ретаргетинга: {rl_id}")

    at = call("audiencetargets", {"AudienceTargets": [{
        "AdGroupId": gr_id,
        "RetargetingListId": rl_id,
    }]}, login, token, "add")
    at_id = added_id("условие показа группы", at, "AddResults")
    print(f"    условие показа привязано: {at_id}")

    # ── 5. быстрые ссылки ─────────────────────────────────────────────────────
    sl = call("sitelinks", {"SitelinksSets": [{"Sitelinks": [
        {"Title": t, "Href": h + ("&" if "?" in h else "?") + UTM, "Description": d}
        for t, h, d in SITELINKS
    ]}]}, login, token, "add")
    sl_id = added_id("быстрые ссылки", sl, "AddResults")
    CREATED.append(f"набор быстрых ссылок {sl_id}")
    print(f"5/7 набор быстрых ссылок: {sl_id}")

    # ── 6. объявление ─────────────────────────────────────────────────────────
    text_ad = {
        "Title": AD_TITLE,
        "Title2": AD_TITLE2,
        "Text": AD_TEXT,
        "Mobile": "NO",
        "Href": PROMO_URL + "?" + UTM,
        "DisplayUrlPath": AD_DISPLAY_PATH,
        "SitelinkSetId": sl_id,
        "AdExtensionIds": CALLOUT_IDS,
    }
    if image_hash:
        text_ad["AdImageHash"] = image_hash
    if video_id:
        text_ad["VideoExtension"] = {"CreativeId": video_id}
    ad = call("ads", {"Ads": [{"AdGroupId": gr_id, "TextAd": text_ad}]}, login, token, "add")
    ad_id = added_id("объявление", ad, "AddResults")
    CREATED.append(f"объявление {ad_id}")
    print(f"6/7 объявление: {ad_id}")

    # ── 7. модерация и запуск ─────────────────────────────────────────────────
    mod = call("ads", {"SelectionCriteria": {"Ids": [ad_id]}}, login, token, "moderate")
    if mod.get("error"):
        fail("отправка на модерацию", mod)
    print(f"7/7 на модерацию: {json.dumps(mod.get('result'), ensure_ascii=False)[:300]}")

    res = call("campaigns", {"SelectionCriteria": {"Ids": [cmp_id]}}, login, token, "resume")
    print(f"    запуск кампании: "
          f"{json.dumps(res.get('result') or res.get('error'), ensure_ascii=False)[:300]}")

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
        "FieldNames": ["Id", "AdGroupId", "RetargetingListId", "State"],
        "Page": {"Limit": 10},
    }, login, token)
    print(json.dumps(chk_at.get("result") or chk_at, ensure_ascii=False, indent=1)[:800])

    print(f"\nГОТОВО: кампания {cmp_id}, группа {gr_id}, объявление {ad_id}, "
          f"условие {rl_id}, быстрые ссылки {sl_id}, сегмент {seg_id}")


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
