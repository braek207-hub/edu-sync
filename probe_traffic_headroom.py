# -*- coding: utf-8 -*-
"""
probe_traffic_headroom.py — чем меряется недобор трафика в кампаниях EDU.

Три вопроса, все закрываются фактом, а не рассуждением:

1. Принимает ли Reports API поле доли выкупа. В списке полей
   (https://yandex.ru/dev/direct/doc/ru/fields-list) его нет, но список бывает
   неполон, а колонка edu_agent_facts.auction_win_share существует и пуста —
   значит когда-то данные откуда-то брались (из Google-таблицы,
   sync/direct_sheets.py:61-62). Пробуем кандидатов по одному: поле, которого
   нет, API отвергает запрос целиком, поэтому спрашивать их пачкой бессмысленно.
2. Заполнен ли AvgTrafficVolume — поле, которое в списке ЕСТЬ и которое
   sync/direct.py:26 уже пишет в direct_stats.w_avg_traffic_vol. Если оно
   живое, недобор трафика считается из имеющихся данных и ждать нечего.
3. Заполнен ли он ВЕЗДЕ ИЛИ ТОЛЬКО НА ПОИСКЕ. Объём трафика определён для
   поисковых показов; в сетях API отдаёт прочерк, а sync/direct.py:117
   (to_num_gas) превращает прочерк в ноль — признак «поля не было» до витрины
   не доезжает вовсе. Прочитанный как «недобор 100 %», такой ноль объявил бы
   всю сетевую часть кабинета недоливаемой. Поэтому заполненность меряется
   В РАЗРЕЗЕ типа кампании и канала показа, а не одним числом по кабинету.

Тип кампании в direct_stats не хранится. Единственный его источник в этом
репозитории — витрина edu_campaign_settings (settings #>> '{meta,campaignType}',
наполняет sync/edu_direct_settings.py:635). Это снимок ТЕКУЩИХ настроек, а не
состояние на день статистики: кампания, сменившая тип за 30 дней, попадёт в
разрез по нынешнему типу. edu_agent_objects типа не содержит — там только
уровни adgroup/keyword/ad (sync/agent_e0.py:687).

Скрипт read-only: пишет только в stdout.

Запуск: python probe_traffic_headroom.py
ENV: DIRECT_TOKEN, DIRECT_CLIENTS_JSON (или DIRECT_CLIENT_LOGIN), DATABASE_URL
"""

import json
import os
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

import requests

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"

# Кандидаты на «долю выкупа». Первые три — как поле могло бы называться по
# аналогии с колонками выгрузки интерфейса (sync/direct_sheets.py:61-62),
# AvgTrafficVolume — заведомо существующий контроль: если и он вернёт
# FIELD_UNKNOWN, сломан probe, а не API.
CANDIDATES = [
    "ImpressionShare",
    "SearchImpressionShare",
    "AuctionWinShare",
    "AvgTrafficVolume",
]

FIELD_ERROR_MARKERS = ("FieldNames", "FieldName")

# Окно замера заполненности витрины. 30 дней — то же окно, на котором агент
# отбирает кандидатов рычагов (sync/agent/objects.py::CANDIDATE_WINDOW_DAYS).
DB_WINDOW_DAYS = 30

# Стратегия канала, означающая «в этом канале кампания не показывается»
# (константа API Директа, встречается в снимках стратегий: Search/Network
# BiddingStrategyType = SERVING_OFF).
SERVING_OFF = "SERVING_OFF"

UNKNOWN_TYPE = "неизвестно"


def field_verdict(status: int, body: str) -> str:
    """Вердикт по одному полю: принято, не существует, иная ошибка."""
    if status in (200, 201, 202):
        return "OK"
    try:
        error = (json.loads(body) or {}).get("error") or {}
    except ValueError:
        return f"ERROR:http{status}"
    code = error.get("error_code")
    detail = f"{error.get('error_detail', '')} {error.get('error_string', '')}"
    if any(marker in detail for marker in FIELD_ERROR_MARKERS):
        return "FIELD_UNKNOWN"
    return f"ERROR:{code}"


def placement_mode(search_type: Optional[str], network_type: Optional[str]) -> str:
    """Где кампания показывается — по типам стратегий обоих каналов.

    Объём трафика определён для поиска, поэтому «только сети» — ожидаемое место
    пустого поля. Строки без настроек (кампании нет в edu_campaign_settings)
    остаются неизвестными: приписать им поиск значило бы выдать незнание за знание.
    """
    search_on = bool(search_type) and search_type != SERVING_OFF
    network_on = bool(network_type) and network_type != SERVING_OFF
    if search_on and network_on:
        return "поиск+сети"
    if search_on:
        return "только поиск"
    if network_on:
        return "только сети"
    return UNKNOWN_TYPE


