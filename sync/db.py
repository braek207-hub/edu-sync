import os
from typing import Any, Dict, List

import psycopg2
import psycopg2.extras


def get_connection():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def upsert_direct_stats(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO direct_stats (date, campaign_id, campaign_name, project, direction, cost, clicks, impressions)
        VALUES (%(date)s, %(campaign_id)s, %(campaign_name)s, %(project)s, %(direction)s,
                %(cost)s, %(clicks)s, %(impressions)s)
        ON CONFLICT (date, campaign_id) DO UPDATE SET
            campaign_name = EXCLUDED.campaign_name,
            project       = EXCLUDED.project,
            direction     = EXCLUDED.direction,
            cost          = EXCLUDED.cost,
            clicks        = EXCLUDED.clicks,
            impressions   = EXCLUDED.impressions
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def upsert_crm_leads(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO crm_leads (date, campaign_id, project, direction, leads, connections, deals)
        VALUES (%(date)s, %(campaign_id)s, %(project)s, %(direction)s,
                %(leads)s, %(connections)s, %(deals)s)
        ON CONFLICT (date, campaign_id) DO UPDATE SET
            project     = EXCLUDED.project,
            direction   = EXCLUDED.direction,
            leads       = EXCLUDED.leads,
            connections = EXCLUDED.connections,
            deals       = EXCLUDED.deals
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)


def upsert_crm_payments(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    sql = """
        INSERT INTO crm_payments (date, campaign_id, project, direction, payments, revenue)
        VALUES (%(date)s, %(campaign_id)s, %(project)s, %(direction)s,
                %(payments)s, %(revenue)s)
        ON CONFLICT (date, campaign_id) DO UPDATE SET
            project   = EXCLUDED.project,
            direction = EXCLUDED.direction,
            payments  = EXCLUDED.payments,
            revenue   = EXCLUDED.revenue
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
    return len(rows)
