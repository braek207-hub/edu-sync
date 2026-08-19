# -*- coding: utf-8 -*-
"""
sync/agent/writer/plan.py — желаемое состояние кабинета.

Декларативно: план описывает, КАК ДОЛЖНО БЫТЬ, а не какие запросы слать.
Разницу считает diff, отправляет apply. Так повторный прогон безопасен —
если состояние уже совпадает, действий не будет.

Источник — edu_agent_computed_settings, посчитанные на Э0. Расписание
(schedule:*) в Э1a не применяется: у него другой механизм (TimeTargeting
в стратегии кампании), он войдёт отдельной задачей позже.

Единицы: percent — ДЕЛЬТА (30 = «+30 %»), как её считает Э0 и как её читает
человек. Перевод в 100-базный коэффициент Директа делается ровно на границе с
API (sync/agent/writer/units.py), а не здесь.

Тип корректировки определяется ПАРОЙ (вид, ключ), а не одним видом. Для
устройств у Директа это разные типы корректировок — MOBILE_ADJUSTMENT,
DESKTOP_ADJUSTMENT, TABLET_ADJUSTMENT (образец разбора: sync/edu_direct_settings.py
:844-866). Отображение «любое устройство → MOBILE_ADJUSTMENT» отправило бы
коэффициент, посчитанный для десктопа, как коэффициент смартфонов.

Значения, которые агент применить не умеет, НЕ подставляются в чужой тип и
не роняют применение: они возвращаются отдельным списком с явной причиной и
видны в отчёте прогона.
"""

from typing import Any, Dict, List, Optional, Tuple

# Вид вычисленной настройки → тип корректировки в API Директа. Устройства
# сюда не входят: у них тип зависит от ключа, см. DEVICE_TYPE_MAP.
SETTING_KIND_MAP: Dict[str, str] = {
    "bid_modifier:gender": "DEMOGRAPHICS_ADJUSTMENT",
    "bid_modifier:age": "DEMOGRAPHICS_ADJUSTMENT",
    "bid_modifier:region": "REGIONAL_ADJUSTMENT",
}

DEVICE_KIND = "bid_modifier:device"

# Ключ устройства → тип корректировки. Ключ приходит из среза Reports API
# (поле Device); сравнение регистронезависимое, а канонической формой ключа
# в плане и в нормализованном факте считается ВЕРХНИЙ регистр — иначе план
# ("mobile") и факт из API ("MOBILE") не сойдутся по паре (тип, ключ) никогда.
DEVICE_TYPE_MAP: Dict[str, str] = {
    "DESKTOP": "DESKTOP_ADJUSTMENT",
    "MOBILE": "MOBILE_ADJUSTMENT",
    "TABLET": "TABLET_ADJUSTMENT",
}

MIN_SUPPORT = 100        # ниже — не трогаем, даже если сжатие дало заметное значение
MIN_ABS_PERCENT = 5      # корректировка меньше ±5% не стоит запроса и риска

UNSUPPORTED_DEVICE_REASON = (
    "устройство вне списка применимых (DESKTOP/MOBILE/TABLET): "
    "подставлять его коэффициент в чужой тип корректировки нельзя"
)
# Reports API отдаёт регион полем TargetingLocationName — это НАЗВАНИЕ
# («Москва»), а RegionalAdjustment требует числовой RegionId. Пока срез не
# отдаёт идентификатор (кандидат TargetingLocationId не проверен probe —
# см. probe_report_fields.py и комментарий в sync/agent/segments.py),
# региональные корректировки не применяются. Это осознанная пауза с причиной
# в отчёте прогона, а не падение: int("Москва") ронял бы действие в 'failed'
# и переприменял его каждый прогон, съедая слоты лимита действий.
UNSUPPORTED_REGION_REASON = (
    "ключ региона не числовой RegionId (срез отдаёт название): "
    "региональные корректировки не применяются до появления TargetingLocationId"
)


def direct_type_for(kind: str, key: str) -> Tuple[Optional[str], str, str]:
    """(вид, ключ) → (тип корректировки, канонический ключ, причина отказа).

    Тип None значит «применить нельзя»; причина непустая и уходит в отчёт.
    """
    if kind == DEVICE_KIND:
        canonical = str(key).strip().upper()
        direct_type = DEVICE_TYPE_MAP.get(canonical)
        if direct_type is None:
            return None, canonical, UNSUPPORTED_DEVICE_REASON
        return direct_type, canonical, ""

    direct_type = SETTING_KIND_MAP.get(kind)
    if direct_type is None:
        return None, str(key), ""

    if direct_type == "REGIONAL_ADJUSTMENT" and not str(key).strip().isdigit():
        return None, str(key), UNSUPPORTED_REGION_REASON

    return direct_type, str(key).strip(), ""


def plan_bid_modifiers(
    computed: List[Dict[str, Any]],
    min_support: int = MIN_SUPPORT,
    min_abs_percent: int = MIN_ABS_PERCENT,
) -> Dict[str, List[Dict[str, Any]]]:
    """План корректировок: {"desired": [...], "unsupported": [...]}.

    unsupported — строки, прошедшие пороги значимости, но которые агент
    применить не умеет. Они не выбрасываются молча: без явного списка
    невозможно отличить «регион не нужен» от «регион отвалился».
    """
    desired: List[Dict[str, Any]] = []
    unsupported: List[Dict[str, Any]] = []

    for row in computed:
        kind = str(row.get("setting_kind") or "")
        if kind != DEVICE_KIND and kind not in SETTING_KIND_MAP:
            continue
        if int(row.get("support_n") or 0) < min_support:
            continue
        percent = int(round(float(row.get("value") or 0.0)))
        if abs(percent) < min_abs_percent:
            continue

        direct_type, key, reason = direct_type_for(kind, str(row.get("setting_key")))
        if direct_type is None:
            unsupported.append({"kind": kind, "key": key, "percent": percent,
                                "reason": reason})
            continue

        desired.append({
            "kind": kind,
            "direct_type": direct_type,
            "key": key,
            "percent": percent,
        })

    return {
        "desired": sorted(desired, key=lambda r: (r["kind"], r["key"])),
        "unsupported": sorted(unsupported, key=lambda r: (r["kind"], r["key"])),
    }


def desired_bid_modifiers(
    computed: List[Dict[str, Any]],
    min_support: int = MIN_SUPPORT,
    min_abs_percent: int = MIN_ABS_PERCENT,
) -> List[Dict[str, Any]]:
    """Только применимая часть плана. Причины отказов — в plan_bid_modifiers."""
    return plan_bid_modifiers(computed, min_support, min_abs_percent)["desired"]
