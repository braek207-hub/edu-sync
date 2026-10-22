import csv
import json
import os
import time
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Tuple

import requests

from sync.classify import detect_direction, detect_project, project_from_client
from sync.direct_reports_limit import MIN_POLL_SECONDS, is_rate_limited, poll_delay
from sync.utils import to_num_gas

DIRECT_API_URL = "https://api.direct.yandex.com/json/v5/reports"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 600
MAX_POLL_ATTEMPTS = 30

FIELD_NAMES = [
    "Date",
    "CampaignId",
    "CampaignName",
    "Impressions",
    "Clicks",
    "Cost",
    "AvgEffectiveBid",
    "AvgTrafficVolume",
    "AvgImpressionPosition",
    "AvgClickPosition",
]

DEFAULT_INCREMENTAL_DAYS = 7
DEFAULT_FULL_DATE_FROM = "2026-01-01"

# Цели-конверсии EDU по проектам (выбор Павла, ID из Метрики). В коде, не в секрете.
# vuz=ВУЗ; vse=ВсеКолледжи (Страница «Спасибо»); provuz=ПроВУЗ (Страница «Спасибо»).
EDU_CONVERSION_GOALS: Dict[str, List[str]] = {
    "vuz": ["360811375"],
    "vse": ["330070378"],
    "provuz": ["330389387"],
}

INTERNATIONAL_MARKERS = ("дубай", "дубае", "за границ", "зарубеж", "international", "оаэ")


def is_international_campaign(campaign_name: str) -> bool:
    s = str(campaign_name or "").lower()
    return any(m in s for m in INTERNATIONAL_MARKERS)


# Внутри кабинета vuz живут ленды со своей целью обучения (выбор Павла 22.10.2026):
# онлайн-школа учится на «Отправка формы тильда» (счётчик school.edunetwork.ru),
# обучение за границей — на «Спасибо контекст (international)» (счётчик edunetwork.ru).
# Цель кабинета для таких кампаний — ноль: ленд другой, и цель там не стреляет.
EDU_CAMPAIGN_GOALS: Dict[str, List[Tuple[Callable[[str], bool], List[str]]]] = {
    "vuz": [
        (lambda name: detect_project(name) == "eduschool", ["596548027"]),
        (is_international_campaign, ["670497292"]),
    ],
}


def cabinet_goals(project: str | None, fallback: List[str] | None = None) -> List[str]:
    """Все цели, которые нужны кабинету в отчёте: своя + цели лендов внутри него."""
    base = EDU_CONVERSION_GOALS.get(project or "") or list(fallback or [])
    extra = [g for _, goals in EDU_CAMPAIGN_GOALS.get(project or "", []) for g in goals]
    return list(dict.fromkeys(base + extra))


def campaign_goals(
    project: str | None, campaign_name: str, fallback: List[str] | None = None
) -> List[str]:
    """Цели, конверсии которых засчитываются кампании."""
    for match, goals in EDU_CAMPAIGN_GOALS.get(project or "", []):
        if match(campaign_name):
            return goals
    return EDU_CONVERSION_GOALS.get(project or "") or list(fallback or [])


def _report_fields(goals: List[str]) -> List[str]:
    """Поля отчёта: базовые + Conversions (это МЕТРИКА, не измерение → строки не множит).
    В ответе Conversions разворачивается в колонку на цель: Conversions_<цель>_LSCCD."""
    return FIELD_NAMES + (["Conversions"] if goals else [])


def _goal_of_column(column: str) -> str | None:
    parts = column.split("_")
    return parts[1] if len(parts) >= 3 and parts[0] == "Conversions" else None


def _direct_clients() -> List[dict]:
    raw_json = os.environ.get("DIRECT_CLIENTS_JSON", "").strip()
    if raw_json:
        clients = json.loads(raw_json)
        out: List[dict] = []
        for item in clients:
            login = str(item.get("login", "")).strip()
            if not login:
                continue
            goals = item.get("goal_ids") or item.get("goals") or []
            out.append(
                {
                    "login": login,
                    "goal_ids": [str(g) for g in goals],
                    "project": project_from_client(login, item),
                    "sheet_name": str(item.get("sheet_name", "")),
                }
            )
        if out:
            return out

    login = os.environ.get("DIRECT_CLIENT_LOGIN", "").strip()
    if login:
        return [{"login": login, "goal_ids": [], "project": None, "sheet_name": ""}]

    raise RuntimeError("Нужен DIRECT_CLIENTS_JSON или DIRECT_CLIENT_LOGIN")


