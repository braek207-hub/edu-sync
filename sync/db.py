import os
import time
from contextlib import contextmanager
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse

import psycopg2
import psycopg2.extras


def _database_url() -> str:
    url = os.environ["DATABASE_URL"]
    # Prisma pooler URI (?pgbouncer=true) — psycopg2 не понимает этот query-параметр
    if "pgbouncer=" in url:
        base, _, qs = url.partition("?")
        if qs:
            kept = "&".join(
                p for p in qs.split("&") if p and not p.startswith("pgbouncer=")
            )
            url = f"{base}?{kept}" if kept else base
    return url


def ensure_schema() -> None:
    """Миграции схемы под GAS (метрики Директа, сегменты CRM)."""
    statements = [
        """
        ALTER TABLE direct_stats
          ADD COLUMN IF NOT EXISTS w_avg_eff_bid DOUBLE PRECISION NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS w_avg_traffic_vol DOUBLE PRECISION NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS w_avg_impr_pos DOUBLE PRECISION NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS w_avg_click_pos DOUBLE PRECISION NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS w_auction_win_share DOUBLE PRECISION NOT NULL DEFAULT 0
        """,
        """
        ALTER TABLE crm_leads
          ADD COLUMN IF NOT EXISTS city_ip_segment TEXT NOT NULL DEFAULT 'rf',
          ADD COLUMN IF NOT EXISTS b24_grad_year TEXT NOT NULL DEFAULT 'unknown',
          ADD COLUMN IF NOT EXISTS b24_edu_level TEXT NOT NULL DEFAULT 'unknown',
          ADD COLUMN IF NOT EXISTS payments_from_leads INTEGER NOT NULL DEFAULT 0,
          ADD COLUMN IF NOT EXISTS revenue_from_leads DOUBLE PRECISION NOT NULL DEFAULT 0
        """,
        """
        ALTER TABLE crm_payments
          ADD COLUMN IF NOT EXISTS city_ip_segment TEXT NOT NULL DEFAULT 'rf',
          ADD COLUMN IF NOT EXISTS b24_grad_year TEXT NOT NULL DEFAULT 'unknown',
          ADD COLUMN IF NOT EXISTS b24_edu_level TEXT NOT NULL DEFAULT 'unknown'
        """,
        """
        ALTER TABLE strategy_snapshots
          ADD COLUMN IF NOT EXISTS serving TEXT NOT NULL DEFAULT ''
        """,
    ]
    with get_connection() as conn:
        with conn.cursor() as cur:
            for sql in statements:
                cur.execute(sql)
            cur.execute(
                """
                DO $$ BEGIN
                  ALTER TABLE crm_leads
                    DROP CONSTRAINT IF EXISTS crm_leads_date_campaign_id_key;
                EXCEPTION WHEN undefined_object THEN NULL;
                END $$;
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS crm_leads_segment_key
                ON crm_leads (date, campaign_id, city_ip_segment, b24_grad_year, b24_edu_level)
                """
            )
            cur.execute(
                """
                DO $$ BEGIN
                  ALTER TABLE crm_payments
                    DROP CONSTRAINT IF EXISTS crm_payments_date_campaign_id_key;
                EXCEPTION WHEN undefined_object THEN NULL;
                END $$;
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS crm_payments_segment_key
                ON crm_payments (date, campaign_id, city_ip_segment, b24_grad_year, b24_edu_level)
                """
            )
        conn.commit()


def _new_connection():
    parsed = urlparse(_database_url())
    if not parsed.hostname:
        return psycopg2.connect(_database_url())
    return psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        dbname=(parsed.path or "/postgres").lstrip("/") or "postgres",
        sslmode="require",
    )


def _open_connection(attempts: int = 4, delay: float = 2.0):
    """Открыть ЖИВОЕ соединение с ретраями.

    Первый коннект к пулеру Supabase в прогоне нередко приходит уже закрытым
    (пробуждение БД/пулера) — psycopg2.connect() проходит, но первый execute
    падает `connection already closed`. Поэтому проверяем коннект `SELECT 1`
    и при провале пересоздаём; следующая попытка попадает на «прогретый» пулер.
    """
    last_err = None
    for i in range(1, attempts + 1):
        conn = None
        try:
            conn = _new_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.rollback()  # завершить probe-транзакцию, отдать чистый коннект
            return conn
        except Exception as e:  # noqa: BLE001
            last_err = e
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            if i < attempts:
                print(f"  [db] коннект не удался ({i}/{attempts}): {e}; повтор через {delay}с")
                time.sleep(delay)
    raise last_err


