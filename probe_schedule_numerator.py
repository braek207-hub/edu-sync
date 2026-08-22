# -*- coding: utf-8 -*-
"""probe_schedule_numerator.py — из чего на самом деле сложены 24 числа расписания.

Аудит движка записи назвал два подозрения по почасовому профилю, и оба меняют
ровно те коэффициенты, которые уходят в TimeTargeting боевой кампании:

  1. У запроса нет фильтра по рекламному трафику (соседняя fetch_campaign_behavior
     фильтр ставит). Значит числитель и знаменатель считаются по ВСЕМУ сайту:
     SEO, прямые заходы, соцсети. Ночью и в выходные доля бесплатного трафика
     выше — час получает коэффициент состава источников, а не ценности клика.

  2. Колонки целей складываются. В этом же репозитории по отчёту Директа
     доказано, что цели дублируются: 330070378 и 369313502 дали одинаковые
     2753/2753 — одно действие под двумя идентификаторами. Сумма считает его
     дважды.

Печатается сырьё, по которому оба вопроса закрываются ДА/НЕТ:
  A. по каждой цели Директа — имя из Метрики, сумма за окно, вектор 24 часов;
  B. попарное сравнение векторов — какие цели дублируют друг друга;
  C. коэффициенты расписания в четырёх вариантах (фильтр × сумма/одна цель)
     и максимальное расхождение между ними.

Только чтение. ENV: YM_TOKEN, DIRECT_TOKEN, DIRECT_CLIENTS_JSON.
"""

import json
import os
import urllib.request
from datetime import date, timedelta
from typing import Dict, List

from sync.agent.metrika import (
    ATTRIBUTION,
    EDU_COUNTERS,
    GOALS_API_URL,
    ROW_LIMIT,
    _metrica_get,
    fetch_counter_goal_ids,
)

AD_FILTER = "ym:s:lastTrafficSource=='ad'"


