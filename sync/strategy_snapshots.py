# -*- coding: utf-8 -*-
"""
sync/strategy_snapshots.py — дневной снимок настроек кампаний в strategy_snapshots.

Из снимков Panda-BI строит журнал изменений и флажки на графике (app/api/chart-marks:
дифф соседних снимков по кампании → остановки, запуски, бюджет, CPA, стратегия). Снимок
берётся из витрины настроек дашборда (<x>_campaign_settings, её пишет синк настроек
этого кабинета) сразу после того, как витрина обновилась.

Чей снимок — колонка `dashboard` (slug дашборда Panda-BI). До миграции Panda-BI
20261007120000_change_journal_all_dashboards колонки нет, и таблица целиком принадлежит
EDU: тогда EDU пишет прежней формой, а остальные дашборды снимок пропускают с
предупреждением — иначе их кампании легли бы в таблицу как EDU-шные.

Строка снимка собирается в Python (snapshot_row), а не выражением в SQL: стратегию надо
выбрать по каналу, который реально показывается, цену у стратегии «несколько целей»
взять из приоритетных целей, а деньги казахстанского кабинета пересчитать в рубли.

Дата — московский день: Директ живёт по МСК, и снимок «на сегодня» осмыслен в его
сутках. Несколько прогонов за день (meshnflesh — раз в 3 часа) переписывают снимок дня.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

# Таблица витрины — slug из кода (CABINETS в sync/direct_settings_cabinets.py и вызовы
# ниже), внешний ввод сюда не попадает; поэтому её имя подставляется в текст запроса.
SETTINGS_TABLES = {
    "edunetwork": "edu_campaign_settings",
    "lime": "lime_campaign_settings",
    "meshnflesh": "mnf_campaign_settings",
    "bjorn": "bjorn_campaign_settings",
    "polinarepik": "polinarepik_campaign_settings",
}

# МСК — фиксированные UTC+3: переходов на летнее время нет с 2014 года.
MSK = timezone(timedelta(hours=3))

# Канал с таким типом не показывается: у кампании «только РСЯ» поиск = SERVING_OFF, и
# стратегия, бюджет и цена живут в network. Пустой тип — канала нет в ответе API.
_NOT_SERVING = ("", "SERVING_OFF")

_COLUMNS_SQL = """
    SELECT column_name FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'strategy_snapshots'
      AND column_name IN ('dashboard', 'target_crr')
"""


def msk_today() -> date:
    return datetime.now(MSK).date()


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _channel_type(ch: Dict[str, Any]) -> str:
    return str(ch.get("biddingStrategyType") or "")


def serving_channel(strategy: Dict[str, Any]) -> Dict[str, Any]:
    """Канал, чья стратегия определяет показы: поиск, если он показывается, иначе сеть.

    Поиск первым: при NETWORK_DEFAULT сеть повторяет поиск, а своих значений у неё нет.
    Оба канала выключены — поиск (SERVING_OFF), это и есть честное «показы отключены».
    """
    search = strategy.get("search") or {}
    network = strategy.get("network") or {}
    if _channel_type(search) not in _NOT_SERVING:
        return search
    if _channel_type(network) not in _NOT_SERVING:
        return network
    return search if _channel_type(search) else network


def _max_goal_price(strategy: Dict[str, Any]) -> Optional[float]:
    """Цена самой дорогой приоритетной цели — у стратегии «несколько целей» общей CPA
    нет, цена задаётся на каждой цели (PriorityGoals.Value). Самая дорогая — главная
    цель кампании (заказ, покупка), её правка и есть «целевой CPA изменён»."""
    prices = [
        p
        for p in (
            _num(g.get("bidValue"))
            for g in strategy.get("priorityGoalsDetails") or []
            if isinstance(g, dict)
        )
        if p is not None and p > 0
    ]
    return max(prices) if prices else None


def _money(v: Optional[float], factor: float) -> Optional[float]:
    return None if v is None else round(v * factor, 2)


def snapshot_row(
    campaign_id: str,
    campaign_name: Optional[str],
    settings: Dict[str, Any],
    money_factor: float = 1.0,
) -> Optional[Dict[str, Any]]:
    """Строка снимка из JSONB витрины настроек (форма одна у edu_direct_settings и
    lime_direct). None — у кампании нет состояния: API её не отдал (РМП нового
    интерфейса, Мастер кампаний), и в журнал ей нечего сказать.

    money_factor — множитель денег в рубли (кабинет LIME KZ ведётся в тенге)."""
    meta = settings.get("meta") or {}
    state = str(meta.get("state") or "")
    if not state:
        return None
    strategy = settings.get("strategy") or {}
    ch = serving_channel(strategy)
    package = strategy.get("package") or {}

    strategy_type = _channel_type(ch) or str(package.get("type") or "")
    weekly = _num(ch.get("weeklyBudget"))
    if weekly is None:
        weekly = _num(package.get("weeklyBudget"))

    target_cpa: Optional[float] = None
    target_crr: Optional[float] = None
    if "CRR" in strategy_type.upper():
        # ДРР — проценты, не рубли: в target_cpa (журнал пишет его с «₽») ему не место.
        target_crr = _num(ch.get("targetDrr"))
        if target_crr is None:
            target_crr = _num(package.get("targetDrr"))
    else:
        target_cpa = _num(ch.get("targetCpa"))
        if target_cpa is None:
            target_cpa = _num(package.get("targetCpa"))
        if target_cpa is None and "MULTIPLE_GOALS" in strategy_type.upper():
            target_cpa = _max_goal_price(strategy)

    return {
        "campaign_id": str(campaign_id),
        "campaign_name": campaign_name or "",
        "weekly_budget": _money(weekly, money_factor),
        "target_cpa": _money(target_cpa, money_factor),
        "target_crr": target_crr,
        "state": state,
        "status": str(meta.get("status") or ""),
        "strategy_type": strategy_type,
    }


def select_sql(dashboard: str, only_ids: bool) -> str:
    """only_ids — снимок только кампаний этого прогона: в lime_campaign_settings пишут
    два кабинета (RU в рублях, KZ в тенге), и каждый снимает свои с своим курсом."""
    table = SETTINGS_TABLES[dashboard]
    where = "\n    WHERE campaign_id = ANY(%(ids)s)" if only_ids else ""
    return f"""
    SELECT campaign_id, campaign_name, settings
    FROM {table}{where}
