# -*- coding: utf-8 -*-
"""
sync/agent/writer/audience.py — Ф15 (запись): рычаг аудиторий и ретаргетинга.

Корректировка ставки на условие ретаргетинга — та же ручка, что у пола,
возраста и устройств, только адресованная сегменту аудитории. Отсюда три
следствия, и все они содержательные, а не оформительские.

  ОБУЧЕНИЕ НЕ СБРАСЫВАЕТСЯ. Справка Директа перечисляет перезапускающие
  изменения поимённо: другая стратегия, смена модели атрибуции или оплаты,
  изменение ограничения расхода, корректировка ЦЕЛЕВЫХ ДЕЙСТВИЙ, остановка
  дольше семи дней. Корректировки ставки в этом списке нет — ни у устройств,
  ни у аудиторий, поэтому вид стоит в learning.SAFE_FOR_LEARNING рядом с
  bidmodifier.*, а не под кулдауном обучения.

  ПОЛОСА — ТОНКАЯ НАСТРОЙКА, А НЕ ПЕРЕРАСПРЕДЕЛЕНИЕ. Полоса 3 меняет, КУДА
  кампания тратит свои деньги, и её ошибка стоит недель переобучения
  стратегии; здесь деньги переносятся ВНУТРИ кампании между сегментом и
  остальным объектом, а ошибка видна за неделю (lanes.MEASURE_DAYS полосы
  tuning). Полоса задаёт горизонт замера, риск-долю и лимит объектов за
  прогон — перепутанная полоса мерила бы действие чужим окном и платила бы за
  него из чужого кармана.

  ПОД УДАРОМ ДОЛЯ СЕГМЕНТА, ЕСЛИ ОНА ИЗВЕСТНА. Цена считается общим правилом
  (exposure.bid_modifier_exposure) и без исключений: доля сегмента × сила
  сдвига, а неизвестная доля означает «под ударом весь объект». Своей скидки
  у рычага нет и быть не может — подстановка средней доли не отличалась бы от
  «сегмент маленький» ничем, и гарантия недельного лимита перестала бы
  держаться.

ЧТО ЗДЕСЬ ГЛАВНОЕ — ЖИВ ЛИ СЕГМЕНТ. Замер 26.08.2026 по четырём кабинетам
(probe_retargeting_lever.py, разбор в docs/AGENT-EXPERIMENT-PIPELINE.md):
форма запроса верна, отчёты по критериям принимаются, а привязок условий к
группам объявлений — НОЛЬ, и расхода через ретаргетинг 375 кликов на четыре
кабинета за месяц. Условие, не привязанное ни к одной группе, не участвует ни
в одном показе: корректировка ставки по нему отчитается успехом и не изменит
ничего. Поэтому непривязанный сегмент — отказ, и неизвестная привязка тоже:
«не знаем, привязан ли» и «привязан» — разные утверждения.

ФОРМА ЗАПРОСА НЕ ВЫДУМАНА. bidmodifiers.add с блоком RetargetingAdjustments
[{RetargetingConditionId, BidModifier}] — та же форма, которую читает из
кабинета sync/edu_direct_settings.py (:886 разбор RetargetingAdjustment,
:927 запрос RetargetingAdjustmentFieldNames) и которую подтвердил probe
заведомо несуществующим идентификатором: ответ 8800 «объект не найден» —
ошибка уровня ЭЛЕМЕНТА, то есть тело разобрано и форма принята.

ЧЕГО РЫЧАГ НЕ ДЕЛАЕТ. Не правит уже стоящую корректировку: add поверх живого
объекта создаёт в кабинете ВТОРОЙ объект на тот же сегмент, а правка
существующего — это bidmodifier.set, и коэффициент там мог поставить человек.
И не считает молчание кабинета пустотой: боевой читатель прогона
(agent_e1._actual_modifiers) сегодня не запрашивает
RetargetingAdjustmentFieldNames вовсе, поэтому состояние без прочитанного
списка — отказ, а не «корректировок нет».
"""

from typing import Any, Dict, List, Optional, Tuple

from sync.agent.writer import exposure, expectation, guardrails
from sync.agent.writer.diff import bidmod_idempotency_key

AUDIENCE_KIND = "audience.add"

# Тип корректировки в терминах API Директа. Тот же литерал читает из кабинета
# sync/edu_direct_settings.py; вторая его форма означала бы, что план и факт
# не сойдутся по паре (тип, ключ) никогда.
RETARGETING_TYPE = "RETARGETING_ADJUSTMENT"

# Ключ состояния кампании со списком уже стоящих корректировок на ретаргетинг.
# Отсутствие ключа — «не читали», пустой список — «читали, их нет».
MODIFIERS_KEY = "retargeting_modifiers"

