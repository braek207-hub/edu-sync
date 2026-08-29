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

from sync.agent.confidence import assess
from sync.agent.writer import negatives, placements, tcpa

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

# DESKTOP и TABLET в одной кампании НЕСОВМЕСТИМЫ. Установлено экспериментом
# (прогоны 32559366898 → 32561294615 → 32561534117), вопреки справочнику
# Яндекса, где это разные категории устройств: планшет поверх десктопной
# корректировки отвергается с Code 6000 «Условия в корректировках
# пересекаются», а он же в кампанию без десктопной — принимается.
#
# Способ отправки НИ ПРИ ЧЁМ — проверено чистым опытом 32566357857 на пустой
# кампании с нейтральными значениями:
#   A) DESKTOP + TABLET одним запросом  → отвергнуты ОБА;
#   B) MOBILE + DESKTOP + TABLET        → мобильный принят, эти два отвергнуты;
#   C) по одной подряд (контроль)       → десктоп принят, планшет после — нет.
# То есть пара несовместима сама по себе, а MOBILE совместим с любым из них.
# Заодно видно, что в наборе Директ отвергает ОБА конфликтующих элемента, а не
# оставляет один: послать пару «на удачу» нельзя, потеряются обе корректировки.
#
# Раз применить обе нельзя, выбор делается по объёму наблюдений: у сегмента с
# бОльшим support_n оценка надёжнее. Проигравший уходит в unsupported с
# причиной — молча терять его нельзя, иначе «планшет не нужен» и «планшет
# отвалился» снова станут неотличимы.
DEVICE_EXCLUSIVE_PAIR = ("DESKTOP", "TABLET")
DEVICE_EXCLUSIVE_REASON = (
    "DESKTOP и TABLET в одной кампании несовместимы (Директ отвечает Code 6000 "
    "«условия в корректировках пересекаются»): применён сегмент с бОльшим "
    "объёмом наблюдений, этот вытеснен"
)

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

    if direct_type == "REGIONAL_ADJUSTMENT":
        # Ноль — не регион: так Reports API помечает строку без определённого
        # места («не определено»). isdigit его пропускает, а bidmodifiers.add
        # отверг бы элемент, и он переотправлялся бы каждый прогон.
        canonical = str(key).strip()
        if not canonical.isdigit() or int(canonical) <= 0:
            return None, canonical, UNSUPPORTED_REGION_REASON

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


def segment_shares(computed: List[Dict[str, Any]]) -> Dict[Tuple[str, str], float]:
    """Доля каждого сегмента внутри своего вида: {(вид, ключ): 0..1}.

    Нужна цене риска: корректировка сегмента ставит под удар только его долю
    объекта, а не весь расход кампании (writer/exposure.py). Считается по
    support_n — кликам сегмента, которые расчёт Э0 уже принёс со строкой;
    отдельного обращения к витрине не требуется.

    Клики, а не расход: расхода в разрезе сегмента строка не несёт. Разница
    между долей кликов и долей денег — это разница в цене клика между
    сегментами (десктоп дороже мобильного), она в пределах полутора раз и
    целиком покрыта запасом BID_ELASTICITY. Занижения доли она не создаёт
    систематически, а завышение цены риска не опасно — оно консервативно.

    Знаменатель — сумма support_n строк того же вида. В расчёт попадают
    только сегменты, прошедшие порог наблюдений, поэтому знаменатель немного
    меньше полного объёма, а доли — немного больше настоящих. Это тоже в
    консервативную сторону.
    """
    totals: Dict[str, float] = {}
    for row in computed:
        kind = str(row.get("setting_kind") or "")
        if not kind.startswith("bid_modifier:"):
            continue
        totals[kind] = totals.get(kind, 0.0) + float(row.get("support_n") or 0)

    shares: Dict[Tuple[str, str], float] = {}
    for row in computed:
        kind = str(row.get("setting_kind") or "")
        total = totals.get(kind, 0.0)
        if total <= 0:
            continue
        shares[(kind, str(row.get("setting_key")))] = (
            float(row.get("support_n") or 0) / total)
    return shares