@contextmanager
def get_connection():
    """Контекст-менеджер, который ГАРАНТИРОВАННО закрывает соединение.

    psycopg2 `with conn` сам по себе соединение НЕ закрывает (только commit/
    rollback транзакции), поэтому каждое `with get_connection()` оставляло
    открытый коннект. ensure_schema() + запись на каждый шаг = утечка ~2 на шаг,
    пул pgbouncer исчерпывался → `connection already closed`. Теперь закрываем.
    """
    conn = _open_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_direct_stats_from(date_from: str) -> int:
    """Удалить direct_stats с date >= date_from (перед перезагрузкой окна)."""
    ensure_schema()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM direct_stats WHERE date >= %s", (date_from,))
            deleted = cur.rowcount
        conn.commit()
    return deleted


def replace_direct_stats(rows: List[Dict[str, Any]]) -> int:
    """Полная перезапись direct_stats (как полный лист в GAS)."""
    if not rows:
        return 0
    ensure_schema()
    sql = """
        INSERT INTO direct_stats (
            date, campaign_id, campaign_name, project, direction,
            cost, clicks, impressions,
            w_avg_eff_bid, w_avg_traffic_vol, w_avg_impr_pos, w_avg_click_pos, w_auction_win_share
        )
        VALUES (
            %(date)s, %(campaign_id)s, %(campaign_name)s, %(project)s, %(direction)s,
            %(cost)s, %(clicks)s, %(impressions)s,
            %(w_avg_eff_bid)s, %(w_avg_traffic_vol)s, %(w_avg_impr_pos)s,
            %(w_avg_click_pos)s, %(w_auction_win_share)s
        )
        ON CONFLICT (date, campaign_id) DO UPDATE SET
            campaign_name = COALESCE(NULLIF(EXCLUDED.campaign_name, ''), direct_stats.campaign_name),
            project       = EXCLUDED.project,
            direction     = EXCLUDED.direction,
            cost          = EXCLUDED.cost,
            clicks        = EXCLUDED.clicks,
            impressions   = EXCLUDED.impressions,
            w_avg_eff_bid = EXCLUDED.w_avg_eff_bid,
            w_avg_traffic_vol = EXCLUDED.w_avg_traffic_vol,
            w_avg_impr_pos = EXCLUDED.w_avg_impr_pos,
            w_avg_click_pos = EXCLUDED.w_avg_click_pos,
            w_auction_win_share = EXCLUDED.w_auction_win_share
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE direct_stats RESTART IDENTITY")
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def upsert_direct_stats(rows: List[Dict[str, Any]]) -> int:
    """Инкрементальный upsert (API fallback)."""
    if not rows:
        return 0
    ensure_schema()
    for r in rows:
        r.setdefault("w_avg_eff_bid", 0)
        r.setdefault("w_avg_traffic_vol", 0)
        r.setdefault("w_avg_impr_pos", 0)
        r.setdefault("w_avg_click_pos", 0)
        r.setdefault("w_auction_win_share", 0)
    sql = """
        INSERT INTO direct_stats (
            date, campaign_id, campaign_name, project, direction,
            cost, clicks, impressions,
            w_avg_eff_bid, w_avg_traffic_vol, w_avg_impr_pos, w_avg_click_pos, w_auction_win_share
        )
        VALUES (
            %(date)s, %(campaign_id)s, %(campaign_name)s, %(project)s, %(direction)s,
            %(cost)s, %(clicks)s, %(impressions)s,
            %(w_avg_eff_bid)s, %(w_avg_traffic_vol)s, %(w_avg_impr_pos)s,
            %(w_avg_click_pos)s, %(w_auction_win_share)s
        )
        ON CONFLICT (date, campaign_id) DO UPDATE SET
            campaign_name = COALESCE(NULLIF(EXCLUDED.campaign_name, ''), direct_stats.campaign_name),
            project       = CASE
                WHEN EXCLUDED.project IS NOT NULL AND EXCLUDED.project <> 'unknown'
                THEN EXCLUDED.project
                ELSE direct_stats.project
            END,
            direction     = CASE
                WHEN EXCLUDED.direction IS NOT NULL AND EXCLUDED.direction <> 'other'
                THEN EXCLUDED.direction
                ELSE direct_stats.direction
            END,
            cost          = EXCLUDED.cost,
            clicks        = EXCLUDED.clicks,
            impressions   = EXCLUDED.impressions,
            w_avg_eff_bid = EXCLUDED.w_avg_eff_bid,
            w_avg_traffic_vol = EXCLUDED.w_avg_traffic_vol,
            w_avg_impr_pos = EXCLUDED.w_avg_impr_pos,
            w_avg_click_pos = EXCLUDED.w_avg_click_pos,
            w_auction_win_share = EXCLUDED.w_auction_win_share
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def replace_crm_leads(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    ensure_schema()
    sql = """
        INSERT INTO crm_leads (
            date, campaign_id, project, direction,
            city_ip_segment, b24_grad_year, b24_edu_level,
            leads, connections, deals, payments_from_leads, revenue_from_leads
        )
        VALUES (
            %(date)s, %(campaign_id)s, %(project)s, %(direction)s,
            %(city_ip_segment)s, %(b24_grad_year)s, %(b24_edu_level)s,
            %(leads)s, %(connections)s, %(deals)s, %(payments_from_leads)s, %(revenue_from_leads)s
        )
        ON CONFLICT (date, campaign_id, city_ip_segment, b24_grad_year, b24_edu_level)
        DO UPDATE SET
            project     = EXCLUDED.project,
            direction   = EXCLUDED.direction,
            leads       = EXCLUDED.leads,
            connections = EXCLUDED.connections,
            deals       = EXCLUDED.deals,
            payments_from_leads = EXCLUDED.payments_from_leads,
            revenue_from_leads = EXCLUDED.revenue_from_leads
    """
    # Заменяем только диапазон загружаемых дат (>= самой ранней даты в данных),
    # а не всю таблицу: историю вне диапазона (напр. 2025 при daily) сохраняем.
    min_date = min(str(r["date"]) for r in rows)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM crm_leads WHERE date >= %s", (min_date,))
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def upsert_crm_leads(rows: List[Dict[str, Any]]) -> int:
    return replace_crm_leads(rows)


def replace_monthly_plans(rows: List[Dict[str, Any]]) -> int:
    """Полная перезапись monthly_plans из листа (как GAS readPlanMonthly_)."""
    if not rows:
        return 0
    ensure_schema()
    sql = """
        INSERT INTO monthly_plans (month, project, direction, budget, leads, connections, deals, payments, revenue)
        VALUES (%(month)s, %(project)s, %(direction)s, %(budget)s,
                %(leads)s, %(connections)s, %(deals)s, %(payments)s, %(revenue)s)
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE monthly_plans RESTART IDENTITY")
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def upsert_monthly_plans(rows: List[Dict[str, Any]]) -> int:
    return replace_monthly_plans(rows)


def upsert_strategy_snapshots(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO strategy_snapshots (
            date, campaign_id, campaign_name, weekly_budget, target_cpa, state, status, serving
        )
        VALUES (
            %(date)s, %(campaign_id)s, %(campaign_name)s, %(weekly_budget)s,
            %(target_cpa)s, %(state)s, %(status)s, %(serving)s
        )
        ON CONFLICT (date, campaign_id) DO UPDATE SET
            campaign_name = EXCLUDED.campaign_name,
            weekly_budget = EXCLUDED.weekly_budget,
            target_cpa    = EXCLUDED.target_cpa,
            state         = EXCLUDED.state,
            status        = EXCLUDED.status,
            serving       = EXCLUDED.serving
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def ensure_dashboard_extras_table() -> None:
    ddl = """
        CREATE TABLE IF NOT EXISTS dashboard_extras (
            id TEXT PRIMARY KEY DEFAULT 'main',
            crm_leads_lite JSONB NOT NULL DEFAULT '[]',
            crm_payments_lite JSONB NOT NULL DEFAULT '[]',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()


def upsert_dashboard_extras(crm_leads_lite_json: str, crm_payments_lite_json: str) -> int:
    ensure_dashboard_extras_table()
    sql = """
        INSERT INTO dashboard_extras (id, crm_leads_lite, crm_payments_lite, updated_at)
        VALUES ('main', %s::jsonb, %s::jsonb, NOW())
        ON CONFLICT (id) DO UPDATE SET
            crm_leads_lite = EXCLUDED.crm_leads_lite,
            crm_payments_lite = EXCLUDED.crm_payments_lite,
            updated_at = NOW()
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (crm_leads_lite_json, crm_payments_lite_json))
        conn.commit()
    return 1


def replace_crm_payments(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    ensure_schema()
    sql = """
        INSERT INTO crm_payments (
            date, campaign_id, project, direction,
            city_ip_segment, b24_grad_year, b24_edu_level,
            payments, revenue
        )
        VALUES (
            %(date)s, %(campaign_id)s, %(project)s, %(direction)s,
            %(city_ip_segment)s, %(b24_grad_year)s, %(b24_edu_level)s,
            %(payments)s, %(revenue)s
        )
        ON CONFLICT (date, campaign_id, city_ip_segment, b24_grad_year, b24_edu_level)
        DO UPDATE SET
            project   = EXCLUDED.project,
            direction = EXCLUDED.direction,
            payments  = EXCLUDED.payments,
            revenue   = EXCLUDED.revenue
    """
    # Заменяем только диапазон дат загружаемых данных — историю (2025) сохраняем.
    min_date = min(str(r["date"]) for r in rows)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM crm_payments WHERE date >= %s", (min_date,))
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def upsert_crm_payments(rows: List[Dict[str, Any]]) -> int:
    return replace_crm_payments(rows)


def upsert_polinarepik_direct_stats(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO polinarepik_direct_stats (
            date, campaign_id, campaign_name, source_type, cost, clicks, impressions, updated_at
        )
        VALUES (
            %(date)s, %(campaign_id)s, %(campaign_name)s, %(source_type)s,
            %(cost)s, %(clicks)s, %(impressions)s, NOW()
        )
        ON CONFLICT (date, campaign_id) DO UPDATE SET
            campaign_name = COALESCE(NULLIF(EXCLUDED.campaign_name, ''), polinarepik_direct_stats.campaign_name),
            source_type   = EXCLUDED.source_type,
            cost          = EXCLUDED.cost,
            clicks        = EXCLUDED.clicks,
            impressions   = EXCLUDED.impressions,
            updated_at    = NOW()
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def delete_polinarepik_metrica_from(date_from: str) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM polinarepik_metrica_visits WHERE date >= %s",
                (date_from,),
            )
            deleted = cur.rowcount
        conn.commit()
    return deleted


def delete_polinarepik_metrica_purchases_from(date_from: str) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM polinarepik_metrica_purchases WHERE purchase_date >= %s",
                (date_from,),
            )
            deleted = cur.rowcount
        conn.commit()
    return deleted


def upsert_polinarepik_metrica_visits(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO polinarepik_metrica_visits (
            date, client_id, traffic_source, utm_source, utm_medium, utm_campaign, visits, updated_at
        )
        VALUES (
            %(date)s, %(client_id)s, %(traffic_source)s, %(utm_source)s,
            %(utm_medium)s, %(utm_campaign)s, %(visits)s, NOW()
        )
        ON CONFLICT (date, client_id, utm_campaign, utm_source, utm_medium) DO UPDATE SET
            traffic_source = EXCLUDED.traffic_source,
            visits         = EXCLUDED.visits,
            updated_at     = NOW()
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def upsert_polinarepik_metrica_purchases(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO polinarepik_metrica_purchases (
            order_id, purchase_date, client_id, traffic_source, utm_source,
            utm_medium, utm_campaign, purchases, revenue, updated_at
        )
        VALUES (
            %(order_id)s, %(purchase_date)s, %(client_id)s, %(traffic_source)s, %(utm_source)s,
            %(utm_medium)s, %(utm_campaign)s, %(purchases)s, %(revenue)s, NOW()
        )
        ON CONFLICT (order_id) DO UPDATE SET
            purchase_date  = EXCLUDED.purchase_date,
            client_id      = COALESCE(NULLIF(EXCLUDED.client_id, ''), polinarepik_metrica_purchases.client_id),
            traffic_source = EXCLUDED.traffic_source,
            utm_source     = EXCLUDED.utm_source,
            utm_medium     = EXCLUDED.utm_medium,
            utm_campaign   = EXCLUDED.utm_campaign,
            purchases      = EXCLUDED.purchases,
            revenue        = EXCLUDED.revenue,
            updated_at     = NOW()
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


# ── Журнал офлайн-конверсий Метрики (дедуп: каждую цель грузим один раз) ──

def ensure_metrika_table() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS metrika_uploaded_conversions (
                    counter_id TEXT NOT NULL,
                    client_id  TEXT NOT NULL,
                    target     TEXT NOT NULL,
                    event_ts   BIGINT,
                    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (counter_id, client_id, target)
                )
                """
            )
        conn.commit()


def load_uploaded_conversion_keys() -> set:
    ensure_metrika_table()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT counter_id, client_id, target FROM metrika_uploaded_conversions")
            return {(r[0], r[1], r[2]) for r in cur.fetchall()}


def record_uploaded_conversions(rows: List[tuple]) -> int:
    """rows: [(counter_id, client_id, target, event_ts)]."""
    if not rows:
        return 0
    sql = """
        INSERT INTO metrika_uploaded_conversions (counter_id, client_id, target, event_ts)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (counter_id, client_id, target) DO NOTHING
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)