def _report_headers(login: str) -> dict:
    token = os.environ["DIRECT_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Client-Login": login,
        "Accept-Language": "ru",
        # auto: Директ сам выбирает online (мгновенно) для малых отчётов или offline
        # для крупных. Явный online давал error 8312 на больших аккаунтах EDU.
        # Опрос — по retryIn, но не чаще лимита метода (sync/direct_reports_limit.py).
        "processingMode": "auto",
        "returnMoneyInMicros": "false",
        "skipReportHeader": "true",
        # Заголовок нужен: колонки конверсий называются по целям, и разбор по позиции
        # зависел бы от порядка, в котором Директ их развернёт.
        "skipColumnHeader": "false",
        "skipReportSummary": "true",
    }


def _parse_report_tsv(
    text: str, project: str | None = None, fallback_goals: List[str] | None = None
) -> List[Dict[str, Any]]:
    """TSV \u0441 \u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043a\u043e\u043c. \u041a\u043e\u043d\u0432\u0435\u0440\u0441\u0438\u0438 \u043a\u0430\u043c\u043f\u0430\u043d\u0438\u0438 \u2014 \u0441\u0443\u043c\u043c\u0430 \u043a\u043e\u043b\u043e\u043d\u043e\u043a \u0435\u0451 \u0446\u0435\u043b\u0435\u0439 (campaign_goals)."""
    rows: List[Dict[str, Any]] = []
    lines = [ln for ln in text.lstrip("\ufeff").splitlines() if ln.strip()]
    if not lines:
        return rows

    reader = csv.reader(lines, delimiter="\t")
    header = next(reader)
    goal_columns = {c: _goal_of_column(c) for c in header if c.startswith("Conversions")}
    for parts in reader:
        row = dict(zip(header, parts))
        campaign_id = str(row.get("CampaignId", "")).strip()
        if not campaign_id or campaign_id == "--":
            continue
        campaign_name = str(row.get("CampaignName", "")).strip()
        impressions = int(float(row.get("Impressions", 0) or 0))
        clicks = int(float(row.get("Clicks", 0) or 0))
        cost = float(row.get("Cost", 0) or 0)

        w_bid = to_num_gas(row.get("AvgEffectiveBid")) * clicks
        w_traffic = to_num_gas(row.get("AvgTrafficVolume")) * impressions
        w_impr = to_num_gas(row.get("AvgImpressionPosition")) * impressions
        w_click = to_num_gas(row.get("AvgClickPosition")) * clicks

        # \u041a\u043e\u043b\u043e\u043d\u043a\u0430 \u0431\u0435\u0437 \u0446\u0435\u043b\u0438 \u0432 \u0438\u043c\u0435\u043d\u0438 (\u00abConversions\u00bb) \u2014 \u043e\u0442\u0447\u0451\u0442 \u043f\u043e \u043e\u0434\u043d\u043e\u0439 \u0446\u0435\u043b\u0438, \u043e\u043d\u0430 \u0438 \u0437\u0430\u0441\u0447\u0438\u0442\u044b\u0432\u0430\u0435\u0442\u0441\u044f.
        own = set(campaign_goals(project, campaign_name, fallback_goals))
        conversions = sum(
            int(to_num_gas(row.get(col)))
            for col, goal in goal_columns.items()
            if goal is None or goal in own
        )

        rows.append(
            {
                "date": str(row.get("Date", "")).strip(),
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "project": detect_project(campaign_name),
                "direction": detect_direction(campaign_name),
                "cost": cost,
                "clicks": clicks,
                "impressions": impressions,
                "w_avg_eff_bid": w_bid,
                "w_avg_traffic_vol": w_traffic,
                "w_avg_impr_pos": w_impr,
                "w_avg_click_pos": w_click,
                "w_auction_win_share": 0.0,
                # LSC по целям кампании; 0, если целей нет.
                "conversions": conversions,
            }
        )
    return rows


