# -*- coding: utf-8 -*-
"""
Замер экономики кабинета — вход для Э2.1 (лестница воронки) и Э3.1 (кривые насыщения).

Что мерим и зачем:
1. Текущая прибыль и ROMI за зрелый период — точка отсчёта: где мы относительно
   контракта «выручка = 2×бюджет».
2. Разброс множителей воронки по направлениям и кампаниям — если множители не
   различаются, перекладывать бюджет незачем; если различаются, видно где рычаг.
3. Средний чек по направлениям — второй множитель ROMI помимо конверсии.
4. Точки (расход → лиды → выручка) по неделям — сырьё для кривых насыщения.
5. Профиль дозревания выручки по лагу — без него факт месяца непереводим в прогноз.

Когортный принцип: выручка недели = оплаты лидов, СОЗДАННЫХ в эту неделю, когда бы
они ни заплатили. Сравнивать кассу месяца с расходом месяца нельзя — лаг оплаты
делает любой рост расхода убыточным на бумаге.

Запуск: python probe_economics.py   (нужен DATABASE_URL)
"""

import json
from collections import defaultdict
from datetime import date, timedelta

import psycopg2.extras

from sync.db import get_connection

LOOKBACK_DAYS = 540
MATURITY_PERCENTILE = 0.90
# Кампании мельче — в разброс множителей не идут: их коэффициенты — шум.
MIN_CAMPAIGN_LEADS = 200


def _fetch(sql, params):
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _as_date(value):
    """Колонка может приехать строкой — сравнение дат тогда молча соврёт."""
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(int(q * len(ordered)), len(ordered) - 1)
    return ordered[idx]


def _week(d):
    """Понедельник ISO-недели — ключ когорты."""
    return (d - timedelta(days=d.weekday())).isoformat()


def _ratio(a, b):
    return round(a / b, 4) if b else None


def _funnel(leads):
    n = len(leads)
    eff = sum(1 for r in leads if r["is_eff"])
    conn = sum(1 for r in leads if r["is_connected"])
    deals = sum(1 for r in leads if r["is_deal"])
    paid = sum(1 for r in leads if r["is_paid"])
    revenue = sum(float(r["amount"] or 0) for r in leads if r["is_paid"])
    return {
        "leads": n, "eff": eff, "connected": conn, "deals": deals, "paid": paid,
        "revenue": round(revenue),
        "p_eff": _ratio(eff, n),
        "p_conn": _ratio(conn, eff),
        "p_deal": _ratio(deals, conn),
        "p_pay": _ratio(paid, deals),
        "avg_check": round(revenue / paid) if paid else None,
        "revenue_per_lead": round(revenue / n) if n else None,
    }


