# -*- coding: utf-8 -*-
"""
probe_crm_lag.py — НАСКОЛЬКО ПОЗЖЕ лида он появляется в базе.

Зачем. Автопилот судит об изменении по CPA на эффективных лидах, а лиды
приезжают из CRM позже расхода. Смещение систематическое и всегда в одну
сторону: расход дня уже полон, лидов ещё нет — наблюдаемый CPA завышен, и
сторож может откатить здоровое изменение. Величина запаса (LEADS_LAG_DAYS)
до сих пор была ДОГАДКОЙ — «двое суток, заведомый запас».

Здесь она измеряется. synced_at ставится при ПЕРВОЙ вставке строки и не
перетирается при обновлении (ON CONFLICT ... DO UPDATE SET не трогает эту
колонку), поэтому synced_at - created_date — честная задержка появления.

Только чтение. Запуск: python probe_crm_lag.py
ENV: DATABASE_URL
"""

import json

from sync.agent.db import _fetch_dicts


def main() -> int:
    out = {}

    # 1. Кривая дозревания: какая доля лидов дня видна через k суток.
    out["maturity_curve"] = _fetch_dicts(
        """
        WITH lag AS (
            SELECT created_date,
                   GREATEST(0, (synced_at AT TIME ZONE 'UTC')::date - created_date) AS d
            FROM crm_lead_details
            WHERE created_date >= CURRENT_DATE - 60
              AND created_date <= CURRENT_DATE - 10   -- дни, успевшие дозреть
        )
        SELECT k AS days_after,
               ROUND(100.0 * COUNT(*) FILTER (WHERE d <= k) / NULLIF(COUNT(*), 0), 1) AS pct_visible
        FROM lag CROSS JOIN generate_series(0, 7) AS k
        GROUP BY k
        ORDER BY k
        """
    )

    # 2. Распределение задержки — медиана и хвост.
    out["lag_percentiles"] = _fetch_dicts(
        """
        WITH lag AS (
            SELECT GREATEST(0, (synced_at AT TIME ZONE 'UTC')::date - created_date) AS d
            FROM crm_lead_details
            WHERE created_date >= CURRENT_DATE - 60 AND created_date <= CURRENT_DATE - 10
        )
        SELECT COUNT(*) AS leads,
               PERCENTILE_DISC(0.5)  WITHIN GROUP (ORDER BY d) AS p50,
               PERCENTILE_DISC(0.9)  WITHIN GROUP (ORDER BY d) AS p90,
               PERCENTILE_DISC(0.95) WITHIN GROUP (ORDER BY d) AS p95,
               PERCENTILE_DISC(0.99) WITHIN GROUP (ORDER BY d) AS p99,
               MAX(d) AS max_lag
        FROM lag
        """
    )

    # 3. Стабилен ли лаг по неделям — или он плавает и одним числом не описывается.
    out["lag_by_week"] = _fetch_dicts(
        """
        WITH lag AS (
            SELECT DATE_TRUNC('week', created_date)::date AS week,
                   GREATEST(0, (synced_at AT TIME ZONE 'UTC')::date - created_date) AS d
            FROM crm_lead_details
            WHERE created_date >= CURRENT_DATE - 90 AND created_date <= CURRENT_DATE - 10
        )
        SELECT week, COUNT(*) AS leads,
               PERCENTILE_DISC(0.5)  WITHIN GROUP (ORDER BY d) AS p50,
               PERCENTILE_DISC(0.95) WITHIN GROUP (ORDER BY d) AS p95
        FROM lag GROUP BY week ORDER BY week
        """
    )

    # 4. Свежий хвост: сколько лидов уже есть за последние дни. Именно эти дни
    #    незрелы, и именно по ним сторож рискует судить.
    out["recent_days"] = _fetch_dicts(
        """
        SELECT created_date, COUNT(*) AS leads,
               COUNT(*) FILTER (WHERE is_eff) AS eff_leads,
               MAX((synced_at AT TIME ZONE 'UTC')::date) AS last_seen
        FROM crm_lead_details
        WHERE created_date >= CURRENT_DATE - 14
        GROUP BY created_date ORDER BY created_date
        """
    )

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
