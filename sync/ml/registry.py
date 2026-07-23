"""Реестр фич ML-скоринга EDU с флагом точки доступности. Защита от темпоральной
утечки: модель на точке решения `point` видит только фичи, известные к `point`."""

from dataclasses import dataclass
from typing import Literal

Availability = Literal["pre_lead", "at_creation", "post_connection", "outcome"]

# Порядок точек во времени жизни лида. `outcome` — вне выбора (метки).
_ORDER = {"pre_lead": 0, "at_creation": 1, "post_connection": 2}


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    availability: Availability
    dtype: Literal["num", "cat"]


REGISTRY: list[FeatureSpec] = [
    # L0 — известны на создании лида
    FeatureSpec("audience", "at_creation", "cat"),
    FeatureSpec("b24_grad_year", "at_creation", "cat"),
    FeatureSpec("b24_edu_level", "at_creation", "cat"),
    FeatureSpec("city_ip_segment", "at_creation", "cat"),
    FeatureSpec("direction", "at_creation", "cat"),
    FeatureSpec("product_group", "at_creation", "cat"),
    FeatureSpec("utm_source", "at_creation", "cat"),
    FeatureSpec("created_dow", "at_creation", "num"),
    FeatureSpec("created_hour", "at_creation", "num"),
    FeatureSpec("days_to_deadline", "at_creation", "num"),
    # L2 (Ф1a — текущий дневной агрегат Метрики; известно ДО заявки)
    FeatureSpec("beh_visits", "pre_lead", "num"),
    FeatureSpec("beh_visit_days", "pre_lead", "num"),
    FeatureSpec("beh_avg_duration_sec", "pre_lead", "num"),
    FeatureSpec("beh_bounce_rate", "pre_lead", "num"),
    FeatureSpec("beh_page_depth", "pre_lead", "num"),
    FeatureSpec("beh_device", "pre_lead", "cat"),
    FeatureSpec("beh_source", "pre_lead", "cat"),
    FeatureSpec("missing_behavior", "pre_lead", "num"),   # 1 если нет поведения/client_id
    FeatureSpec("repeat_lead", "pre_lead", "num"),        # >1 лида у client_id
    # L1 — известно только ПОСЛЕ дозвона
    FeatureSpec("time_to_connection_days", "post_connection", "num"),
    FeatureSpec("dispatcher", "post_connection", "cat"),
    FeatureSpec("responsible", "post_connection", "cat"),
]


def select_features(point: Availability) -> list[str]:
    """Имена фич, известных к точке решения `point` (включая более ранние точки).
    `outcome` не возвращается никогда."""
    if point == "outcome":
        return []
    cutoff = _ORDER[point]
    return [
        spec.name
        for spec in REGISTRY
        if spec.availability in _ORDER and _ORDER[spec.availability] <= cutoff
    ]


def feature_key(name: str) -> str:
    """Физический ключ фичи в JSONB-колонке `features` (build_feature_rows пишет с
    префиксом f__). Ф1b выбирает фичи так: [feature_key(n) for n in select_features(point)]."""
    return f"f__{name}"
