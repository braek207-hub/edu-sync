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
    # audience/b24_* заполняет МЕНЕДЖЕР ПРИ ЗВОНКЕ, не форма: SQL-проба 2026-08-05 —
    # b24_grad_year заполнен у 96.5% дозвонившихся vs 3.7% недозвонившихся (90д, vuz).
    # На at_creation это утечка дозвона (та же грабля, что product_group в Ф1b) →
    # переклассифицированы в post_connection, где анкета честно существует.
    FeatureSpec("audience", "post_connection", "cat"),
    FeatureSpec("b24_grad_year", "post_connection", "cat"),
    FeatureSpec("b24_edu_level", "post_connection", "cat"),
    # L0 — известны на создании лида
    FeatureSpec("city_ip_segment", "at_creation", "cat"),
    FeatureSpec("direction", "at_creation", "cat"),
    FeatureSpec("campaign_id", "at_creation", "cat"),
    # product_group / utm_source в crm_lead_details заполняются от привязанного продукта/заказа
    # НА ЭТАПЕ ОПЛАТЫ (null у 100% неоплативших) → это outcome-утечка, НЕ выбирать в фичи.
    FeatureSpec("product_group", "outcome", "cat"),
    FeatureSpec("utm_source", "outcome", "cat"),
    FeatureSpec("created_dow", "at_creation", "num"),
    FeatureSpec("created_hour", "at_creation", "num"),
    FeatureSpec("days_to_deadline", "at_creation", "num"),
    # L2 (Ф1a-Ф2 переход — per-visit сессии из Logs API; известно ДО заявки)
    FeatureSpec("beh_visits", "pre_lead", "num"),
    FeatureSpec("beh_visit_days", "pre_lead", "num"),
    FeatureSpec("beh_avg_duration_sec", "pre_lead", "num"),
    FeatureSpec("beh_bounce_rate", "pre_lead", "num"),
    FeatureSpec("beh_page_depth", "pre_lead", "num"),
    FeatureSpec("beh_device", "pre_lead", "cat"),
    FeatureSpec("beh_source", "pre_lead", "cat"),
    FeatureSpec("missing_behavior", "pre_lead", "num"),   # 1 если нет поведения/client_id
    FeatureSpec("repeat_lead", "pre_lead", "num"),        # >1 лида у client_id
    FeatureSpec("visits_before_lead", "pre_lead", "num"),
    FeatureSpec("days_since_first_touch", "pre_lead", "num"),
    FeatureSpec("sessions_before", "pre_lead", "num"),
    FeatureSpec("had_repeat_visit", "pre_lead", "num"),
    # L1 — известно только ПОСЛЕ дозвона
    FeatureSpec("time_to_connection_days", "post_connection", "num"),
    FeatureSpec("dispatcher", "post_connection", "cat"),
    FeatureSpec("responsible", "post_connection", "cat"),
    FeatureSpec("mins_to_connection", "post_connection", "num"),
    # Ф2 (Task 4) — per-visit сессии из Logs API (edu_visit_sessions, Task 3).
    # UTM/Директ визита известны на клике ДО заявки, но описывают саму заявку → at_creation.
    # first_traffic/is_new_user/устройство — история поведения ДО заявки → pre_lead.
    FeatureSpec("sess_is_new_user", "pre_lead", "num"),
    FeatureSpec("sess_utm_source", "at_creation", "cat"),
    FeatureSpec("sess_utm_medium", "at_creation", "cat"),
    FeatureSpec("sess_utm_campaign", "at_creation", "cat"),
    FeatureSpec("sess_utm_content", "at_creation", "cat"),
    FeatureSpec("sess_utm_term", "at_creation", "cat"),
    FeatureSpec("sess_first_traffic_source", "pre_lead", "cat"),
    FeatureSpec("sess_source_engine", "pre_lead", "cat"),
    FeatureSpec("sess_direct_platform_type", "at_creation", "cat"),
    FeatureSpec("sess_direct_condition_type", "at_creation", "cat"),
    FeatureSpec("sess_direct_phrase_bucket", "at_creation", "cat"),
    FeatureSpec("sess_has_gclid", "at_creation", "num"),
    FeatureSpec("sess_phone_model", "pre_lead", "cat"),
    FeatureSpec("sess_network_type", "pre_lead", "cat"),
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
