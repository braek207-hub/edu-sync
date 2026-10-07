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

Дата — московский день: Директ живёт по МСК, и снимок «на сегодня» осмыслен в его
сутках. Несколько прогонов за день (meshnflesh — раз в 3 часа) переписывают снимок дня.
"""

from typing import List, Optional

import psycopg2

# Таблица витрины — slug из кода (CABINETS в sync/direct_settings_cabinets.py и вызовы
# ниже), внешний ввод сюда не попадает; поэтому её имя подставляется в текст запроса.
SETTINGS_TABLES = {
    "edunetwork": "edu_campaign_settings",
    "lime": "lime_campaign_settings",
    "meshnflesh": "mnf_campaign_settings",
    "bjorn": "bjorn_campaign_settings",
    "polinarepik": "polinarepik_campaign_settings",
}

# Пути в settings — форма JSONB одна у всех витрин (общий разбор edu_direct_settings /
# lime_direct). У пакетной стратегии значения на поиске, у РСЯ-only — в network.
_SELECT_COLUMNS = """
           campaign_id,
           COALESCE(campaign_name, ''),
           COALESCE((settings #>> '{strategy,search,weeklyBudget}')::numeric,
                    (settings #>> '{strategy,network,weeklyBudget}')::numeric),
           COALESCE((settings #>> '{strategy,search,targetCpa}')::numeric,
                    (settings #>> '{strategy,network,targetCpa}')::numeric),
           COALESCE(settings #>> '{meta,state}', ''),
           COALESCE(settings #>> '{meta,status}', ''),
           COALESCE(settings #>> '{strategy,search,biddingStrategyType}',
                    settings #>> '{strategy,network,biddingStrategyType}', '')"""

_ON_CONFLICT = """
    ON CONFLICT (date, campaign_id) DO UPDATE SET
        campaign_name = EXCLUDED.campaign_name,
        weekly_budget = EXCLUDED.weekly_budget,
        target_cpa    = EXCLUDED.target_cpa,
        state         = EXCLUDED.state,
        status        = EXCLUDED.status,
        strategy_type = EXCLUDED.strategy_type"""

_HAS_DASHBOARD_COLUMN_SQL = """
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'strategy_snapshots'
      AND column_name = 'dashboard'
"""


def snapshot_sql(dashboard: str, with_dashboard_column: bool, only_ids: bool) -> str:
    """Текст INSERT … SELECT снимка. Ключ (date, campaign_id) общий для всех дашбордов:
    Id кампании в Директе глобален, одна кампания двум кабинетам не принадлежит.

    only_ids — снимок только кампаний этого прогона (LIME: в витрину пишут два кабинета,
    казахстанский — в тенге, и в рублёвый журнал его бюджеты не идут)."""
    table = SETTINGS_TABLES[dashboard]
    where = "\n    WHERE campaign_id = ANY(%(ids)s)" if only_ids else ""
    if with_dashboard_column:
        return f"""
    INSERT INTO strategy_snapshots (
        dashboard, date, campaign_id, campaign_name, weekly_budget, target_cpa, state, status,
        strategy_type
    )
    SELECT %(dashboard)s, (now() AT TIME ZONE 'Europe/Moscow')::date,{_SELECT_COLUMNS}
    FROM {table}{where}{_ON_CONFLICT},
        dashboard     = EXCLUDED.dashboard
"""
    if dashboard != "edunetwork":
        raise ValueError("без колонки dashboard снимок пишет только edunetwork")
    return f"""
    INSERT INTO strategy_snapshots (
        date, campaign_id, campaign_name, weekly_budget, target_cpa, state, status, strategy_type
    )
    SELECT (now() AT TIME ZONE 'Europe/Moscow')::date,{_SELECT_COLUMNS}
    FROM {table}{where}{_ON_CONFLICT}
"""


def write_snapshot(
    pg_url: str, dashboard: str, campaign_ids: Optional[List[str]] = None
) -> int:
    """Снимок витрины настроек `dashboard` на сегодня (МСК). Возвращает число строк;
    0 и предупреждение — если колонки dashboard ещё нет, а дашборд не EDU."""
    if dashboard not in SETTINGS_TABLES:
        raise ValueError(f"неизвестный дашборд для снимка настроек: {dashboard}")
    with psycopg2.connect(pg_url) as conn:
        with conn.cursor() as cur:
            cur.execute(_HAS_DASHBOARD_COLUMN_SQL)
            has_column = cur.fetchone() is not None
            if not has_column and dashboard != "edunetwork":
                # ::warning:: — аннотация GitHub Actions: прогон зелёный (настройки
                # записаны), но пропуск журнала виден в сводке, а не только в логе.
                print(
                    f"::warning::[strategy_snapshots] {dashboard}: колонки dashboard нет "
                    "(миграция Panda-BI 20261007120000 не накачена) — снимок пропущен"
                )
                return 0
            params = {"dashboard": dashboard, "ids": list(campaign_ids or [])}
            cur.execute(
                snapshot_sql(dashboard, has_column, only_ids=campaign_ids is not None),
                params,
            )
            n = cur.rowcount
        conn.commit()
    print(f"[strategy_snapshots] {dashboard}: снимок {n} строк")
    return n