def plan_bid_modifiers(
    computed: List[Dict[str, Any]],
    min_support: int = MIN_SUPPORT,
    min_abs_percent: int = MIN_ABS_PERCENT,
    thresholds: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """План корректировок:
    {"desired": [...], "unsupported": [...], "low_confidence": [...],
     "confidence_unknown": N}.

    unsupported — строки, прошедшие пороги значимости, но которые агент
    применить не умеет. Они не выбрасываются молча: без явного списка
    невозможно отличить «регион не нужен» от «регион отвалился».

    low_confidence (Э2.3) — строки, у которых вероятность, что знак эффекта
    верен, ниже порога класса bid_modifier: применять такое — монетка, а не
    решение. confidence_unknown — строки БЕЗ rel_error (расчёт старого
    формата): для обратимого класса они применяются как раньше, но их число
    видно — молчаливое «неизвестно = уверены» недопустимо.
    """
    desired: List[Dict[str, Any]] = []
    unsupported: List[Dict[str, Any]] = []
    low_confidence: List[Dict[str, Any]] = []
    confidence_unknown = 0

    shares = segment_shares(computed)

    for row in computed:
        kind = str(row.get("setting_kind") or "")
        # Вид с собственным рычагом здесь пропускается: его применяет соседняя
        # ветка, а не корректировки. См. OWN_LEVER_KINDS — реестр обязан быть
        # один, иначе очередной рычаг снова окажется «неприменяемым» в отчёте.
        if kind in OWN_LEVER_KINDS or kind in NOT_A_SETTING_KINDS:
            continue
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

        # Э2.3: уверенность в знаке против порога класса. Гейт стоит ПОСЛЕ
        # порогов значимости (незначимое и так не действие) и ДО разбора типа:
        # неуверенная строка не должна занимать место в desired независимо от
        # того, умеет ли движок её применять.
        verdict = assess(float(row.get("raw_value") or 0.0),
                         row.get("rel_error"), "bid_modifier", thresholds)
        if verdict["confident"] is None:
            confidence_unknown += 1
        elif not verdict["confident"]:
            low_confidence.append({
                "kind": kind, "key": str(row.get("setting_key")),
                "percent": percent, "p_sign": verdict["p_sign"],
                "min_p_sign": verdict["min_p_sign"],
            })
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
            "support_n": int(row.get("support_n") or 0),
            # Доля объекта, которую занимает сегмент, — множитель цены риска
            # (writer/exposure.py). None значит «установить не удалось»: там
            # под ударом считается весь объект.
            "share": shares.get((kind, str(row.get("setting_key")))),
        })

    desired, crowded_out = _resolve_device_exclusion(desired)
    unsupported += crowded_out

    return {
        "desired": sorted(desired, key=lambda r: (r["kind"], r["key"])),
        "unsupported": sorted(unsupported, key=lambda r: (r["kind"], r["key"])),
        "low_confidence": sorted(low_confidence,
                                 key=lambda r: (r["kind"], r["key"])),
        "confidence_unknown": confidence_unknown,
    }