UNREAD_REASON = (
    "список корректировок на ретаргетинг у кампании не прочитан: молчание — "
    "не «их нет», а добавление поверх живой корректировки создало бы в "
    "кабинете второй объект на тот же сегмент"
)
NOT_ATTACHED_REASON = (
    "условие {condition} не привязано ни к одной группе кампании: сегмент не "
    "участвует ни в одном показе, и корректировка ставки по нему не изменит "
    "ничего (замер 26.08.2026: 0 привязок в четырёх кабинетах)"
)
UNKNOWN_ATTACHMENT_REASON = (
    "неизвестно, привязано ли условие {condition} к группам кампании: «не "
    "знаем» — не то же самое, что «привязано», и корректировка ушла бы вслепую"
)
ALREADY_SET_REASON = (
    "на условие {condition} уже стоит корректировка {percent}%: править её — "
    "дело bidmodifier.set, а add создал бы второй объект на тот же сегмент"
)
UNUSABLE_REASON = (
    "корректировка на условие {condition} прочитана негодной (в ответе API не "
    "оказалось коэффициента): прошлое состояние сегмента неизвестно"
)
BAD_CONDITION_REASON = (
    "адрес сегмента {condition!r} не идентификатор: RetargetingConditionId — "
    "число, и строку Директ отвергнет отказом уровня элемента, то есть внутри "
    "успешного ответа"
)
ZERO_SHIFT_REASON = (
    "сдвиг ноль: в кабинете появился бы объект, который ничего не меняет"
)
BAD_PERCENT_REASON = "сила сдвига {percent!r} не читается числом"
OVER_CAP_REASON = (
    "сдвиг {percent}% выше общего потолка корректировки ±{cap}%: своего "
    "потолка у рычага нет, и негодный кандидат не имеет права занимать "
    "единственное место кампании"
)
ONE_PER_TICK_REASON = (
    "за такт кампания получает одну корректировку аудитории, и её взял "
    "сегмент {winner}: две правки за такт замеряются одним исходом, разделить "
    "их вклад потом нечем"
)


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None   # NaN — то же «неизвестно»


def _condition_id(value: Any) -> Optional[int]:
    """Идентификатор условия ретаргетинга. None — адрес непригоден.

    Булево отсекается явно: True прошёл бы через int() единицей и адресовал
    бы корректировку условию с идентификатором 1.
    """
    if isinstance(value, bool):
        return None
    number = _number(value)
    if number is None or number != int(number) or int(number) <= 0:
        return None
    return int(number)


def _percent(value: Any) -> Optional[int]:
    number = _number(value)
    if number is None or number != int(number):
        return None
    return int(number)


def _value_of(percent: int, share: Optional[float]) -> float:
    """Порядок ценности кандидатов ОДНОЙ кампании.

    Это не вторая модель обещания, а его ПОРЯДОК. Обещание корректировки
    (expectation._bid_modifier) — расход × доля × сдвиг × сдвиг ÷ цена лида ×
    дни; внутри одной кампании расход, цена лида и горизонт общие, и
    сравнение по оставшимся множителям даёт тот же порядок. Считать здесь
    само обещание значило бы требовать экономику кампании там, где она
    отсутствует: без цены лида обещания нет вовсе, а выбрать лучшего из
    кандидатов надо и тогда.

    Доля неизвестна — ценность ноль: незнание не аргумент, и такой кандидат
    не имеет права вытеснить посчитанного. Права участвовать он при этом не
    теряет — если других нет, он поедет по цене «весь объект под ударом».
    """
    if share is None:
        return 0.0
    move = abs(percent) / 100.0
    return float(share) * move * move


def _refusal(campaign_id: str, condition: Any, reason: str) -> Dict[str, Any]:
    return {"campaign_id": str(campaign_id), "condition_id": condition,
            "reason": reason}


def _existing(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Прочитанные корректировки на ретаргетинг: ключ сегмента → запись.

    Форма записи — та же, что у нормализованного факта корректировок
    (agent_e1._normalize_actual): Id, Type, key, percent и признак unusable.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for item in state.get(MODIFIERS_KEY) or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("Type")) != RETARGETING_TYPE:
            continue
        out[str(item.get("key"))] = item
    return out


def _screen(item: Dict[str, Any], existing: Dict[str, Dict[str, Any]],
                       ) -> Tuple[Optional[Tuple[int, int, Optional[float]]],
                                  Optional[str]]:
    """Кандидат → ((условие, сдвиг, доля), None) либо (None, причина отказа)."""
    condition = _condition_id(item.get("condition_id"))
    if condition is None:
        return None, BAD_CONDITION_REASON.format(condition=item.get("condition_id"))

    percent = _percent(item.get("percent"))
    if percent is None:
        return None, BAD_PERCENT_REASON.format(percent=item.get("percent"))
    if percent == 0:
        return None, ZERO_SHIFT_REASON
    if abs(percent) > guardrails.MODIFIER_CAP:
        return None, OVER_CAP_REASON.format(percent=percent,
                                            cap=guardrails.MODIFIER_CAP)

    attached = _number(item.get("attached_ad_groups"))
    if attached is None:
        return None, UNKNOWN_ATTACHMENT_REASON.format(condition=condition)
    if attached <= 0:
        return None, NOT_ATTACHED_REASON.format(condition=condition)

    current = existing.get(str(condition))
    if current is not None:
        if current.get("unusable"):
            return None, UNUSABLE_REASON.format(condition=condition)
        return None, ALREADY_SET_REASON.format(
            condition=condition, percent=int(current.get("percent") or 0))

    return (condition, percent, _number(item.get("share"))), None