"""


def insert_sql(with_dashboard_column: bool, with_crr_column: bool) -> str:
    """Ключ (date, campaign_id) общий для всех дашбордов: Id кампании в Директе
    глобален, одна кампания двум кабинетам не принадлежит."""
    cols = ["date", "campaign_id", "campaign_name", "weekly_budget", "target_cpa",
            "state", "status", "strategy_type"]
    if with_crr_column:
        cols.append("target_crr")
    if with_dashboard_column:
        cols.insert(0, "dashboard")
    values = ", ".join(f"%({c})s" for c in cols)
    updates = ",\n        ".join(
        f"{c} = EXCLUDED.{c}" for c in cols if c not in ("date", "campaign_id")
    )
    return f"""
    INSERT INTO strategy_snapshots ({", ".join(cols)})
    VALUES ({values})
    ON CONFLICT (date, campaign_id) DO UPDATE SET
        {updates}
"""


def write_snapshot(
    pg_url: str,
    dashboard: str,
    campaign_ids: Optional[List[str]] = None,
    money_factor: float = 1.0,
) -> int:
    """Снимок витрины настроек `dashboard` на сегодня (МСК). Возвращает число строк;
    0 и предупреждение — если колонки dashboard ещё нет, а дашборд не EDU.

    Целевой ДРР пишется в target_crr, только когда колонка есть (миграция Panda-BI —
    по апруву владельца); до неё ДРР в снимок не попадает, остальное пишется как было."""
    if dashboard not in SETTINGS_TABLES:
        raise ValueError(f"неизвестный дашборд для снимка настроек: {dashboard}")
    day = msk_today()
    with psycopg2.connect(pg_url) as conn:
        with conn.cursor() as cur:
            cur.execute(_COLUMNS_SQL)
            columns = {r[0] for r in cur.fetchall()}
            has_dashboard = "dashboard" in columns
            if not has_dashboard and dashboard != "edunetwork":
                # ::warning:: — аннотация GitHub Actions: прогон зелёный (настройки
                # записаны), но пропуск журнала виден в сводке, а не только в логе.
                print(
                    f"::warning::[strategy_snapshots] {dashboard}: колонки dashboard нет "
                    "(миграция Panda-BI 20261007120000 не накачена) — снимок пропущен"
                )
                return 0
            only_ids = campaign_ids is not None
            cur.execute(select_sql(dashboard, only_ids), {"ids": list(campaign_ids or [])})
            rows = []
            skipped = 0
            for cid, name, settings in cur.fetchall():
                row = snapshot_row(cid, name, settings or {}, money_factor)
                if row is None:
                    skipped += 1
                    continue
                rows.append({**row, "dashboard": dashboard, "date": day})
            if rows:
                psycopg2.extras.execute_batch(
                    cur, insert_sql(has_dashboard, "target_crr" in columns), rows, page_size=500
                )
        conn.commit()
    tail = f", без состояния пропущено {skipped}" if skipped else ""
    print(f"[strategy_snapshots] {dashboard}: снимок {len(rows)} строк{tail}")
    return len(rows)
