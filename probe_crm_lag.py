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

    # 0. ЕСТЕСТВЕННЫЙ ЭКСПЕРИМЕНТ. edu_agent_facts — снимок, сделанный прогоном
    #    Э0 и с тех пор не перезаписанный (следующий прогон упал на гейте).
    #    Сравнение его с сегодняшней CRM по тем же дням показывает, СКОЛЬКО
    #    лидов дозрело после снимка. Это и есть дозревание, измеренное честно:
    #    два независимых замера одного и того же дня, разнесённые во времени.
    out["snapshot_vs_now"] = _fetch_dicts(
        """
        WITH snap AS (
            SELECT fact_date, SUM(leads) AS leads_then,
                   SUM(eff_leads) AS eff_then, MAX(collected_at) AS collected_at
            FROM edu_agent_facts
            WHERE fact_date >= CURRENT_DATE - 45
            GROUP BY fact_date
        ), now_crm AS (
            SELECT created_date AS fact_date, COUNT(*) AS leads_now,
                   COUNT(*) FILTER (WHERE is_eff) AS eff_now
            FROM crm_lead_details
            WHERE created_date >= CURRENT_DATE - 45
            GROUP BY created_date
        )
        SELECT s.fact_date,
               (s.collected_at AT TIME ZONE 'UTC')::date AS snapshot_taken,
               (s.collected_at AT TIME ZONE 'UTC')::date - s.fact_date AS days_old_at_snapshot,
               s.leads_then, n.leads_now,
               s.eff_then, n.eff_now,
               n.leads_now - s.leads_then AS leads_added,
               ROUND(100.0 * s.leads_then / NULLIF(n.leads_now, 0), 1) AS pct_seen_at_snapshot
        FROM snap s JOIN now_crm n USING (fact_date)
        ORDER BY s.fact_date
        """
    )

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
    #    ВНИМАНИЕ: synced_at перезаписывается синком (замер 32455026738 показал
    #    last_seen = сегодня у всех дней, включая месячной давности), поэтому
    #    п.1-3 НЕ измеряют лаг. Оставлены как улика перезаписи, судить по ним
    #    о дозревании нельзя — источник истины здесь п.0.
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
