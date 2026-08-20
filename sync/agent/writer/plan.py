# -*- coding: utf-8 -*-
"""
sync/agent/writer/plan.py — желаемое состояние кабинета.

Декларативно: план описывает, КАК ДОЛЖНО БЫТЬ, а не какие запросы слать.
Разницу считает diff, отправляет apply. Так повторный прогон безопасен —
если состояние уже совпадает, действий не будет.

Источник — edu_agent_computed_settings, посчитанные на Э0. Расписание
(schedule:*) в Э1a не применяется: у него другой механизм (TimeTargeting
в стратегии кампании), он войдёт отдельной задачей позже. «Не применяется»
здесь значит «видно в отчёте с причиной», а не «выпало молча»: любая значимая
строка, для которой не нашлось типа корректировки, уходит в unsupported.

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
GENDER_KIND = "bid_modifier:gender"
AGE_KIND = "bid_modifier:age"

# Допустимые значения демографических сегментов Директа. Перечни не выдуманы:
# это те же списки, по которым рабочий код проекта РАЗБИРАЕТ корректировки,
# прочитанные из кабинета (sync/edu_direct_settings.py::_GENDER_RU и _AGE_RU,
# строки 43–51). Здесь они стоят на ЗАПИСИ.
#
# Зачем список вообще. У устройств допустимые значения перечислены
# (DEVICE_TYPE_MAP), у региона стоит проверка на числовой идентификатор, а у
# пола и возраста не было ничего: любой ключ уезжал в тело запроса. Отчёты
# Директа штатно отдают значение «не определено» (UNKNOWN) и по полу, и по
# возрасту — сегмент крупный, порог по объёму наблюдений проходит легко, — а
# в bidmodifiers.add такого значения нет. Итог: отказ уровня элемента,
# статус 'rejected' и вечная переотправка каждый прогон.
GENDER_KEYS: Tuple[str, ...] = ("GENDER_MALE", "GENDER_FEMALE")
AGE_KEYS: Tuple[str, ...] = (
    "AGE_0_17", "AGE_18_24", "AGE_25_34", "AGE_35_44", "AGE_45_54", "AGE_55",
)

# Ключ демографического сегмента → поле тела запроса bidmodifiers.add.
# Явное отображение, а не «если не пол, значит возраст»: неизвестный ключ при
# таком «иначе» молча становился возрастом и уходил в API.
DEMOGRAPHIC_FIELD: Dict[str, str] = {
    **{k: "Gender" for k in GENDER_KEYS},
    **{k: "Age" for k in AGE_KEYS},
}

# Вид настройки → его перечень допустимых ключей.
DEMOGRAPHIC_KEYS_BY_KIND: Dict[str, Tuple[str, ...]] = {
    GENDER_KIND: GENDER_KEYS,
    AGE_KIND: AGE_KEYS,
}

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
# Демографический сегмент вне перечня API. Первый и самый частый постоялец —
# «не определено» (UNKNOWN): отчёты Директа отдают такой сегмент штатно, а
# запрос на создание корректировки такого значения не принимает.
UNSUPPORTED_DEMOGRAPHIC_REASON = (
    "демографический сегмент «{key}» вне перечня API Директа ({allowed}): "
    "отчёты отдают и «не определено», а корректировка такого значения не "
    "принимает — элемент был бы отклонён и переотправлялся каждый прогон"
)
# Вид настройки, которого нет ни в SETTING_KIND_MAP, ни среди устройств.
# Молчаливое выпадение здесь уже стоило дорого: расписание (schedule:*) в Э1a
# не применяется сознательно, но так же молча исчезал и bid_modifier:network —
# посчитанный, значимый и никем не замеченный. «Не применяем» и «в данных
# нет» обязаны различаться в отчёте.
UNSUPPORTED_KIND_REASON = (
    "вид настройки «{kind}» движок Э1a не применяет: его нет ни среди "
    "корректировок (устройство/пол/возраст/регион), ни в справочнике типов "
    "API — применение такой строки не запланировано, а не потеряно"
)


def direct_type_for(kind: str, key: str) -> Tuple[Optional[str], str, str]:
    """(вид, ключ) → (тип корректировки, канонический ключ, причина отказа).

    Тип None значит «применить нельзя»; причина непустая ВСЕГДА и уходит в
    отчёт. Пустая причина при отказе означала бы молчаливое выпадение строки —
    ровно то, из-за чего значимые корректировки исчезали, не попадая ни в
    план, ни в «неподдерживаемые».
    """
    if kind == DEVICE_KIND:
        canonical = str(key).strip().upper()
        direct_type = DEVICE_TYPE_MAP.get(canonical)
        if direct_type is None:
            return None, canonical, UNSUPPORTED_DEVICE_REASON
        return direct_type, canonical, ""

    direct_type = SETTING_KIND_MAP.get(kind)
    if direct_type is None:
        return None, str(key), UNSUPPORTED_KIND_REASON.format(kind=kind or "—")

    if direct_type == "REGIONAL_ADJUSTMENT" and not str(key).strip().isdigit():
        return None, str(key), UNSUPPORTED_REGION_REASON

    if direct_type == "DEMOGRAPHICS_ADJUSTMENT":
        # Канонический регистр — верхний, как у устройств и как в самих
        # ответах API: иначе план ("gender_male") и факт ("GENDER_MALE") не
        # сойдутся по паре (тип, ключ) никогда.
        canonical = str(key).strip().upper()
        allowed = DEMOGRAPHIC_KEYS_BY_KIND.get(kind, ())
        if canonical not in allowed:
            return None, canonical, UNSUPPORTED_DEMOGRAPHIC_REASON.format(
                key=canonical or "—", allowed="/".join(allowed))
        return direct_type, canonical, ""

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
        # Отбор по виду настройки СНЯТ намеренно. Прежде незнакомый вид
        # (schedule:*, bid_modifier:network) выпадал здесь молча: строка не
        # попадала ни в desired, ни в unsupported, и отчёт прогона был
        # неотличим от «таких данных нет». Теперь незнакомый вид проходит
        # те же пороги значимости, что и остальные, и отказ по нему выносит
        # direct_type_for — с причиной.
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
