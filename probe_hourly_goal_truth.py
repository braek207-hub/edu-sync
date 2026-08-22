# -*- coding: utf-8 -*-
"""
probe_hourly_goal_truth.py — на что на самом деле оптимизируется расписание.

Почасовой профиль строится по `ym:s:sumGoalReachesAny` — сумме достижений
ЛЮБОЙ цели счётчика. На замере 22.08 это 0.38 достижения на визит: столько
заявок не бывает, значит величину задают микроцели (скролл, время на сайте,
открытие формы), а не лиды.

Расчёт после этого говорит, что ночь 3:00–7:00 — лучшее время (+13, +27,
+21 %), а вечер 20:00–21:00 худшее (−7, −13 %). Павел утверждает обратное:
ночные лиды конвертятся хуже дневных. Одно из двух неверно, и цена ошибки —
ставки в кабинете, поднятые на часы, которые денег не приносят.

Опыт: по каждому счётчику EDU берётся список целей и почасовой профиль
отдельно по каждой цели. Печатается, как выглядит час по каждой цели и
насколько порядок часов расходится с профилем по «любой цели».

Только чтение. Запуск: python probe_hourly_goal_truth.py
ENV: YM_TOKEN
"""

import json
import os
from datetime import date, timedelta

import requests

from sync.agent.metrika import ATTRIBUTION, EDU_COUNTERS

STAT_URL = "https://api-metrika.yandex.net/stat/v1/data"
GOALS_URL = "https://api-metrika.yandex.net/management/v1/counter/{counter}/goals"
HOURS = 24


def _get(url, params):
    headers = {"Authorization": f"OAuth {os.environ['YM_TOKEN']}"}
    r = requests.get(url, params=params, headers=headers, timeout=120)
    if r.status_code != 200:
        return {"_http": r.status_code, "_body": r.text[:300]}
    return r.json()


def _goals(counter):
    data = _get(GOALS_URL.format(counter=counter), {})
    if "_http" in data:
        return []
    return [{"id": g["id"], "name": g.get("name", ""), "type": g.get("type", "")}
            for g in data.get("goals", [])]


def _hourly(counter, metric, date_from, date_to):
    """{час: значение} по одной метрике."""
    data = _get(STAT_URL, {
        "ids": counter,
        "metrics": f"ym:s:visits,{metric}",
        "dimensions": "ym:s:hour",
        "date1": date_from, "date2": date_to,
        "attribution": ATTRIBUTION, "limit": 100, "accuracy": "full",
    })
    if "_http" in data:
        return None, data
    out = {}
    for row in data.get("data", []):
        dims, metrics = row.get("dimensions") or [], row.get("metrics") or []
        if not dims or len(metrics) < 2:
            continue
        try:
            hour = int(str(dims[0].get("name", "")).split(":")[0])
        except (ValueError, AttributeError):
            continue
        out[hour] = {"visits": float(metrics[0] or 0), "value": float(metrics[1] or 0)}
    return out, None


def _profile(hourly):
    """Отношение конверсии часа к базе по сумме — ровно как считает Э0."""
    total_v = sum(h["visits"] for h in hourly.values())
    total_g = sum(h["value"] for h in hourly.values())
    if not total_v or not total_g:
        return None
    base = total_g / total_v
    return {h: round((v["value"] / v["visits"]) / base, 3)
            for h, v in sorted(hourly.items()) if v["visits"]}


def _night_vs_day(profile):
    """Ночь 1–7 против дня 10–20 — та самая проверяемая величина."""
    night = [v for h, v in profile.items() if 1 <= h <= 7]
    day = [v for h, v in profile.items() if 10 <= h <= 20]
    if not night or not day:
        return None
    return round(sum(night) / len(night) / (sum(day) / len(day)), 3)


def main() -> int:
    date_to = (date.today() - timedelta(days=1)).isoformat()
    date_from = (date.today() - timedelta(days=90)).isoformat()
    out = {"window": [date_from, date_to], "counters": {}}

    for counter in EDU_COUNTERS:
        block = {"goals": []}

        any_hourly, err = _hourly(counter, "ym:s:sumGoalReachesAny", date_from, date_to)
        if err:
            out["counters"][str(counter)] = {"error": err}
            continue
        any_profile = _profile(any_hourly)
        block["any_goal"] = {
            "reaches_per_visit": round(
                sum(h["value"] for h in any_hourly.values())
                / max(sum(h["visits"] for h in any_hourly.values()), 1), 4),
            "night_over_day": _night_vs_day(any_profile) if any_profile else None,
            "profile": any_profile,
        }

        for goal in _goals(counter):
            hourly, err = _hourly(counter, f"ym:s:goal{goal['id']}reaches",
                                  date_from, date_to)
            if err:
                block["goals"].append({**goal, "error": err})
                continue
            reaches = sum(h["value"] for h in hourly.values())
            visits = sum(h["visits"] for h in hourly.values())
            profile = _profile(hourly)
            block["goals"].append({
                **goal,
                "reaches": int(reaches),
                "reaches_per_visit": round(reaches / max(visits, 1), 5),
                # Доля цели в «любой цели»: видно, кто задаёт профиль.
                "share_of_any": round(
                    reaches / max(sum(h["value"] for h in any_hourly.values()), 1), 3),
                "night_over_day": _night_vs_day(profile) if profile else None,
                "profile": profile,
            })

        block["goals"].sort(key=lambda g: g.get("reaches", 0), reverse=True)
        out["counters"][str(counter)] = block

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