def bucket_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Свод по группе кампаний.

    avg_traffic_volume — среднее, взвешенное ПОКАЗАМИ: в витрине лежит
    w_avg_traffic_vol = значение × показы (sync/direct.py:117).
    volume_coverage — доля показов, пришедшихся на дни с НЕНУЛЕВЫМ объёмом.
    Именно она отличает «объём измерен и низок» от «поля не было»: у второго
    покрытие нулевое при живых показах.
    """
    impressions = sum(int(r.get("impressions") or 0) for r in rows)
    with_volume = sum(int(r.get("impressions_with_volume") or 0) for r in rows)
    weighted = sum(float(r.get("traffic_weighted") or 0.0) for r in rows)
    win_weighted = sum(float(r.get("win_weighted") or 0.0) for r in rows)
    return {
        "campaigns": len(rows),
        "days": sum(int(r.get("days") or 0) for r in rows),
        "days_with_volume": sum(int(r.get("days_with_volume") or 0) for r in rows),
        "days_with_win_share": sum(int(r.get("days_with_win") or 0) for r in rows),
        "impressions": impressions,
        "impressions_with_volume": with_volume,
        "volume_coverage": (round(with_volume / impressions, 4)
                            if impressions else None),
        "avg_traffic_volume": (round(weighted / impressions, 2)
                               if impressions else None),
        "avg_win_share": (round(win_weighted / impressions, 4)
                          if impressions else None),
    }


def group_stats(rows: List[Dict[str, Any]],
                key: Callable[[Dict[str, Any]], str]) -> Dict[str, Dict[str, Any]]:
    """Свод по группам, ключ группы задаётся функцией."""
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(key(row), []).append(row)
    return {name: bucket_stats(bucket)
            for name, bucket in sorted(buckets.items(),
                                       key=lambda kv: -sum(int(r.get("impressions") or 0)
                                                           for r in kv[1]))}


def _client_login() -> str:
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    if raw:
        for item in json.loads(raw):
            if isinstance(item, dict) and str(item.get("login", "")).strip():
                return str(item["login"]).strip()
    return os.environ["DIRECT_CLIENT_LOGIN"]


def probe_field(login: str, field: str, date_from: str, date_to: str) -> str:
    """Один запрос на одно поле. Режим offline: ответ нужен про поле, не про данные."""
    params = {
        "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
        "FieldNames": ["CampaignId", field],
        "ReportName": f"probe-headroom-{field}-{date_from}",
        "ReportType": "CUSTOM_REPORT",
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        "IncludeVAT": "YES",
        "IncludeDiscount": "NO",
    }
    resp = requests.post(
        REPORTS_URL,
        # Кириллица в ответе и в теле — только явный UTF-8, иначе на Windows
        # байты теряются по дороге.
        data=json.dumps({"params": params}, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ['DIRECT_TOKEN']}",
            "Client-Login": login,
            "Accept-Language": "ru",
            "processingMode": "offline",
            "returnMoneyInMicros": "false",
            "skipReportHeader": "true",
            "skipColumnHeader": "true",
            "skipReportSummary": "true",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=120,
    )
    resp.encoding = "utf-8"
    return field_verdict(resp.status_code, resp.text)


# Заполненность взвешенных колонок direct_stats по кампаниям окна, с типом
# кампании и стратегиями каналов из снимка настроек. LEFT JOIN обязателен:
# кампании без строки в edu_campaign_settings (архивные, чужие кабинеты) должны
# быть видны отдельной группой, а не выпасть из знаменателя.
_DB_SQL = """
    SELECT ds.campaign_id::text                                  AS campaign_id,
           max(ds.project)                                       AS project,
           count(*)                                              AS days,
           count(*) FILTER (WHERE coalesce(ds.w_avg_traffic_vol, 0) > 0)
                                                                 AS days_with_volume,
           count(*) FILTER (WHERE coalesce(ds.w_auction_win_share, 0) > 0)
                                                                 AS days_with_win,
           coalesce(sum(ds.impressions), 0)                      AS impressions,
           coalesce(sum(ds.impressions) FILTER (
               WHERE coalesce(ds.w_avg_traffic_vol, 0) > 0), 0)   AS impressions_with_volume,
           coalesce(sum(ds.w_avg_traffic_vol), 0)                AS traffic_weighted,
           coalesce(sum(ds.w_auction_win_share), 0)              AS win_weighted,
           cs.settings #>> '{meta,campaignType}'                 AS campaign_type,
           cs.settings #>> '{strategy,search,biddingStrategyType}'  AS search_type,
           cs.settings #>> '{strategy,network,biddingStrategyType}' AS network_type
    FROM direct_stats ds
    LEFT JOIN edu_campaign_settings cs
           ON cs.campaign_id::text = ds.campaign_id::text
    WHERE ds.date >= %s
    GROUP BY ds.campaign_id,
             cs.settings #>> '{meta,campaignType}',
             cs.settings #>> '{strategy,search,biddingStrategyType}',
             cs.settings #>> '{strategy,network,biddingStrategyType}'
"""


def load_db_rows(since: str) -> List[Dict[str, Any]]:
    """Строки витрины за окно. Единственное место, где probe ходит в БД."""
    import psycopg2.extras

    from sync.db import get_connection

    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_DB_SQL, (since,))
            return [dict(r) for r in cur.fetchall()]


def db_fill(rows: List[Dict[str, Any]], since: str) -> Dict[str, Any]:
    """Заполненность объёма трафика: всего и в разрезах."""
    return {
        "window_days": DB_WINDOW_DAYS,
        "since": since,
        "campaign_type_source":
            "edu_campaign_settings.settings#>>'{meta,campaignType}' — снимок "
            "ТЕКУЩИХ настроек кабинета, не состояние на день статистики; "
            "в direct_stats и в edu_agent_objects типа кампании нет",
        "totals": bucket_stats(rows),
        "by_campaign_type": group_stats(
            rows, lambda r: r.get("campaign_type") or UNKNOWN_TYPE),
        "by_placement_mode": group_stats(
            rows, lambda r: placement_mode(r.get("search_type"),
                                           r.get("network_type"))),
        "by_project": group_stats(rows, lambda r: str(r.get("project") or UNKNOWN_TYPE)),
    }


def main() -> int:
    login = _client_login()
    date_to = (date.today() - timedelta(days=1)).isoformat()
    date_from = (date.today() - timedelta(days=8)).isoformat()
    fields = {field: probe_field(login, field, date_from, date_to)
              for field in CANDIDATES}
    since = (date.today() - timedelta(days=DB_WINDOW_DAYS)).isoformat()
    print(json.dumps({"login": login, "window": [date_from, date_to],
                      "fields": fields, "db": db_fill(load_db_rows(since), since)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
