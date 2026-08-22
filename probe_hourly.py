# -*- coding: utf-8 -*-
"""
probe_hourly.py — почему почасовой профиль опускает 22 часа из 24.

Сухой прогон 32568178620 дал расписание, где почти все часы ниже нейтрали:
80,80,110,70,100,60,80,90,90,80,90,80,80,90,90,70,70,80,70,70,80,80,80,80

Так не бывает по построению. Коэффициент — это отношение конверсионности часа
к БАЗОВОЙ по срезу, а база считается из тех же строк (Σ целей / Σ визитов).
Значит примерно половина часов обязана оказаться выше базы, медиана — около
100. Здесь медиана ~80, то есть знаменатель больше, чем должен быть.

Печатается сырьё, по которому это можно проверить руками:
  · что лежит в edu_agent_computed_settings по schedule:hour;
  · сырой почасовой профиль Метрики по каждому счётчику EDU;
  · база, посчитанная теми же двумя способами — по сумме всех строк и как
    медиана часов, — и сколько часов оказывается ниже каждой.

Только чтение. Запуск: python probe_hourly.py
ENV: DATABASE_URL, YM_TOKEN
"""

import json
from datetime import date, timedelta

from sync.agent.db import _fetch_dicts
from sync.agent.metrika import (
    EDU_COUNTERS,
    fetch_counter_goal_ids,
    fetch_hourly_profile,
)


def main() -> int:
    out = {}

    out["computed_rows"] = _fetch_dicts(
        """
        SELECT object_id, setting_key, value, support_n, raw_value, calc_date
        FROM edu_agent_computed_settings
        WHERE setting_kind = 'schedule:hour'
        ORDER BY object_id, (setting_key)::int
        LIMIT 96
        """
    )

    date_to = (date.today() - timedelta(days=1)).isoformat()
    date_from = (date.today() - timedelta(days=90)).isoformat()
    profiles = {}
    for counter in EDU_COUNTERS:
        try:
            # Профиль считается по целям-лидам; какие из них есть у счётчика —
            # знает только сам счётчик (чужой id Метрика отвергает целиком).
            goals = fetch_counter_goal_ids(counter)
            rows, _chosen = fetch_hourly_profile(counter, date_from, date_to, goals)
        except Exception as exc:  # доступ к счётчику мог быть не выдан
            profiles[str(counter)] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            continue

        total_visits = sum(int(r["clicks"] or 0) for r in rows)
        total_goals = sum(float(r["sum_p_pay"] or 0.0) for r in rows)
        base = (total_goals / total_visits) if total_visits else 0.0
        per_hour = []
        for row in rows:
            visits = int(row["clicks"] or 0)
            goals = float(row["sum_p_pay"] or 0.0)
            conv = (goals / visits) if visits else 0.0
            per_hour.append({
                "hour": int(row["segment_key"]),
                "visits": visits,
                "goals": round(goals, 1),
                "conv": round(conv, 5),
                "ratio_to_base": round(conv / base, 3) if base else None,
            })
        below = sum(1 for h in per_hour if h["ratio_to_base"] and h["ratio_to_base"] < 1)
        conv_values = sorted(h["conv"] for h in per_hour)
        median_conv = conv_values[len(conv_values) // 2] if conv_values else 0.0
        profiles[str(counter)] = {
            "hours": len(rows),
            "total_visits": total_visits,
            "total_goals": round(total_goals, 1),
            "base_conv_by_sum": round(base, 5),
            "median_hour_conv": round(median_conv, 5),
            # Если база по сумме заметно выше медианы часа — среднее тянут
            # вверх немногие часы с большим объёмом, и «ниже базы» оказывается
            # большинство. Это и есть проверяемая здесь версия.
            "base_over_median": round(base / median_conv, 3) if median_conv else None,
            "hours_below_base": below,
            "per_hour": per_hour,
        }
    out["metrika_profiles"] = profiles

    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