def _report_body(login: str, date_from: str, date_to: str, goals: List[str]) -> dict:
    params: Dict[str, Any] = {
        "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
        "FieldNames": _report_fields(goals),
        "ReportName": f"edu_sync_{login}_{date_from}_{date_to}",
        "ReportType": "CUSTOM_REPORT",
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        "IncludeVAT": "YES",
        "IncludeDiscount": "NO",
    }
    if goals:
        params["Goals"] = goals
        params["AttributionModels"] = ["LSC"]
    return {"params": params}


def _fetch_report(
    login: str, date_from: str, date_to: str, goals: List[str], project: str | None = None
) -> List[Dict[str, Any]]:
    headers = _report_headers(login)
    body = _report_body(login, date_from, date_to, goals)

    resp = None
    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        try:
            resp = requests.post(
                DIRECT_API_URL,
                json=body,
                headers=headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except requests.exceptions.Timeout as e:
            print(f"  [{login}] timeout (попытка {attempt}/{MAX_POLL_ATTEMPTS}): {e}")
            time.sleep(min(60, 15 + attempt * 5))
            continue

        if resp.status_code == 200:
            break
        # 204 с пустым телом и без RequestId — ответ балансера Яндекса при лежащем API (сбой
        # 11.10.2026), не «данных нет»: повторяем, как офлайн-ожидание (retryIn у него нет).
        if resp.status_code in (201, 202, 204):
            retry_in = min(poll_delay(resp.headers.get("retryIn"), MIN_POLL_SECONDS), 60)
            print(f"  [{login}] ждём {retry_in}с (HTTP {resp.status_code})...")
            time.sleep(retry_in)
            continue
        if is_rate_limited(resp.status_code, resp.text):
            print(f"  [{login}] лимит опроса Reports API, ждём {MIN_POLL_SECONDS}с...")
            time.sleep(MIN_POLL_SECONDS)
            continue
        raise RuntimeError(
            f"Директ API [{login}] error {resp.status_code}: {resp.text[:500]}"
        )
    else:
        raise RuntimeError(f"Директ API [{login}]: превышено число попыток")

    return _parse_report_tsv(resp.text, project, goals)


def sync_direct_api_range(
    date_from: str, date_to: str, *, replace_from: bool = False
) -> int:
    """Загрузка Direct через API за период [date_from, date_to]."""
    clients = _direct_clients()
    print(f"Директ API: {date_from} — {date_to}, клиентов: {len(clients)}")

    all_rows: List[Dict[str, Any]] = []
    errors: List[str] = []
    for client in clients:
        login = client["login"]
        try:
            project = client.get("project")
            goals = cabinet_goals(project, client.get("goal_ids"))
            chunk = _fetch_report(login, date_from, date_to, goals, project)
            print(f"  [{login}] получено {len(chunk)} строк")
            all_rows.extend(chunk)
        except Exception as e:
            print(f"  [{login}] ОШИБКА: {e}")
            errors.append(f"{login}: {e}")

    # Ни один кабинет не загрузился — сбой, а не «нет данных»: раньше здесь был return 0, и
    # прогон, где у всех четырёх «Reports API: превышено число попыток», выходил с кодом 0
    # при витрине, застывшей на позавчера.
    if errors and len(errors) == len(clients):
        raise RuntimeError(f"Директ API: ни один кабинет не загрузился: {'; '.join(errors)}")
    # Full-перезагрузка удаляет окно у ВСЕХ кабинетов (delete_direct_stats_from без фильтра по
    # логину): при частичном сбое упавший кабинет потерял бы историю. Ничего не трогаем.
    if errors and replace_from:
        raise RuntimeError(
            "Директ API: full-перезагрузка отменена, удаление не выполнялось — "
            f"не загрузились: {'; '.join(errors)}"
        )

    all_rows = [r for r in all_rows if str(r.get("date", "")) >= date_from]
    written = 0
    if all_rows:
        from sync.db import delete_direct_stats_from, upsert_direct_stats

        if replace_from:
            deleted = delete_direct_stats_from(date_from)
            print(f"Директ API: удалено {deleted} строк с {date_from}")
        written = upsert_direct_stats(all_rows)

    # Частичный сбой: загрузившиеся кабинеты записаны (upsert чужие строки не задевает), но
    # прогон красный. WARN в логе зелёного прогона не читает никто, а сторож свежести кабинет
    # не видит: соседи двигают max(date) витрины, и расход одного направления застывает
    # незаметно — так три кабинета из четырёх месяцами стояли у настроек
    # (sync/edu_direct_settings.py). Кэш дашборда сбрасывается и при красном исходе.
    if errors:
        raise RuntimeError(
            f"Директ API: записано {written} строк, не загрузились кабинеты: {'; '.join(errors)}"
        )
    return written


def sync_direct_incremental(days_back: int = DEFAULT_INCREMENTAL_DAYS) -> int:
    """Триггер: последние N дней, upsert (по умолчанию 7)."""
    today = date.today()
    date_from = (today - timedelta(days=days_back)).isoformat()
    date_to = today.isoformat()
    print(f"Директ incremental: {days_back} дн. ({date_from} — {date_to})")
    return sync_direct_api_range(date_from, date_to, replace_from=False)


def sync_direct_full(date_from: str | None = None) -> int:
    """Полная перезагрузка периода с date_from по сегодня (delete + upsert)."""
    start = (date_from or os.environ.get("DIRECT_DATE_FROM") or DEFAULT_FULL_DATE_FROM).strip()
    today = date.today().isoformat()
    print(f"Директ full: {start} — {today}")
    return sync_direct_api_range(start, today, replace_from=True)


def sync_direct_backfill_monthly(
    date_from: str, date_to: str, *, pause_sec: float = 7.0
) -> int:
    """Upsert Direct за период, разбивая на календарные месяцы (без delete)."""
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if start > end:
        raise ValueError(f"date_from {date_from} > date_to {date_to}")

    total = 0
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        month_end = min(end, next_month - timedelta(days=1))
        chunk_from = max(start, cursor).isoformat()
        chunk_to = month_end.isoformat()
        print(f"Директ backfill monthly: {chunk_from} — {chunk_to}")
        n = sync_direct_api_range(chunk_from, chunk_to, replace_from=False)
        print(f"  upsert {n} строк")
        total += n
        if month_end >= end:
            break
        cursor = next_month
        if cursor <= end:
            time.sleep(pause_sec)
    print(f"Директ backfill monthly итого: {total} строк")
    return total


def sync_direct_all() -> int:
    """
    Только Яндекс Direct API.

    DIRECT_SYNC_MODE:
      - incremental (default) — последние DIRECT_DAYS_BACK дней (default 7)
      - full — с DIRECT_DATE_FROM (default 2026-01-01) по сегодня

    DIRECT_SOURCE=sheets — legacy, только если явно задан.
    """
    source = os.environ.get("DIRECT_SOURCE", "api").strip().lower()
    if source == "sheets":
        from sync.direct_sheets import sync_direct_sheets

        print("WARN: DIRECT_SOURCE=sheets (legacy), API отключён")
        return sync_direct_sheets()

    mode = os.environ.get("DIRECT_SYNC_MODE", "incremental").strip().lower()
    if mode in ("monthly_upsert", "backfill_monthly"):
        date_from = (
            os.environ.get("DIRECT_DATE_FROM") or DEFAULT_FULL_DATE_FROM
        ).strip()
        date_to = (
            os.environ.get("DIRECT_DATE_TO") or date.today().isoformat()
        ).strip()
        return sync_direct_backfill_monthly(date_from, date_to)
    if mode in ("full", "full_replace"):
        return sync_direct_full()
    days_back = int(os.environ.get("DIRECT_DAYS_BACK", str(DEFAULT_INCREMENTAL_DAYS)))
    return sync_direct_incremental(days_back=days_back)