def _action_for(campaign_id: str, condition: int, percent: int,
                share: Optional[float], move: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "CampaignId": int(campaign_id),
        "Type": RETARGETING_TYPE,
        "key": str(condition),
        # Сдвиг лежит в payload в ДЕЛЬТАХ, как весь внутренний план: перевод в
        # 100-базу API происходит на границе (apply.to_api_call). Он же —
        # поле, по которому общая рельса считает потолок ±50 %, и свой потолок
        # рычагу заводить не нужно.
        "BidModifier": percent,
    }
    return expectation.attach({
        "action_kind": AUDIENCE_KIND,
        "object_level": "campaign",
        "object_id": str(campaign_id),
        "exposure": exposure.bid_modifier_exposure(percent, share),
        # Тип и ключ на верхнем уровне действия: по ним адресуются кулдаун
        # после отката и счётчик попыток, и без них сегмент жил бы только
        # внутри payload — то есть вне обоих механизмов.
        "direct_type": RETARGETING_TYPE,
        "key": str(condition),
        "payload": payload,
        # Объекта до действия не существовало, и пустой словарь здесь —
        # утверждение, а не пропуск: откат добавления возвращает нейтраль, а
        # не прежний коэффициент (rollback.rollback_payload).
        "previous_state": {},
        # Ключ считается ТЕМ ЖЕ способом, что у корректировки сегмента: одно и
        # то же изменение кабинета не имеет права приезжать под двумя ключами,
        # иначе закрытый ключ прошлого прогона не отсечёт повтор.
        "idempotency_key": bidmod_idempotency_key(
            str(campaign_id), RETARGETING_TYPE, str(condition), percent),
    }, {**{k: v for k, v in move.items() if k != "audiences"},
        "segment_share": share})


def diff_audience(
    desired: Dict[str, Dict[str, Any]],
    actual_by_campaign: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Желаемые аудитории × прочитанное состояние кабинета → (действия, отказы).

    desired — по кампании: список audiences (условие ретаргетинга, сдвиг
    ставки, доля сегмента в расходе, число групп, к которым условие
    привязано) и экономика кампании для обещания (расход в день и цена лида —
    ключи writer/expectation).

    actual_by_campaign — прочитанное состояние: по кампании список уже
    стоящих корректировок на ретаргетинг под ключом retargeting_modifiers.
    Кампании без записи в нём не порождают ни действия, ни отказа — их не
    оказалось в кабинете, и это видно счётчиком not_found вызывающего;
    кампания без самого списка отказывается: молчание — не пустота.

    На кампанию за такт выходит РОВНО ОДНО действие — самое ценное из
    прошедших проверки. Остальные пригодные кандидаты возвращаются отказом с
    названным победителем: две корректировки одной кампании в один такт
    замеряются одним исходом, разделить их вклад потом нечем, а риск-бюджет
    списывается за обе.
    """
    actions: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []

    for cid in sorted(desired):
        move = desired[cid] or {}
        state = actual_by_campaign.get(str(cid))
        if state is None:
            continue
        if MODIFIERS_KEY not in state:
            refused.append(_refusal(cid, None, UNREAD_REASON))
            continue

        existing = _existing(state)
        eligible: List[Tuple[int, int, Optional[float]]] = []
        for item in move.get("audiences") or ():
            if not isinstance(item, dict):
                continue
            passed, reason = _screen(item, existing)
            if passed is None:
                refused.append(_refusal(cid, item.get("condition_id"), reason))
                continue
            eligible.append(passed)
        if not eligible:
            continue

        # Порядок детерминирован до последнего разряда: при равной ценности
        # решает идентификатор условия, иначе один и тот же вход давал бы
        # разные такты.
        eligible.sort(key=lambda row: (-_value_of(row[1], row[2]), row[0]))
        winner = eligible[0]
        actions.append(_action_for(str(cid), winner[0], winner[1], winner[2], move))
        for condition, _percent_value, _share in eligible[1:]:
            refused.append(_refusal(cid, condition,
                                    ONE_PER_TICK_REASON.format(winner=winner[0])))
    return actions, refused


def to_api_call(action: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    """Действие → вызов API. Тонкая обёртка над общим сборщиком apply."""
    from sync.agent.writer.apply import to_api_call as apply_to_api_call
    return apply_to_api_call(action)
