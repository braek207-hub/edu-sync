# -*- coding: utf-8 -*-
"""
sync/agent/segments.py — загрузчики Директа для автопилота.

Три источника:
  1. сегментные срезы (Reports API, CUSTOM_REPORT) — для корректировок и для
     витрины срезов по кампаниям;
  2. объекты кабинета (adgroups/keywords/ads, API v5) — снимок структуры;
  3. поисковые запросы (SEARCH_QUERY_PERFORMANCE_REPORT).

Reports API асинхронный: 201/202 значат «отчёт готовится», нужен цикл ожидания.
"""

import hashlib
import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import requests

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"
CAMPAIGNS_URL = "https://api.direct.yandex.com/json/v5/campaigns"
ADGROUPS_URL = "https://api.direct.yandex.com/json/v5/adgroups"
KEYWORDS_URL = "https://api.direct.yandex.com/json/v5/keywords"
ADS_URL = "https://api.direct.yandex.com/json/v5/ads"

MAX_WAIT_SECONDS = 600
POLL_SECONDS = 10
# Формы, проверенные рабочим sync/edu_direct_settings.py: страница 1000,
# кампании чанками по 10, глубина offset ограничена 10000.
PAGE_LIMIT = 1000
CAMPAIGN_CHUNK = 10
MAX_OFFSET = 10_000
# Параллелизм снимка объектов: у API Директа есть суточный лимит баллов,
# поэтому воркеров немного.
OBJECT_WORKERS = 4

# Срез → поле Reports API. Состав проверен probe-прогоном 31781715471:
# Device / Gender / Age / AdNetworkType / TargetingLocationName принимаются,
# а HourOfDay отвергается ошибкой 8000 — почасовой детализации расхода
# CUSTOM_REPORT не отдаёт вообще. Расписание считается на Э1 из Метрики.
#
# Регион отдаётся НАЗВАНИЕМ (TargetingLocationName), а RegionalAdjustment в
# API записи требует числовой RegionId. Кандидат TargetingLocationId probe-ом
# не проверялся (probe_report_fields.py его теперь спрашивает), поэтому поле
# здесь не меняем вслепую: отказ по 8000 обрушил бы весь региональный срез,
# который сейчас работает и нужен витрине. До ответа probe региональные
# корректировки не ПРИМЕНЯЮТСЯ — с явной причиной в отчёте прогона
# (sync/agent/writer/plan.py::UNSUPPORTED_REGION_REASON), а не падают на
# int("Москва") с вечным переприменением.
SEGMENT_FIELDS = {
    "device": "Device",
    "gender": "Gender",
    "age": "Age",
    "region": "TargetingLocationName",
    "network": "AdNetworkType",
}

# Тексты объявлений живут НЕ в FieldNames, а в отдельном TextAdFieldNames — ads.get
# отвергает "TextAd" в FieldNames ошибкой 8000 с перечнем допустимых значений.
_OBJECT_ENDPOINTS = {
    "adgroup": (ADGROUPS_URL, "AdGroups",
                ["Id", "CampaignId", "Name", "RegionIds", "NegativeKeywords"], {}),
    "keyword": (KEYWORDS_URL, "Keywords",
                ["Id", "CampaignId", "AdGroupId", "Keyword", "State", "Status"], {}),
    "ad": (ADS_URL, "Ads",
           ["Id", "CampaignId", "AdGroupId", "State", "Status", "Type", "Subtype"],
           {"TextAdFieldNames": ["Title", "Title2", "Text", "Href", "DisplayUrlPath"]}),
}


def _api_headers(login: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
        "Client-Login": login,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
    }


def _report_headers(login: str) -> Dict[str, str]:
    headers = _api_headers(login)
    headers.update({
        "processingMode": "auto",
        "returnMoneyInMicros": "false",
        "skipReportHeader": "true",
        "skipReportSummary": "true",
    })
    return headers


def _stamp_report_name(payload: Dict[str, Any]) -> None:
    """Имя отчёта = префикс + хеш параметров.

    Директ помнит связку имя↔параметры: повторный запрос с тем же именем, но
    изменившимися параметрами отвергается с ошибкой 4000. Хеш делает имя
    детерминированным по содержимому — те же параметры переиспользуют отчёт,
    любое изменение даёт новое имя.
    """
    params = payload["params"]
    prefix = str(params.get("ReportName") or "agent")
    body = {k: v for k, v in params.items() if k != "ReportName"}
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:10]
    params["ReportName"] = f"{prefix}-{digest}"


