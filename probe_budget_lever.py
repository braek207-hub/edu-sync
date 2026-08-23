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
               settings #> '{strategy,package}' IS NOT NULL AS is_package,
               settings #>> '{strategy,package,id}'   AS package_id,
               settings #>> '{strategy,package,name}' AS package_name,
               settings #>> '{strategy,package,type}' AS package_type,
               (settings #>> '{strategy,package,weeklyBudget}')::float AS package_weekly
        FROM edu_campaign_settings
    """)
    by_id = {str(r["campaign_id"]): r for r in settings}

    # Расход 28 зрелых дней ВСЕХ кампаний (не только целевых): кампании без
    # budget_target, сидящие на том же пакете, тратят тот же общий лимит.
    spend = {str(r["campaign_id"]): float(r["cost"] or 0) for r in q(cur, """
        SELECT campaign_id, SUM(cost) AS cost
        FROM edu_agent_facts
        WHERE fact_date >= CURRENT_DATE - 28
        GROUP BY campaign_id
    """)}

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
            "package_id": s["package_id"],
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

    # Карта пакетов: кто ещё делит пакет с целевыми кампаниями, суммарный
    # расход участников против недельного лимита пакета.
    target_ids = {str(t["campaign_id"]) for t in targets}
    packages: dict = {}
    for r in settings:
        pid = r["package_id"]
        if not pid:
            continue
        p = packages.setdefault(str(pid), {
            "name": r["package_name"], "type": r["package_type"],
            "weekly_budget": r["package_weekly"],
            "members": [], "target_members": [],
            "members_spend_28d": 0.0,
        })
        cid = str(r["campaign_id"])
        p["members"].append(cid)
        if cid in target_ids:
            p["target_members"].append(cid)
        p["members_spend_28d"] += spend.get(cid, 0.0)
    shared = {pid: p for pid, p in packages.items() if p["target_members"]}
    for p in shared.values():
        p["members"] = len(p["members"])
        p["members_spend_28d"] = round(p["members_spend_28d"])
        wl = p["weekly_budget"]
        p["binding"] = (bool(wl)
                        and p["members_spend_28d"] / 4 / VAT >= BINDING_SHARE * wl)

    print(json.dumps({
        "counters": counters,
        "strategy_types": strategy_types,
        "packages_of_targets": {
            "count": len(shared),
            "single_member": sum(1 for p in shared.values() if p["members"] == 1),
            "multi_member": sum(1 for p in shared.values() if p["members"] > 1),
            "detail": dict(sorted(shared.items(),
                                  key=lambda kv: -kv[1]["members_spend_28d"])),
        },
        "campaigns": sorted(out, key=lambda r: -r["cost_28d"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
