# -*- coding: utf-8 -*-
"""
sync/agent/guard.py — гейт качества данных (слой 1 защиты автопилота).

Принцип: молчание безопаснее действия. Любой красный пункт → агент не делает ничего.
Агент, работающий на битых данных, вредит быстрее, чем приносит пользу: он примет
провал синка за обвал конверсии и вырубит здоровые кампании. Человек, глядя на дашборд,
чувствует «что-то не так»; агент — нет.

Все функции чистые: на вход метаданные, на выход вердикты. БД не трогают.
"""

from datetime import datetime, timedelta
from statistics import mean, pstdev
from typing import Any, Dict, List, Optional

MAX_AGE_HOURS = 36
SIGMA_LIMIT = 4.0
SUM_TOLERANCE = 0.01
MIN_HISTORY_FOR_SIGMA = 5


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def check_freshness(
    sources: Dict[str, Optional[str]], now_iso: str, max_age_hours: int = MAX_AGE_HOURS
) -> List[Dict[str, Any]]:
    """Каждый источник обязан быть свежее max_age_hours."""
    now = _parse(now_iso)
    out: List[Dict[str, Any]] = []
    for name, ts in sources.items():
        if not ts:
            out.append({"check_name": f"freshness:{name}", "status": "FAIL",
                        "detail": {"reason": "источник пуст"}})
            continue
        age = (now - _parse(ts)).total_seconds() / 3600.0
        out.append({
            "check_name": f"freshness:{name}",
            "status": "OK" if age <= max_age_hours else "FAIL",
            "detail": {"age_hours": round(age, 2), "limit": max_age_hours},
        })
    return out


def check_volume_anomaly(
    history: List[float], today: float, sigma: float = SIGMA_LIMIT
) -> Dict[str, Any]:
    """Дневной объём не должен отклоняться от истории больше чем на sigma сигм."""
    if len(history) < MIN_HISTORY_FOR_SIGMA:
        return {"check_name": "volume", "status": "OK",
                "detail": {"reason": "истории недостаточно для оценки"}}
    mu = mean(history)
    sd = pstdev(history)
    if sd == 0:
        # Нулевая дисперсия: ловим только точное расхождение с константой.
        status = "OK" if today == mu else "FAIL"
        return {"check_name": "volume", "status": status,
                "detail": {"mean": mu, "sd": 0.0, "today": today}}
    z = abs(today - mu) / sd
    return {
        "check_name": "volume",
        "status": "OK" if z <= sigma else "FAIL",
        "detail": {"mean": round(mu, 2), "sd": round(sd, 2), "today": today, "z": round(z, 2)},
    }


def check_sum_reconciliation(
    direct_cost: float, mart_cost: float, tolerance: float = SUM_TOLERANCE
) -> Dict[str, Any]:
    """Расход по API Директа и расход в витрине должны сходиться."""
    if direct_cost == 0 and mart_cost == 0:
        return {"check_name": "sums", "status": "OK", "detail": {"both": 0}}
    base = max(abs(direct_cost), abs(mart_cost))
    diff = abs(direct_cost - mart_cost) / base
    return {
        "check_name": "sums",
        "status": "OK" if diff <= tolerance else "FAIL",
        "detail": {"direct": direct_cost, "mart": mart_cost, "diff_share": round(diff, 4)},
    }


def check_continuity(dates: List[str], expected_last: str) -> Dict[str, Any]:
    """В ряду дат не должно быть пропущенных дней и последний день обязан быть на месте."""
    if not dates:
        return {"check_name": "continuity", "status": "FAIL", "detail": {"reason": "нет дат"}}
    parsed = sorted(datetime.fromisoformat(str(d)).date() for d in dates)
    if parsed[-1].isoformat() != expected_last:
        return {"check_name": "continuity", "status": "FAIL",
                "detail": {"last": parsed[-1].isoformat(), "expected": expected_last}}
    gaps = []
    for prev, nxt in zip(parsed, parsed[1:]):
        if (nxt - prev) > timedelta(days=1):
            gaps.append(f"{prev.isoformat()}→{nxt.isoformat()}")
    return {
        "check_name": "continuity",
        "status": "OK" if not gaps else "FAIL",
        "detail": {"gaps": gaps},
    }


def verdict(checks: List[Dict[str, Any]]) -> str:
    """GREEN только если все проверки прошли."""
    return "RED" if any(c["status"] == "FAIL" for c in checks) else "GREEN"