def main() -> int:
    today = date.today()
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()

    leads = _fetch(
        """
        SELECT lead_id, campaign_id, created_date, payment_date,
               is_eff, is_connected, is_deal, is_paid, amount, direction, project
        FROM crm_lead_details
        WHERE created_date >= %s
        """,
        (since,),
    )
    cost_rows = _fetch(
        """
        SELECT date, campaign_id, project, direction, cost
        FROM direct_stats
        WHERE date >= %s
        """,
        (since,),
    )

    for r in leads:
        r["created_date"] = _as_date(r["created_date"])
        r["payment_date"] = _as_date(r["payment_date"])
    for r in cost_rows:
        r["date"] = _as_date(r["date"])

    # 1. Окно созревания — из данных, не константой.
    lags = [
        (r["payment_date"] - r["created_date"]).days
        for r in leads
        if r["is_paid"] and r["payment_date"] and r["created_date"]
    ]
    maturity_days = int(_pct(lags, MATURITY_PERCENTILE) or 0)
    mature_before = today - timedelta(days=maturity_days)

    mature = [r for r in leads if r["created_date"] <= mature_before]
    cost_mature = [r for r in cost_rows if r["date"] <= mature_before]

    report = {
        "lookback_days": LOOKBACK_DAYS,
        "maturity_days": maturity_days,
        "mature_before": mature_before.isoformat(),
        "payment_lag_days": {"p50": _pct(lags, 0.5), "p90": _pct(lags, 0.9),
                             "p99": _pct(lags, 0.99), "n": len(lags)},
    }

    # 2. Покрытие: чей расход и чьи лиды вообще сшиваются.
    lead_campaigns = {str(r["campaign_id"]) for r in mature if r["campaign_id"]}
    cost_by_campaign = defaultdict(float)
    for r in cost_mature:
        cost_by_campaign[str(r["campaign_id"])] += float(r["cost"] or 0)
    cost_total = sum(cost_by_campaign.values())
    cost_matched = sum(v for k, v in cost_by_campaign.items() if k in lead_campaigns)
    leads_with_campaign = sum(1 for r in mature if r["campaign_id"])
    report["coverage"] = {
        "cost_total": round(cost_total),
        "cost_on_campaigns_with_leads": round(cost_matched),
        "cost_matched_share": _ratio(cost_matched, cost_total),
        "leads_mature": len(mature),
        "leads_with_campaign_share": _ratio(leads_with_campaign, len(mature)),
        "projects_in_cost": sorted({str(r["project"]) for r in cost_rows}),
        "projects_in_leads": sorted({str(r["project"]) for r in leads}),
    }

    # 3. Экономика зрелого окна: все лиды и отдельно привязанные к Директу.
    #    Контракт месяца — выручка 2×бюджет — проверяется на привязанных:
    #    органика в ROMI платного трафика не входит.
    attributed = [r for r in mature if r["campaign_id"]]
    for label, subset in (("все_лиды", mature), ("лиды_директа", attributed)):
        f = _funnel(subset)
        f["cost"] = round(cost_total)
        f["profit"] = round(f["revenue"] - cost_total)
        f["romi"] = _ratio(f["revenue"] - cost_total, cost_total)
        f["revenue_to_cost"] = _ratio(f["revenue"], cost_total)
        f["cpl"] = round(cost_total / f["leads"]) if f["leads"] else None
        f["cp_eff"] = round(cost_total / f["eff"]) if f["eff"] else None
        report.setdefault("экономика_зрелая", {})[label] = f

    # 4. Направления: воронка, чек, расход, ROMI.
    by_dir_leads = defaultdict(list)
    for r in attributed:
        by_dir_leads[r["direction"] or "БЕЗ_НАПРАВЛЕНИЯ"].append(r)
    cost_by_dir = defaultdict(float)
    for r in cost_mature:
        cost_by_dir[r["direction"] or "БЕЗ_НАПРАВЛЕНИЯ"] += float(r["cost"] or 0)

    directions = {}
    for d in sorted(set(by_dir_leads) | set(cost_by_dir)):
        f = _funnel(by_dir_leads.get(d, []))
        c = cost_by_dir.get(d, 0.0)
        f["cost"] = round(c)
        f["romi"] = _ratio(f["revenue"] - c, c)
        f["cpl"] = round(c / f["leads"]) if f["leads"] else None
        f["cp_eff"] = round(c / f["eff"]) if f["eff"] else None
        directions[d] = f
    report["направления"] = directions

    # 5. Разброс множителей по кампаниям внутри направления.
    #    Если revenue_per_lead у кампаний одного направления различается в разы —
    #    портфельные решения внутри направления имеют смысл, а не только между.
    by_campaign = defaultdict(list)
    for r in attributed:
        by_campaign[str(r["campaign_id"])].append(r)
    spread = {}
    for d in sorted(by_dir_leads):
        camp_stats = []
        for cid, rows in by_campaign.items():
            if len(rows) < MIN_CAMPAIGN_LEADS:
                continue
            if (rows[0]["direction"] or "БЕЗ_НАПРАВЛЕНИЯ") != d:
                continue
            f = _funnel(rows)
            c = cost_by_campaign.get(cid, 0.0)
            camp_stats.append({
                "campaign_id": cid, "leads": f["leads"], "cost": round(c),
                "cpl": round(c / f["leads"]) if f["leads"] else None,
                "p_eff": f["p_eff"], "p_conn": f["p_conn"],
                "p_deal": f["p_deal"], "p_pay": f["p_pay"],
                "avg_check": f["avg_check"],
                "revenue_per_lead": f["revenue_per_lead"],
                "romi": _ratio(f["revenue"] - c, c) if c else None,
            })
        if not camp_stats:
            continue
        rpl = sorted(x["revenue_per_lead"] or 0 for x in camp_stats)
        spread[d] = {
            "campaigns": len(camp_stats),
            "revenue_per_lead_min": rpl[0],
            "revenue_per_lead_p25": _pct(rpl, 0.25),
            "revenue_per_lead_p50": _pct(rpl, 0.5),
            "revenue_per_lead_p75": _pct(rpl, 0.75),
            "revenue_per_lead_max": rpl[-1],
            "по_кампаниям": sorted(camp_stats, key=lambda x: -x["cost"]),
        }
    report["разброс_по_кампаниям"] = spread

    # 6. Недельные точки для кривых насыщения: расход недели против когорты её лидов.
    weekly = defaultdict(lambda: defaultdict(lambda: {"cost": 0.0, "leads": 0,
                                                      "eff": 0, "revenue": 0.0}))
    for r in cost_mature:
        w = _week(r["date"])
        slot = weekly[r["direction"] or "БЕЗ_НАПРАВЛЕНИЯ"][w]
        slot["cost"] += float(r["cost"] or 0)
    for r in attributed:
        w = _week(r["created_date"])
        slot = weekly[r["direction"] or "БЕЗ_НАПРАВЛЕНИЯ"][w]
        slot["leads"] += 1
        slot["eff"] += 1 if r["is_eff"] else 0
        if r["is_paid"]:
            slot["revenue"] += float(r["amount"] or 0)
    # Только полные зрелые недели: неделя целиком до границы созревания.
    last_full = _week(mature_before)  # сама неделя границы может быть неполной
    report["недели"] = {
        d: [
            [w, round(s["cost"]), s["leads"], s["eff"], round(s["revenue"])]
            for w, s in sorted(weeks.items()) if w < last_full
        ]
        for d, weeks in weekly.items()
    }
    report["недели_формат"] = "[понедельник, расход, лиды, эфф, выручка_когорты]"

    # 7. Профиль дозревания: какая доля выручки когорты видна через N дней после лида.
    paid = [(
        (r["payment_date"] - r["created_date"]).days, float(r["amount"] or 0))
        for r in mature
        if r["is_paid"] and r["payment_date"] and r["created_date"]]
    total_rev = sum(a for _, a in paid)
    profile = {}
    for horizon in (7, 14, 30, 60, 90, 180):
        got = sum(a for lag, a in paid if lag <= horizon)
        profile[f"до_{horizon}_дн"] = _ratio(got, total_rev)
    report["дозревание_выручки"] = profile

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
