import os
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
          ADD COLUMN IF NOT EXISTS b24_edu_level TEXT NOT NULL DEFAULT 'unknown'
        """,
        """
        ALTER TABLE crm_payments
          ADD COLUMN IF NOT EXISTS city_ip_segment TEXT NOT NULL DEFAULT 'rf',
          ADD COLUMN IF NOT EXISTS b24_grad_year TEXT NOT NULL DEFAULT 'unknown',
          ADD COLUMN IF NOT EXISTS b24_edu_level TEXT NOT NULL DEFAULT 'unknown'
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


def get_connection():
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
            leads, connections, deals
        )
        VALUES (
            %(date)s, %(campaign_id)s, %(project)s, %(direction)s,
            %(city_ip_segment)s, %(b24_grad_year)s, %(b24_edu_level)s,
            %(leads)s, %(connections)s, %(deals)s
        )
        ON CONFLICT (date, campaign_id, city_ip_segment, b24_grad_year, b24_edu_level)
        DO UPDATE SET
            project     = EXCLUDED.project,
            direction   = EXCLUDED.direction,
            leads       = EXCLUDED.leads,
            connections = EXCLUDED.connections,
            deals       = EXCLUDED.deals
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE crm_leads RESTART IDENTITY")
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def upsert_crm_leads(rows: List[Dict[str, Any]]) -> int:
    return replace_crm_leads(rows)


def upsert_monthly_plans(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO monthly_plans (month, project, direction, budget, leads, connections, deals, payments, revenue)
        VALUES (%(month)s, %(project)s, %(direction)s, %(budget)s,
                %(leads)s, %(connections)s, %(deals)s, %(payments)s, %(revenue)s)
        ON CONFLICT (month, project, direction) DO UPDATE SET
            budget      = EXCLUDED.budget,
            leads       = EXCLUDED.leads,
            connections = EXCLUDED.connections,
            deals       = EXCLUDED.deals,
            payments    = EXCLUDED.payments,
            revenue     = EXCLUDED.revenue
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def upsert_strategy_snapshots(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO strategy_snapshots (date, campaign_id, campaign_name, weekly_budget, target_cpa, state, status)
        VALUES (%(date)s, %(campaign_id)s, %(campaign_name)s, %(weekly_budget)s,
                %(target_cpa)s, %(state)s, %(status)s)
        ON CONFLICT (date, campaign_id) DO UPDATE SET
            campaign_name = EXCLUDED.campaign_name,
            weekly_budget = EXCLUDED.weekly_budget,
            target_cpa    = EXCLUDED.target_cpa,
            state         = EXCLUDED.state,
            status        = EXCLUDED.status
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
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE crm_payments RESTART IDENTITY")
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def upsert_crm_payments(rows: List[Dict[str, Any]]) -> int:
    return replace_crm_payments(rows)
