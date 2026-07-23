"""Чистые трансформации фич ML-скоринга EDU. Без побочных эффектов — тестируются
отдельно. Оркестрация чтения/записи — в sync/ml_features_build.py."""

import json
from datetime import date, datetime
from typing import Any, Optional


def load_admission_deadlines(path: str) -> list[date]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return [datetime.strptime(d, "%Y-%m-%d").date() for d in cfg["deadlines"]]


def days_to_deadline(created: date, deadlines: list[date]) -> int:
    """Дней до ближайшего дедлайна ≥ created. Если все в прошлом — дней до последнего
    (отрицательное)."""
    future = [d for d in deadlines if d >= created]
    if future:
        return (min(future) - created).days
    return (max(deadlines) - created).days