def _run_report(login: str, payload: Dict[str, Any]) -> str:
    """Reports API асинхронный: 201/202 значит «готовится», нужен цикл ожидания."""
    _stamp_report_name(payload)
    waited = 0
    while waited <= MAX_WAIT_SECONDS:
        resp = requests.post(
            REPORTS_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=_report_headers(login),
            timeout=120,
        )
        # Директ отдаёт UTF-8, но без charset в заголовке — requests угадывает latin-1
        # и русский текст ошибки превращается в мусор.
        resp.encoding = "utf-8"
        if resp.status_code == 200:
            return resp.text
        if resp.status_code in (201, 202):
            time.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            continue
        raise RuntimeError(f"Reports API {resp.status_code}: {resp.text[:400]}")
    raise TimeoutError(f"Отчёт не готов за {MAX_WAIT_SECONDS} с")


def _with_goals(params: Dict[str, Any], goals: List[str]) -> Dict[str, Any]:
    """Conversions — метрика, доступная только вместе с Goals: без них Reports API
    отвергает FieldNames с ошибкой 8000 (проверено на прогоне 31781031949)."""
    if goals:
        params["Goals"] = [str(g) for g in goals]
        params["AttributionModels"] = ["LSC"]
    return params


def _parse_tsv(text: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    reader = io.StringIO(text)
    header_line = reader.readline().rstrip("\n")
    if not header_line:
        return out
    header = header_line.split("\t")
    for line in reader:
        cells = line.rstrip("\n").split("\t")
        if len(cells) != len(header):
            continue
        out.append(dict(zip(header, cells)))
    return out


# Reports API не отдаёт колонку с именем "Conversions". Он отдаёт ПО КОЛОНКЕ НА
# ЦЕЛЬ с суффиксом модели атрибуции: Conversions_330070378_LSCCD. Запрошенная
# нами модель LSC в имени превращается в LSCCD (last significant click,
# cross-device) — тот же сдвиг имён, что уже ловили в витрине LIME.
#
# Пока парсер читал rec["Conversions"], он получал None → 0, и расчёт честно
# докладывал «в срезе нет ни одной конверсии». Так на боевом прогоне
# 32469160289 отказали ВСЕ двенадцать срезов при живых данных: в том же отчёте
# лежало 2753, 60, 2826 и 2753 конверсии по четырём целям.
CONVERSIONS_PREFIX = "Conversions"
# «Нет данных» Директ пишет двумя дефисами, а не нулём и не пустой строкой.
# Проверка на них избыточна — except ValueError ниже поймал бы то же самое, —
# но оставлена намеренно: она называет формат ответа. Мутация, снимающая эту
# ветку, тестами не ловится и не должна: поведение от неё не меняется.
NO_DATA = "--"


def conversion_columns(records: List[Dict[str, str]]) -> List[str]:
    """Имена колонок конверсий в ответе — по одной на цель."""
    seen: List[str] = []
    for rec in records:
        for key in rec:
            if key.startswith(CONVERSIONS_PREFIX + "_") and key not in seen:
                seen.append(key)
    return sorted(seen)


def _cell_int(value: Any) -> int:
    text = str(value or "").strip()
    if not text or text == NO_DATA:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def primary_goal_column(records: List[Dict[str, str]]) -> Optional[str]:
    """Колонка ОДНОЙ цели, по которой считается конверсионность среза.

    Суммировать колонки нельзя: цели пересекаются. В боевом отчёте
    330070378 и 369313502 дали одинаковые числа во всех трёх моделях
    атрибуции (2753/2753, 2654/2654, 2213/2213) — это одно и то же целевое
    действие под двумя идентификаторами. Сумма учла бы его дважды и раздула
    конверсионность втрое.

    Выбирается самая массовая цель кабинета — основное целевое действие,
    то есть заявка. Выбор делается ОДИН РАЗ на весь срез, а не построчно:
    иначе сегменты сравнивались бы по разным целям, и «мобильные
    конверсионнее десктопа» означало бы всего лишь «у них разные цели».

    При равенстве побеждает меньший идентификатор — выбор обязан быть
    детерминированным, иначе имя отчёта и его кеш на стороне API поплывут.
    """
    columns = conversion_columns(records)
    if not columns:
        return None
    totals = {c: sum(_cell_int(r.get(c)) for r in records) for c in columns}
    best = max(totals.values())
    if best <= 0:
        return None
    return sorted(c for c in columns if totals[c] == best)[0]


def fetch_segment_report(
    login: str, segment_kind: str, date_from: str, date_to: str,
    by_campaign: bool = False, goals: List[str] = (),
) -> List[Dict[str, Any]]:
    """Срез за окно. by_campaign=True добавляет разрез по кампаниям и датам —
    для edu_agent_facts_sliced; без него агрегат по аккаунту для корректировок."""
    field = SEGMENT_FIELDS[segment_kind]
    fields = [field, "Clicks", "Cost", "Impressions"]
    if goals:
        fields.append("Conversions")
    if by_campaign:
        fields = ["CampaignId", "Date"] + fields

    payload = {
        "params": _with_goals({
            "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
            "FieldNames": fields,
            "ReportName": f"agent-{segment_kind}-{'bycamp-' if by_campaign else ''}{date_from}-{date_to}",
            "ReportType": "CUSTOM_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }, list(goals))
    }

    records = _parse_tsv(_run_report(login, payload))
    # Одна цель на весь срез: см. primary_goal_column. Выбор построчно сравнивал
    # бы сегменты по разным целям.
    goal_column = primary_goal_column(records)

    rows: List[Dict[str, Any]] = []
    for rec in records:
        row = {
            "segment_kind": segment_kind,
            "segment_key": rec.get(field, ""),
            "slice_key": rec.get(field, ""),
            "clicks": _cell_int(rec.get("Clicks")),
            "impressions": _cell_int(rec.get("Impressions")),
            "conversions": _cell_int(rec.get(goal_column)) if goal_column else 0,
            "cost": float(rec.get("Cost") or 0.0),
        }
        if by_campaign:
            row["campaign_id"] = rec.get("CampaignId", "")
            row["date"] = rec.get("Date", "")
        rows.append(row)
    return rows


def _api_post(url: str, login: str, payload: Dict[str, Any], what: str) -> Dict[str, Any]:
    resp = requests.post(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_api_headers(login),
        timeout=120,
    )
    resp.encoding = "utf-8"
    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"{what}: {body['error']}")
    return body.get("result") or {}


def fetch_campaign_ids(login: str) -> List[int]:
    """Id всех кампаний кабинета — по ним отбираются объекты нижних уровней."""
    out: List[int] = []
    offset = 0
    while True:
        result = _api_post(
            CAMPAIGNS_URL,
            login,
            {
                "method": "get",
                "params": {
                    "SelectionCriteria": {},
                    "FieldNames": ["Id"],
                    "Page": {"Limit": PAGE_LIMIT, "Offset": offset},
                },
            },
            "campaigns.get",
        )
        items = result.get("Campaigns") or []
        out += [int(c["Id"]) for c in items]
        if len(items) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return out


# Ключи, под которыми в блоке стратегии лежит цель оптимизации. PriorityGoals —
# список (у кампании их бывает несколько), GoalId — одиночная цель у стратегий
# вида MaximumConversionRate / PayForConversion.
_STRATEGY_CHANNELS = ("Search", "Network")
_CAMPAIGN_TYPES = ("TextCampaign", "UnifiedCampaign", "MobileAppCampaign")


def goal_ids_from_campaign(campaign: Dict[str, Any]) -> List[int]:
    """Цели оптимизации кампании из её блока стратегии.

    Почему цели вообще нужны здесь: метрика Conversions в Reports API доступна
    ТОЛЬКО вместе с параметром Goals (без него запрос отвергается ошибкой 8000).
    Нет целей — нет конверсий — сегментный расчёт отказывается работать, и
    автопилоту нечего применять. Раньше цели приходили только из секрета
    DIRECT_CLIENTS_JSON: не проставил руками — агент молча оставался слепым.
    """
    out: List[int] = []
    for type_key in _CAMPAIGN_TYPES:
        strategy = (campaign.get(type_key) or {}).get("BiddingStrategy") or {}
        for channel in _STRATEGY_CHANNELS:
            block = strategy.get(channel)
            if not isinstance(block, dict):
                continue
            for value in block.values():
                if not isinstance(value, dict):
                    continue
                for goal in (value.get("PriorityGoals") or []):
                    if isinstance(goal, dict) and goal.get("GoalId") is not None:
                        out.append(int(goal["GoalId"]))
                if value.get("GoalId") is not None:
                    out.append(int(value["GoalId"]))
    # Порядок стабилен и не зависит от порядка ключей в ответе: цели уходят в
    # параметр запроса, а от него зависит имя отчёта и его кеш на стороне API.
    return sorted(set(out))


def fetch_account_goal_ids(login: str) -> List[int]:
    """Цели, на которые реально оптимизируются кампании кабинета.

    Форма запроса взята из рабочего sync/edu_direct_settings.py (кампании трёх
    типов запрашиваются тремя *FieldNames одновременно) — гадать про неё уже
    стоило восьми упавших прогонов.

    Метода /v5/goals у Директа не существует (отдаёт 404), а имена целей живут
    в Метрике; здесь нужны только идентификаторы, и они есть в стратегии.
    """
    type_fields = ["BiddingStrategy"]
    found: List[int] = []
    offset = 0
    while True:
        result = _api_post(
            CAMPAIGNS_URL,
            login,
            {
                "method": "get",
                "params": {
                    "SelectionCriteria": {},
                    "FieldNames": ["Id"],
                    "TextCampaignFieldNames": type_fields,
                    "UnifiedCampaignFieldNames": type_fields,
                    "MobileAppCampaignFieldNames": type_fields,
                    "Page": {"Limit": PAGE_LIMIT, "Offset": offset},
                },
            },
            "campaigns.get:goals",
        )
        items = result.get("Campaigns") or []
        for campaign in items:
            found += goal_ids_from_campaign(campaign)
        if len(items) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        if offset >= MAX_OFFSET:
            break
    return sorted(set(found))


def fetch_objects(login: str, object_level: str, campaign_ids: List[int]) -> List[Dict[str, Any]]:
    """Объекты уровня по кампаниям.

    SelectionCriteria для adgroups/keywords/ads — это CampaignIds, НЕ States:
    States отвергается ошибкой 8000 «Указан неизвестный параметр». Кампании
    подаются чанками по 10 (лимит API), страницы по 1000, глубина offset
    ограничена 10000 — форма проверена рабочим sync/edu_direct_settings.py.
    """
    url, collection, fields, extra = _OBJECT_ENDPOINTS[object_level]
    chunks = [campaign_ids[i:i + CAMPAIGN_CHUNK]
              for i in range(0, len(campaign_ids), CAMPAIGN_CHUNK)]

    def _fetch_chunk(chunk: List[int]) -> List[Dict[str, Any]]:
        items_all: List[Dict[str, Any]] = []
        offset = 0
        while True:
            result = _api_post(
                url,
                login,
                {
                    "method": "get",
                    "params": {
                        "SelectionCriteria": {"CampaignIds": chunk},
                        "FieldNames": fields,
                        "Page": {"Limit": PAGE_LIMIT, "Offset": offset},
                        **extra,
                    },
                },
                f"{object_level}.get",
            )
            items = result.get(collection) or []
            items_all += items
            if len(items) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT
            if offset >= MAX_OFFSET:
                break
        return items_all

    # Чанки кампаний параллельно: 163 кампании × 3 уровня × 4 кабинета — это ~200
    # последовательных запросов, из-за которых прогон 31783879686 шёл 35+ минут.
    out: List[Dict[str, Any]] = []
    if not chunks:
        return out
    with ThreadPoolExecutor(max_workers=OBJECT_WORKERS) as pool:
        for done in as_completed([pool.submit(_fetch_chunk, c) for c in chunks]):
            out += done.result()
    return out


def fetch_search_queries(
    login: str, date_from: str, date_to: str, goals: List[str] = ()
) -> List[Dict[str, Any]]:
    """Поисковые запросы за окно, агрегат без дат. Только строки с кликами:
    показы без кликов дают миллионы строк и ничего не решают.

    Без goals колонка Conversions недоступна, и правило «расход без конверсий»
    вырождается — кандидаты в минус-слова считать будет нечем."""
    fields = ["CampaignId", "Query", "Criteria", "Cost", "Clicks"]
    if goals:
        fields.append("Conversions")
    payload = {
        "params": _with_goals({
            "SelectionCriteria": {
                "DateFrom": date_from,
                "DateTo": date_to,
                "Filter": [{"Field": "Clicks", "Operator": "GREATER_THAN", "Values": ["0"]}],
            },
            "FieldNames": fields,
            "ReportName": f"agent-queries-{date_from}-{date_to}",
            "ReportType": "SEARCH_QUERY_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }, list(goals))
    }
    records = _parse_tsv(_run_report(login, payload))
    # Та же история с именами колонок, что и у сегментных срезов: "Conversions"
    # в ответе нет, есть Conversions_<цель>_<атрибуция>. Здесь цена ошибки
    # другая, но не меньше: по этим числам отбираются кандидаты в минус-слова,
    # и нулевые конверсии у всех запросов означают «весь расход бесполезен» —
    # то есть предложение отминусовать работающую семантику.
    goal_column = primary_goal_column(records)
    return [
        {
            "window_from": date_from,
            "window_to": date_to,
            "campaign_id": rec.get("CampaignId", ""),
            "query": rec.get("Query", ""),
            "matched_key": rec.get("Criteria"),
            "cost": float(rec.get("Cost") or 0.0),
            "clicks": _cell_int(rec.get("Clicks")),
            "conversions": _cell_int(rec.get(goal_column)) if goal_column else 0,
        }
        for rec in records
    ]