def goal_names(counter_id: int, token: str) -> Dict[int, str]:
    req = urllib.request.Request(
        GOALS_API_URL.format(counter=counter_id),
        headers={"Authorization": "OAuth " + token},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return {int(g["id"]): g.get("name", "") for g in data.get("goals", [])}


def _by_hour(counter_id, d1, d2, token, metrics, column, ad_only):
    params = {
        "ids": counter_id,
        "metrics": metrics,
        "dimensions": "ym:s:hour",
        "date1": d1,
        "date2": d2,
        "attribution": ATTRIBUTION,
        "limit": ROW_LIMIT,
        "accuracy": "full",
    }
    if ad_only:
        params["filters"] = AD_FILTER
    out = [0.0] * 24
    for row in _metrica_get(params, token).get("data") or []:
        raw = str(row["dimensions"][0].get("name") or "0")
        out[int(raw.split(":")[0])] = float(row["metrics"][column] or 0.0)
    return out


def hourly(counter_id, goal_id, d1, d2, token, ad_only):
    return _by_hour(counter_id, d1, d2, token,
                    "ym:s:visits,ym:s:goal%dreaches" % goal_id, 1, ad_only)


def visits_by_hour(counter_id, d1, d2, token, ad_only):
    return _by_hour(counter_id, d1, d2, token, "ym:s:visits", 0, ad_only)


def coefficients(goals: List[float], visits: List[float]) -> List[int]:
    """Те же 24 числа, что уходят в кабинет: конверсионность часа к базе."""
    total_g, total_v = sum(goals), sum(visits)
    base = (total_g / total_v) if total_v else 0.0
    out = []
    for g, v in zip(goals, visits):
        conv = (g / v) if v else 0.0
        ratio = (conv / base) if base else 1.0
        # Округление до десятков — требование API (см. writer/schedule.py).
        out.append(max(10, min(200, int(round(ratio * 100 / 10.0)) * 10)))
    return out


def correlation(a: List[float], b: List[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db = sum((y - mb) ** 2 for y in b) ** 0.5
    return round(num / (da * db), 4) if da and db else 0.0


def main() -> int:
    token = os.environ["YM_TOKEN"]
    d2 = (date.today() - timedelta(days=1)).isoformat()
    d1 = (date.today() - timedelta(days=90)).isoformat()

    # Тот же набор целей, что Э0 отдаёт в расписание.
    from sync.agent_e0 import resolve_goal_ids
    clients = json.loads(os.environ["DIRECT_CLIENTS_JSON"])
    lead_goals = set()
    for client in clients:
        lead_goals.update(int(g) for g in resolve_goal_ids(client) if str(g).isdigit())

    out = {"window": [d1, d2], "direct_lead_goals_total": len(lead_goals), "counters": {}}

    for counter in EDU_COUNTERS:
        block = {}
        try:
            names = goal_names(counter, token)
            mine = sorted(lead_goals & set(fetch_counter_goal_ids(counter)))
        except Exception as exc:
            out["counters"][str(counter)] = {"error": ("%s: %s" % (type(exc).__name__, exc))[:200]}
            continue

        v_all = visits_by_hour(counter, d1, d2, token, False)
        v_ad = visits_by_hour(counter, d1, d2, token, True)
        block["visits_all_total"] = int(sum(v_all))
        block["visits_ad_total"] = int(sum(v_ad))
        block["ad_share_by_hour"] = [
            round(a / t, 3) if t else None for a, t in zip(v_ad, v_all)
        ]

        # A. по каждой цели — имя, объём, вектор
        per_goal = {}
        for gid in mine:
            vec_all = hourly(counter, gid, d1, d2, token, False)
            vec_ad = hourly(counter, gid, d1, d2, token, True)
            per_goal[str(gid)] = {
                "name": names.get(gid, "?"),
                "total_all": int(sum(vec_all)),
                "total_ad": int(sum(vec_ad)),
                "hours_all": [int(x) for x in vec_all],
                "hours_ad": [int(x) for x in vec_ad],
            }
        block["goals"] = per_goal

        # B. кто кого дублирует
        dupes = []
        ids = list(per_goal.keys())
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a = per_goal[ids[i]]["hours_all"]
                b = per_goal[ids[j]]["hours_all"]
                if sum(a) == 0 or sum(b) == 0:
                    continue
                dupes.append({
                    "pair": [ids[i], ids[j]],
                    "identical": a == b,
                    "corr": correlation([float(x) for x in a], [float(x) for x in b]),
                    "totals": [sum(a), sum(b)],
                })
        block["pairs"] = sorted(dupes, key=lambda d: -d["corr"])[:20]

        # C. четыре варианта числителя → четыре расписания
        if per_goal:
            sum_all = [float(sum(per_goal[g]["hours_all"][h] for g in per_goal)) for h in range(24)]
            sum_ad = [float(sum(per_goal[g]["hours_ad"][h] for g in per_goal)) for h in range(24)]
            top = max(per_goal, key=lambda g: per_goal[g]["total_all"])
            one_all = [float(x) for x in per_goal[top]["hours_all"]]
            one_ad = [float(x) for x in per_goal[top]["hours_ad"]]

            variants = {
                "sum_goals_no_filter": coefficients(sum_all, v_all),
                "sum_goals_ad_filter": coefficients(sum_ad, v_ad),
                "one_goal_no_filter": coefficients(one_all, v_all),
                "one_goal_ad_filter": coefficients(one_ad, v_ad),
            }
            block["top_goal"] = {"id": top, "name": per_goal[top]["name"]}
            block["variants"] = variants
            live = variants["sum_goals_no_filter"]
            block["max_delta_vs_live"] = {
                k: max(abs(a - b) for a, b in zip(live, v))
                for k, v in variants.items() if k != "sum_goals_no_filter"
            }
        out["counters"][str(counter)] = block

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