def _resolve_device_exclusion(
    desired: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Оставляет из пары DESKTOP/TABLET один сегмент — тот, где данных больше.

    Директ принимает только одну корректировку из этой пары (см.
    DEVICE_EXCLUSIVE_PAIR). Раньше движок планировал обе, вторая получала отказ
    уровня элемента и переотправлялась каждый прогон, пока не упиралась в
    счётчик попыток. Теперь конфликт разрешается ДО отправки.
    """
    by_key = {r["key"]: r for r in desired
              if r["kind"] == DEVICE_KIND and r["key"] in DEVICE_EXCLUSIVE_PAIR}
    if len(by_key) < 2:
        return desired, []

    first, second = (by_key[k] for k in DEVICE_EXCLUSIVE_PAIR)
    # Больший объём наблюдений выигрывает; при равенстве — DESKTOP, он покрывает
    # больше трафика. Выбор обязан быть детерминированным: иначе один и тот же
    # расчёт давал бы разные планы от прогона к прогону.
    loser = second if first["support_n"] >= second["support_n"] else first

    kept = [r for r in desired if r is not loser]
    return kept, [{"kind": loser["kind"], "key": loser["key"],
                   "percent": loser["percent"], "reason": DEVICE_EXCLUSIVE_REASON}]


SCHEDULE_KIND = "schedule:hour"
# Вид строк Э3.2 (portfolio.computed_rows): их применяет рычаг бюджета.
BUDGET_TARGET_KIND = "budget_target"
# Вид строк Э3.4 (там же): кандидаты на выключение, применяет writer/switch.py.
CAMPAIGN_SWITCH_KIND = "campaign_switch"
# Расчётная строка Э3.1: коэффициенты кривой насыщения. В кабинет не пишется
# никогда и ничем — это вход портфеля, а не настройка кампании.
SATURATION_KIND = "saturation"

# Виды, у которых СВОЙ механизм записи. Общая ветка корректировок их
# пропускает — не потому, что не умеет, а потому, что применяет их соседний
# рычаг. Реестр держится в одном месте и собирается из констант самих
# рычагов: пока это были три вида в трёх if-ах, добавленные позже минус-фразы,
# площадки и целевой CPA молча уехали в unsupported, и боевой отчёт 25.08
# уверял, что «применение не запланировано» — про 3 934 строки минус-фраз,
# которые в этом же прогоне раскладывались по 27 кампаниям.
OWN_LEVER_KINDS = frozenset({
    SCHEDULE_KIND,
    BUDGET_TARGET_KIND,
    CAMPAIGN_SWITCH_KIND,
    negatives.NEGATIVE_SETTING_KIND,
    placements.PLACEMENT_SETTING_KIND,
    tcpa.TCPA_SETTING_KIND,
})

# Виды, которые вообще не являются настройкой кабинета: расчёт, который Э0
# кладёт в ту же таблицу. Отличать их от «умеем, но другим рычагом» важно —
# иначе непонятно, ждать ли для них когда-нибудь записи.
NOT_A_SETTING_KINDS = frozenset({SATURATION_KIND})
# Часов в профиле 24, и порог значимости у каждого свой. Но само расписание —
# ОДНО действие на кампанию: Директ принимает Schedule целиком, а не по часу.
# Поэтому решение «менять ли расписание» принимается по набору: хотя бы один
# час должен пройти пороги, иначе запрос не стоит риска.
SCHEDULE_MIN_HOURS = 1


def plan_schedule(
    computed: List[Dict[str, Any]],
    min_support: int = MIN_SUPPORT,
    min_abs_percent: int = MIN_ABS_PERCENT,
) -> List[Dict[str, Any]]:
    """Часы профиля, прошедшие пороги значимости.

    Пустой список значит «расписание не трогаем»: либо наблюдений мало, либо
    отклонения в пределах шума. Незначимые часы отбрасываются ЗДЕСЬ, а не
    внутри построения строк, — там час без данных обязан остаться нейтральным,
    и отличить «нет данных» от «данные есть, но слабые» уже было бы нельзя.
    """
    out: List[Dict[str, Any]] = []
    for row in computed:
        if str(row.get("setting_kind") or "") != SCHEDULE_KIND:
            continue
        if int(row.get("support_n") or 0) < min_support:
            continue
        percent = int(round(float(row.get("value") or 0.0)))
        if abs(percent) < min_abs_percent:
            continue
        out.append({"setting_kind": SCHEDULE_KIND,
                    "setting_key": str(row.get("setting_key")),
                    "value": percent,
                    "support_n": int(row.get("support_n") or 0)})
    if len(out) < SCHEDULE_MIN_HOURS:
        return []
    return sorted(out, key=lambda r: int(r["setting_key"]))
