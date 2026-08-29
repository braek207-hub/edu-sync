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
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from sync.agent import scope as agent_scope

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
# Регион берётся ЧИСЛОМ. RegionalAdjustment в API записи требует RegionId, а
# TargetingLocationName отдаёт «Москва» — на таком ключе региональные
# корректировки не применялись вовсе (167 отказов за прогон 29.08.2026).
# TargetingLocationId проверен probe-ом (run 33248004571: OK), поэтому ключ —
# он, а название едет рядом отдельным полем: слепым ключ делать нельзя, «213»
# в идее человеку ничего не говорит.
SEGMENT_FIELDS = {
    "device": "Device",
    "gender": "Gender",
    "age": "Age",
    "region": "TargetingLocationId",
    "network": "AdNetworkType",
}

# Читаемое имя сегмента. Есть только у региона: у остальных срезов ключ сам
# себе имя (MOBILE, GENDER_MALE). Пара запрашивается одним запросом — Id и
# Name одного региона идут в одной строке отчёта и лишних строк не порождают.
SEGMENT_LABEL_FIELDS = {
    "region": "TargetingLocationName",
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


def chosen_goal(records: List[Dict[str, str]], goal_column: Optional[str]) -> Dict[str, Any]:
    """Паспорт выбранной цели — чтобы выбор был ВИДЕН в отчёте прогона.

    По этой одной колонке считается вся конверсионность среза, а значит и все
    корректировки ставок. Выбор автоматический (самая массовая цель кабинета),
    имён целей у Директа в отчёте нет — только идентификаторы, — поэтому
    единственная защита от «корректировки посчитаны по прокрутке страницы» это
    возможность сверить идентификатор глазами. Молчаливый выбор такой
    возможности не даёт.
    """
    return {
        "goal_column": goal_column,
        "conversions": (sum(_cell_int(r.get(goal_column)) for r in records)
                        if goal_column else 0),
        "columns_offered": len(conversion_columns(records)),
    }


def _own_records(tsv: str, excluded_campaign_ids: Iterable[Any]) -> List[Dict[str, str]]:
    """Разбор ответа отчёта без строк кампаний вне зоны ответственности.

    Отсечение стоит ЗДЕСЬ, а не у вызывающего, потому что по этим же строкам
    выбирается колонка цели (primary_goal_column) и считается паспорт отчёта.
    Отсечь после выбора значило бы, что цель — а через неё вся
    конверсионность кабинета и все корректировки ставок — назначена чужой
    кампанией: у неё может быть массовой совсем другая цель.

    Ключ CampaignId, а не campaign_id: строки здесь ещё сырые, как их отдал
    Директ. Отчёт без этой колонки не отсекается ничем — пустое значение ни
    с чем не совпадает, — и это верно: без CampaignId различать нечем.
    """
    return agent_scope.drop_campaign_ids(
        _parse_tsv(tsv), excluded_campaign_ids, id_key="CampaignId")


def fetch_segment_report(
    login: str, segment_kind: str, date_from: str, date_to: str,
    goals: List[str] = (), with_date: bool = True,
    excluded_campaign_ids: Iterable[Any] = (),
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Срез за окно, всегда с разрезом по кампаниям.

    Условия в запросе не бывает никогда — ни в каком виде. «Всё кроме этих
    кампаний» Reports API выразить не умеет: замер 30.08.2026 (run
    33274646184) получил ошибку 4001 «для поля CampaignId допустимы только
    операторы EQUALS, IN», а перечисление своих через IN выбросило бы из
    ответа кампании Мастера — campaigns.get их не отдаёт вовсе
    (sync/agent/master.py), и агент потерял бы их расход, отсекая семь чужих
    РК. Поэтому CampaignId просится ВСЕГДА: без него отсечь чужие строки
    нечем, а кабинетные числа складываются в Python
    (aggregate_account_rows).

    with_date=False убирает из разреза дату: кабинетному агрегату она не
    нужна (числа всё равно суммируются по всему окну), а лишний разрез
    умножает объём ответа на число дней окна. Покампанийным фактам
    (edu_agent_facts_sliced) дата нужна — там недели.

    Возвращает (строки, паспорт выбранной цели) — см. chosen_goal."""
    field = SEGMENT_FIELDS[segment_kind]
    label_field = SEGMENT_LABEL_FIELDS.get(segment_kind)
    fields = [field, "Clicks", "Cost", "Impressions"]
    if label_field:
        fields.insert(1, label_field)
    if goals:
        fields.append("Conversions")
    fields = (["CampaignId", "Date"] if with_date else ["CampaignId"]) + fields

    criteria: Dict[str, Any] = {"DateFrom": date_from, "DateTo": date_to}

    payload = {
        "params": _with_goals({
            "SelectionCriteria": criteria,
            "FieldNames": fields,
            "ReportName": f"agent-{segment_kind}-bycamp-{date_from}-{date_to}",
            "ReportType": "CUSTOM_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }, list(goals))
    }

    records = _own_records(_run_report(login, payload), excluded_campaign_ids)
    # Одна цель на весь срез: см. primary_goal_column. Выбор построчно сравнивал
    # бы сегменты по разным целям.
    goal_column = primary_goal_column(records)

    rows: List[Dict[str, Any]] = []
    for rec in records:
        row = {
            "segment_kind": segment_kind,
            "segment_key": rec.get(field, ""),
            "slice_key": rec.get(field, ""),
            "slice_label": rec.get(label_field, "") if label_field else "",
            "clicks": _cell_int(rec.get("Clicks")),
            "impressions": _cell_int(rec.get("Impressions")),
            "conversions": _cell_int(rec.get(goal_column)) if goal_column else 0,
            "cost": float(rec.get("Cost") or 0.0),
        }
        row["campaign_id"] = rec.get("CampaignId", "")
        if with_date:
            row["date"] = rec.get("Date", "")
        rows.append(row)
    return rows, chosen_goal(records, goal_column)


# Поля, которые складываются при сборке кабинетного агрегата. Остальные —
# описание сегмента, у строк одного segment_key они совпадают.
_SUMMED_FIELDS = ("clicks", "impressions", "conversions")


def aggregate_account_rows(
    rows: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Покампанийные строки среза -> кабинетный агрегат. Чистая функция.

    Заменяет отдельный запрос агрегата у Директа, и не ради экономии: в
    ответе агрегата нет CampaignId, и отсечь в нём чужие РК нечем — их клики
    оседали бы в знаменателе конверсионности сегмента, то есть в кабинетных
    корректировках ставок. Отсечение делается ВЫШЕ по течению
    (scope.drop_campaign_ids), пока Id ещё есть; сюда приходят только свои.

    Порядок результата детерминированный (по segment_key): настройки пишутся
    в базу пачкой, и стабильный порядок делает diff двух прогонов читаемым.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("segment_key", ""))
        slot = out.get(key)
        if slot is None:
            slot = out[key] = {
                "segment_kind": row.get("segment_kind"),
                "segment_key": key,
                "slice_key": row.get("slice_key", key),
                "slice_label": row.get("slice_label", ""),
                "clicks": 0, "impressions": 0, "conversions": 0, "cost": 0.0,
            }
        # Метка сегмента приходит не от каждой кампании (у части строк она
        # пустая), а нужна ровно одна — первая непустая.
        if not slot["slice_label"] and row.get("slice_label"):
            slot["slice_label"] = row["slice_label"]
        for field in _SUMMED_FIELDS:
            slot[field] += int(row.get(field) or 0)
        slot["cost"] += float(row.get("cost") or 0.0)
    for slot in out.values():
        slot["cost"] = round(slot["cost"], 2)
    return [out[key] for key in sorted(out)]


# Сколько раз повторяется ЧТЕНИЕ при обрыве транспорта. Чтение идемпотентно:
# повтор возвращает то же самое, а обрыв на стороне сети — обычное дело на
# длинных отчётах. Боевой прогон Э0 (run 32730235917) упал целиком на
# ChunkedEncodingError «Response ended prematurely» посреди отчёта одного
# кабинета — час работы и все остальные кабинеты вместе с ним.
READ_ATTEMPTS = 4

# Исключения транспорта, при которых повтор осмыслен: тело не дошло или
# соединение оборвалось. Ошибки уровня API (error в теле) сюда не входят —
# они детерминированы, и повтор дал бы тот же ответ.
_TRANSPORT_ERRORS = (
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)


def _api_post(url: str, login: str, payload: Dict[str, Any], what: str,
              attempts: int = READ_ATTEMPTS) -> Dict[str, Any]:
    """POST к API Директа с повтором на обрывах транспорта.

    Повторяется только транспортный сбой. Ответ с полем error — решение
    сервиса, а не помеха связи: он поднимается сразу.
    """
    last_error = None
    for attempt in range(attempts):
        try:
            resp = requests.post(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=_api_headers(login),
                timeout=120,
            )
            resp.encoding = "utf-8"
            body = resp.json()
        except _TRANSPORT_ERRORS as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        if body.get("error"):
            raise RuntimeError(f"{what}: {body['error']}")
        return body.get("result") or {}
    raise last_error  # недостижимо: последняя попытка либо вернула, либо подняла


def fetch_campaign_ids(login: str) -> List[int]:
    """Кампании кабинета в зоне ответственности агента.

    Имя запрашивается ради самой границы (sync/agent/scope.py): кампании вне
    её различаются только по нему, а этот список раздаёт Id снимку структуры,
    справочнику «кампания -> кабинет» и через него — адресации идей. Без
    "Name" в FieldNames фильтру нечего сравнивать.
    """
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
                    "FieldNames": ["Id", "Name"],
                    "Page": {"Limit": PAGE_LIMIT, "Offset": offset},
                },
            },
            "campaigns.get",
        )
        items = result.get("Campaigns") or []
        out += [int(c["Id"]) for c in
                agent_scope.filter_campaign_rows(items, name_key="Name")]
        # Постранично считается ВЕСЬ ответ, а не оставшееся после границы:
        # отфильтрованная страница короче лимита, и обход оборвался бы на
        # первой же странице с чужой кампанией.
        if len(items) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return out


# Состояния, которыми кампанию из кабинета уже не спрятать. Список полный
# намеренно: у вопроса «отдаёт ли API эту кампанию вообще» фильтр состояний —
# ложный отрицательный ответ. Замер 25.08.2026 (probe_blind_campaigns_api,
# run 32866947540) показал цену такого ответа: 12 из 15 кампаний, считавшихся
# слепой зоной, API отдавал — просто синк спрашивал их с фильтром
# ON/OFF/SUSPENDED/ENDED и не находил.
ALL_CAMPAIGN_STATES = ["ON", "OFF", "SUSPENDED", "ENDED", "CONVERTED", "ARCHIVED"]


def fetch_campaigns_by_ids(login: str, campaign_ids: List[Any],
                           ) -> Dict[str, Dict[str, Any]]:
    """Что кабинет знает о конкретных кампаниях: {campaign_id: {name, type, ...}}.

    Отличается от fetch_campaign_ids не формой, а вопросом. Тот спрашивает
    «кто есть в кабинете» и годится, чтобы перечислить объекты; этот
    спрашивает «знаешь ли ты вот эту» и годится, чтобы РАЗЛИЧИТЬ два случая,
    которые снаружи выглядят одинаково — кампания есть в кабинете, но не
    доехала до витрины (дефект синка, чинится кодом), и кампании нет в API
    вовсе (Мастер кампаний, чинится человеком).

    Различение не теоретическое. 25.08.2026 всю слепую зону списывали на
    Мастер кампаний, а замер по явным Id показал, что две трети её —
    обычные TEXT_CAMPAIGN, которые синк ронял на форме массивов. Вопрос без
    фильтра состояний задавать обязательно: с фильтром ответ «нет» означает
    «нет в этих состояниях», и оба случая снова сливаются.

    Отсутствующий в ответе Id — это НЕ ошибка и исключением не становится:
    именно молчание API и есть искомый факт. Кампания одного кабинета,
    спрошенная у другого, отвечает тем же молчанием, поэтому вызывающий
    обходит логины и складывает ответы (sync/agent/master.py).

    Форма запроса взята из probe_blind_campaigns_api.py, которым замер и
    сделан: гадать про формы запросов Директа в этом репозитории уже стоило
    восьми упавших прогонов (см. fetch_account_goal_ids).
    """
    ids = [int(cid) for cid in campaign_ids or ()]
    out: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(ids), PAGE_LIMIT):
        chunk = ids[start:start + PAGE_LIMIT]
        result = _api_post(
            CAMPAIGNS_URL,
            login,
            {
                "method": "get",
                "params": {
                    "SelectionCriteria": {"Ids": chunk,
                                          "States": ALL_CAMPAIGN_STATES},
                    "FieldNames": ["Id", "Name", "Type", "State", "Status"],
                    "Page": {"Limit": PAGE_LIMIT},
                },
            },
            "campaigns.get by ids",
        )
        for campaign in result.get("Campaigns") or []:
            out[str(campaign.get("Id"))] = {
                "login": login,
                "campaign_name": campaign.get("Name"),
                "campaign_type": campaign.get("Type"),
                "state": campaign.get("State"),
                "status": campaign.get("Status"),
            }
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
    login: str, date_from: str, date_to: str, goals: List[str] = (),
    excluded_campaign_ids: Iterable[Any] = (),
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
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
    # Чужие строки — до выбора цели: расход и конверсии ФРАЗЫ складываются по
    # всем её кампаниям (objects.py), а минус-слово пишется в свои. Строка
    # чужой РК решала бы порог, по которому режут собственную семантику.
    records = _own_records(_run_report(login, payload), excluded_campaign_ids)
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
    ], chosen_goal(records, goal_column)


def fetch_placements(
    login: str, date_from: str, date_to: str, goals: List[str] = (),
    excluded_campaign_ids: Iterable[Any] = (),
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Площадки сети за окно, агрегат без дат.

    Отдельный отчёт (PLACEMENT_PERFORMANCE_REPORT): в сегментных срезах
    площадок нет, а рычаг запрета площадок без них не построить. Строки
    поисковой сети отбрасываются — «площадка» там означает саму выдачу
    Яндекса, запрещать её нечем и незачем.

    Без goals колонка конверсий недоступна, и правило «расход без конверсий»
    вырождается ровно так же, как у поисковых запросов.
    """
    fields = ["CampaignId", "Placement", "AdNetworkType", "Cost", "Clicks",
              "Impressions"]
    if goals:
        fields.append("Conversions")
    payload = {
        "params": _with_goals({
            "SelectionCriteria": {
                "DateFrom": date_from,
                "DateTo": date_to,
                "Filter": [{"Field": "Clicks", "Operator": "GREATER_THAN",
                            "Values": ["0"]}],
            },
            "FieldNames": fields,
            "ReportName": f"agent-placements-{date_from}-{date_to}",
            # CUSTOM_REPORT, а не выдуманный PLACEMENT_PERFORMANCE_REPORT:
            # такого типа в API v5 нет (боевой прогон получил 8000
            # «ReportType содержит неверное значение перечисления»). Поля
            # Placement и AdNetworkType живут в CUSTOM_REPORT — там же, где
            # их берут сегментные срезы.
            "ReportType": "CUSTOM_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "NO",
        }, list(goals))
    }
    # Та же причина, что у запросов: площадка запрещается в своих кампаниях,
    # значит и считаться должна по ним.
    records = _own_records(_run_report(login, payload), excluded_campaign_ids)
    goal_column = primary_goal_column(records)
    rows = [
        {
            "window_from": date_from,
            "window_to": date_to,
            "campaign_id": rec.get("CampaignId", ""),
            "placement": rec.get("Placement", ""),
            "cost": float(rec.get("Cost") or 0.0),
            "clicks": _cell_int(rec.get("Clicks")),
            "impressions": _cell_int(rec.get("Impressions")),
            "conversions": _cell_int(rec.get(goal_column)) if goal_column else 0,
        }
        for rec in records
        if str(rec.get("AdNetworkType") or "").upper() == "AD_NETWORK"
        and rec.get("Placement")
    ]
    return rows, chosen_goal(records, goal_column)
