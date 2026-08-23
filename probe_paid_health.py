# -*- coding: utf-8 -*-
"""Разовый замер: куда делись оплаты из crm_lead_details.

Прогон e0 32638649803 показал maturity_days=0 и paid=0 по всем кампаниям
зрелого окна лестницы — то есть в 180-дневном окне НЕТ ни одного лида с
is_paid. При этом probe_economics (540 дней до 20.07) оплаты видел. Проба
отвечает, что в базе на самом деле: оплаты кончились в данных (сломан синк /
сдвиг колонки) или их не было в этом окне никогда.
"""

import json

import psycopg2.extras

from sync.db import get_connection


def q(cur, sql, params=()):
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def main() -> int:
    with get_connection() as conn:
        return _report(conn)


def _report(conn) -> int:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    monthly = q(cur, """
        SELECT date_trunc('month', created_date)::date::text AS m,
               COUNT(*) AS leads,
               COUNT(*) FILTER (WHERE is_paid) AS paid,
               COUNT(*) FILTER (WHERE is_deal) AS deals,
               COUNT(*) FILTER (WHERE payment_date IS NOT NULL) AS with_pay_date,
               COALESCE(SUM(amount) FILTER (WHERE is_paid), 0)::bigint AS paid_amount
        FROM crm_lead_details
        WHERE created_date >= CURRENT_DATE - 540
        GROUP BY 1 ORDER BY 1
    """)
    last_paid = q(cur, """
        SELECT created_date::text, payment_date::text, amount, direction
        FROM crm_lead_details
        WHERE is_paid
        ORDER BY created_date DESC
        LIMIT 10
    """)
    flags_vs_date = q(cur, """
        SELECT COUNT(*) FILTER (WHERE is_paid AND payment_date IS NULL) AS paid_no_date,
               COUNT(*) FILTER (WHERE NOT is_paid AND payment_date IS NOT NULL) AS date_no_flag,
               MAX(payment_date)::text AS max_payment_date,
               MAX(created_date) FILTER (WHERE is_paid)::text AS max_paid_created
        FROM crm_lead_details
    """)

    print(json.dumps({
        "monthly_540d": monthly,
        "last_10_paid_by_created": last_paid,
        "flags_vs_payment_date": flags_vs_date[0],
    }, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
