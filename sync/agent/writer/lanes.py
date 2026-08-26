# -*- coding: utf-8 -*-
"""
sync/agent/writer/lanes.py — полосы действий вместо одного лимита на прогон.

Полоса — класс решений с общей физикой ошибки и общим сроком замера. Полосы не
конкурируют друг с другом ни за слоты, ни за деньги: у каждой свой лимит и своя
ступень автономии (autonomy.step_of).

Зачем разделение. Общий лимит прогона выбирал не важное, а ПЕРВОЕ: порядок
сборки плана — не порядок ценности, и корректировки сегментов, которых
генерится сотнями, вытесняли сдвиги бюджета, которых единицы (дефект d36d1c3).
Второй перекос — знак риска: минус-фраза деньги ВЫСВОБОЖДАЕТ, а платила из того
же кармана, что доливка бюджета. Третий — абсолют: 50 000 ₽/нед были 0,9 %
расхода кабинета при 5,7 млн ₽ в неделю (замер 26.08.2026) и молча меняли смысл
вместе с размером кабинета.

Семь полос (§1.3 плана беты):

  1. hygiene     — negative.add, placement.exclude; замер 3 дня; риском не платит
  2. tuning      — bidmodifier.*, schedule.set; сегмент кампании; 7 дней
  3. allocation  — budget.*, tcpa.set; кампания; 14 дней
  4. suspend     — campaign.suspend; ≤1 объект за прогон; 14 дней
  5. exploration — любой вид с признаком exploration; платит карман explore_share
  6. launch      — campaign.create/resume; новая кампания; 30 дней
  7. proposal    — не применяется никогда (Мастер кампаний, смысловые гипотезы)

Риск-долю платят ЧЕТЫРЕ полосы: tuning, allocation, suspend, launch. Гигиена не
платит по построению (её действия снимают деньги с огня, а не ставят под удар —
цена ошибки считается отдельно, exposure.cut_exposure), разведка платит из
разведочного кармана бюджета (portfolio.EXPLORATION_SHARE), предложения не
применяются вовсе. У гигиены свой ограничитель — доля вырезаемого расхода за
такт: сломанный источник данных не должен вырезать кабинет за один проход.

Карта LANE_OF_KIND знает виды действий, рычагов для которых ещё нет (Ф14–Ф15).
Это не задел на будущее, а предохранитель: вид, не попавший в карту, не имеет
ни лимита, ни цены и прошёл бы бесплатно — поэтому lane_of на незнакомом виде
падает, а не назначает полосу по умолчанию.

Модуль ничего не читает и никуда не пишет: вид действия → полоса, полоса и
ступень → её политика.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from sync.agent import autonomy
from sync.agent.experiments import is_bet
from sync.agent.writer.switch import MAX_SUSPENDS_PER_RUN

LANE_HYGIENE = "hygiene"
LANE_TUNING = "tuning"
LANE_ALLOCATION = "allocation"
LANE_SUSPEND = "suspend"
LANE_EXPLORATION = "exploration"
LANE_LAUNCH = "launch"
LANE_PROPOSAL = "proposal"

ALL_LANES: Tuple[str, ...] = (LANE_HYGIENE, LANE_TUNING, LANE_ALLOCATION,
                              LANE_SUSPEND, LANE_EXPLORATION, LANE_LAUNCH,
                              LANE_PROPOSAL)

# Вид действия → полоса. Ключей здесь БОЛЬШЕ, чем в guardrails.ALLOWED_ACTION_KINDS:
# карта идёт впереди allow-листа, вид пропускается к применению только вместе
# со своим рычагом и тестом.
LANE_OF_KIND: Dict[str, str] = {
    "negative.add": LANE_HYGIENE,
    "placement.exclude": LANE_HYGIENE,
    # Снятие своей же минус-фразы — исправление собственной арифметики, тот же
    # знак риска и тот же трёхдневный срок, что у её добавления (Ф14).
    "negative.remove_added": LANE_HYGIENE,

    "bidmodifier.add": LANE_TUNING,
    "bidmodifier.set": LANE_TUNING,
    "schedule.set": LANE_TUNING,
    # Аудитория — та же корректировка сегмента: обучение стратегии не
    # сбрасывает, замеряется на горизонте недели (задача 23).
    "audience.add": LANE_TUNING,

    "budget.set": LANE_ALLOCATION,
    "budget.set_daily": LANE_ALLOCATION,
    "tcpa.set": LANE_ALLOCATION,
    # Цель, стратегия и гео меняют, КУДА кампания тратит те же деньги, и сбрасывают
    # обучение ровно как сдвиг лимита, — значит их физика ошибки полосы 3
    # (задачи 21, 22, 24).
    "goal.set": LANE_ALLOCATION,
    "strategy.set": LANE_ALLOCATION,
    "geo.set": LANE_ALLOCATION,

    "campaign.suspend": LANE_SUSPEND,

    "campaign.create": LANE_LAUNCH,
    "campaign.resume": LANE_LAUNCH,
}

# Рекомендация человеку рычага записи не имеет; вид называется префиксом, чтобы
# её нельзя было спутать с действием и случайно отправить в кабинет.
PROPOSAL_KIND_PREFIX = "proposal."

# Ступень, на которой снимается изоляция «одно действие на объект»: класс уже
# доказан, и измеримость держат заповедник и замер такта (задача 25), а не
# искусственная редкость правок.
TOP_STEP = 3

# Доля расхода кабинета за такт, которую полосе 1 позволено вырезать. Гигиена
# риском не платит, поэтому это её единственный ограничитель — предохранитель
# сломанных данных: при недельном расходе 5,7 млн ₽ (замер 26.08.2026) это
# 285 000 ₽ вырезаемого расхода за прогон. Больше — уже не чистка, а
# остановка кабинета по ошибке в источнике.
HYGIENE_MAX_CUT_SHARE = 0.05

# Сроки замера по полосам — §1.3. Не «сколько ждать не скучно», а через сколько
# исход становится читаемым: вырезанный расход виден сразу, корректировка
# сегмента набирает статистику неделю, сдвиг лимита требует переобучения
# стратегии, новая кампания — выхода из обучения целиком.
MEASURE_DAYS: Dict[str, int] = {
    LANE_HYGIENE: 3,
    LANE_TUNING: 7,
    LANE_ALLOCATION: 14,
    LANE_SUSPEND: 14,
    LANE_EXPLORATION: 14,
    LANE_LAUNCH: 30,
    LANE_PROPOSAL: 0,
}

# Полосы, которые платят риск-долей. Остальные три — не послабление: у гигиены
# обратный знак риска, у разведки свой карман, предложения не применяются.
RISK_PAYING_LANES = frozenset({LANE_TUNING, LANE_ALLOCATION,
                               LANE_SUSPEND, LANE_LAUNCH})

# Полосы, где на одном объекте живёт несколько независимых правок (сегменты
# кампании, бюджет и цель) — только у них изоляция «одно на объект» имеет смысл
# и только у них она снимается на верхней ступени.
MULTI_LEVER_LANES = frozenset({LANE_TUNING, LANE_ALLOCATION})


@dataclass(frozen=True)
class LanePolicy:
    lane: str
    max_actions_per_object: Optional[int]   # None — без ограничения
    max_objects_per_run: Optional[int]
    risk_share: float                        # доля недельного расхода; 0.0 — не платит
    max_cut_share: Optional[float]           # только гигиена: доля расхода кабинета
    measure_days: int


def lane_of(action: Dict[str, Any]) -> str:
    """Полоса действия.

    Признак разведки перебивает вид: разведочный сдвиг бюджета — это покупка
    информации, у него снят гейт уверенности (portfolio._apply_exploration), и
    платить он обязан разведочным карманом, а не риском полосы 3. Останься он
    в своей полосе по виду — разведка съедала бы лимит доказанных решений и
    ходила бы по чужой ступени.
    """
    if is_bet(action):
        return LANE_EXPLORATION

    kind = str(action.get("action_kind") or "")
    if kind.startswith(PROPOSAL_KIND_PREFIX):
        return LANE_PROPOSAL

    lane = LANE_OF_KIND.get(kind)
    if lane is None:
        raise ValueError(
            f"вид действия {kind!r} не отнесён ни к одной полосе; "
            "вид без полосы не имеет ни лимита, ни цены и прошёл бы бесплатно")
    return lane


def policy_of(lane: str, step: int,
              config: Optional[Dict[str, Any]] = None) -> LanePolicy:
    """Что полосе положено на её ступени.

    config — активный конфиг панели (sync/agent/config.resolve). Оттуда берётся
    max_suspends_per_run: настройка была в панели до полос и остаётся в ней,
    став лимитом объектов полосы 4, — переезд не должен молча обнулять то, чем
    человек уже управлял.

    Ступень 0 — тень: полоса пишет «сделал бы X, жду Y к дате D», не делая.
    Нулевой риск-доли для этого мало: гигиена риском не платит вовсе и в тени
    продолжала бы применять. Поэтому тень обнуляет и счётчик объектов, и долю
    вырезаемого расхода.
    """
    if lane not in ALL_LANES:
        raise ValueError(f"неизвестная полоса: {lane!r}")
    share = autonomy.share_of(step)   # заодно проверяет ступень

    if lane == LANE_PROPOSAL or step == 0:
        return LanePolicy(
            lane=lane,
            max_actions_per_object=0,
            max_objects_per_run=0,
            risk_share=0.0,
            max_cut_share=0.0 if lane == LANE_HYGIENE else None,
            measure_days=MEASURE_DAYS[lane],
        )

    per_object: Optional[int] = 1
    if lane == LANE_HYGIENE:
        # Класс 0 вносится весь и сразу: это утверждения о прошлом, а не
        # прогнозы, и очередь из них — та самая, что сегодня стоит позади
        # корректировок. Ограничитель у полосы один, и он в рублях.
        per_object = None
    elif lane in MULTI_LEVER_LANES and step >= TOP_STEP:
        per_object = None

    return LanePolicy(
        lane=lane,
        max_actions_per_object=per_object,
        max_objects_per_run=_max_objects(lane, config),
        risk_share=share if lane in RISK_PAYING_LANES else 0.0,
        max_cut_share=HYGIENE_MAX_CUT_SHARE if lane == LANE_HYGIENE else None,
        measure_days=MEASURE_DAYS[lane],
    )


def _max_objects(lane: str, config: Optional[Dict[str, Any]]) -> Optional[int]:
    if lane != LANE_SUSPEND:
        # Остальные полосы объекты не рационируют: их держат риск-доля и
        # ограничитель гигиены. Счётчик объектов поверх денег вернул бы тот же
        # дефект «взяли первое, а не важное», только внутри полосы.
        return None
    # Выключения меняют состав портфеля, на котором посчитаны λ и целевые
    # бюджеты всех остальных кампаний: второе выключение за прогон принято по
    # экономике, которой после первого уже нет (writer/switch.py).
    raw = (config or {}).get("max_suspends_per_run", MAX_SUSPENDS_PER_RUN)
    return int(raw)
