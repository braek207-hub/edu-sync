# -*- coding: utf-8 -*-
"""Разовый read-only замер под Э3.3: применим ли рычаг бюджета к кампаниям
с целевыми бюджетами Э3.2, и каким механизмом.

Вопросы, на которые отвечает проба (по данным edu_campaign_settings +
edu_agent_computed_settings + edu_agent_facts):

1. У скольких кампаний с budget_target задан WeeklySpendLimit в стратегии
   (settings.strategy.search/network.weeklyBudget)?
2. Сколько сидит на ПАКЕТНОЙ стратегии (strategy.package.id) — их лимит общий
   на несколько кампаний, менять его от имени одной кампании нельзя?
3. Связывает ли лимит расход: недельный расход без НДС (факты с НДС, лимит
   кабинета без) против лимита — binding, если расход ≥ 90 % лимита. Для
   сдвига вверх повышение НЕ связывающего лимита эффекта не даст.
4. Типы стратегий (biddingStrategyType) — форма блока для campaigns.update.

Результат — вход дизайна diff'а Э3.3, не решение: решение печатается в план.
"""

import json

import psycopg2.extras

from sync.db import get_connection

VAT = 1.2          # факты расхода с НДС, лимиты кабинета без НДС
BINDING_SHARE = 0.9


def q(cur, sql, params=()):
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def main() -> int:
    with get_connection() as conn:
        return _report(conn)


def _report(conn) -> int:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    targets = q(cur, """
        SELECT s.object_id AS campaign_id, s.value AS target_28d,
               s.raw_value AS cost_28d, s.rel_error
        FROM edu_agent_computed_settings s
        JOIN (
            SELECT object_id, MAX(calc_date) AS calc_date
            FROM edu_agent_computed_settings
            WHERE object_level = 'campaign' AND setting_kind = 'budget_target'
            GROUP BY object_id
        ) latest USING (object_id, calc_date)
        WHERE s.object_level = 'campaign' AND s.setting_kind = 'budget_target'
    """)

    settings = q(cur, """
        SELECT campaign_id,
               settings #>> '{meta,state}'  AS state,
               settings #>> '{meta,campaignType}' AS campaign_type,
               settings #>> '{strategy,search,biddingStrategyType}'  AS search_type,
               settings #>> '{strategy,network,biddingStrategyType}' AS network_type,
               (settings #>> '{strategy,search,weeklyBudget}')::float  AS weekly_search,
               (settings #>> '{strategy,network,weeklyBudget}')::float AS weekly_network,
               (settings #>> '{strategy,dailyBudget}')::float AS daily_budget,
               settings #> '{strategy,package}' IS NOT NULL AS is_package
        FROM edu_campaign_settings
    """)
    by_id = {str(r["campaign_id"]): r for r in settings}

    out = []
    counters = {"total": 0, "no_settings_row": 0, "package": 0,
                "no_weekly_limit": 0, "weekly_limit_set": 0,
                "binding": 0, "not_binding": 0}
    strategy_types = {}
    for t in targets:
        cid = str(t["campaign_id"])
        counters["total"] += 1
        s = by_id.get(cid)
        row = {
            "campaign_id": cid,
            "cost_28d": round(float(t["cost_28d"] or 0)),
            "target_28d": round(float(t["target_28d"] or 0)),
        }
        if s is None:
            counters["no_settings_row"] += 1
            row["problem"] = "нет строки в edu_campaign_settings"
            out.append(row)
            continue
        row.update({
            "state": s["state"], "type": s["campaign_type"],
            "search_type": s["search_type"], "network_type": s["network_type"],
            "weekly_search": s["weekly_search"], "weekly_network": s["weekly_network"],
            "daily_budget": s["daily_budget"], "package": bool(s["is_package"]),
        })
        key = f"{s['campaign_type']}/{s['search_type']}/{s['network_type']}"
        strategy_types[key] = strategy_types.get(key, 0) + 1
        if s["is_package"]:
            counters["package"] += 1
        weekly_limit = s["weekly_search"] or s["weekly_network"]
        if not weekly_limit:
            counters["no_weekly_limit"] += 1
        else:
            counters["weekly_limit_set"] += 1
            weekly_spend_no_vat = float(t["cost_28d"] or 0) / 4 / VAT
            row["weekly_spend_no_vat"] = round(weekly_spend_no_vat)
            row["binding"] = weekly_spend_no_vat >= BINDING_SHARE * weekly_limit
            counters["binding" if row["binding"] else "not_binding"] += 1
        out.append(row)

    print(json.dumps({
        "counters": counters,
        "strategy_types": strategy_types,
        "campaigns": sorted(out, key=lambda r: -r["cost_28d"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
