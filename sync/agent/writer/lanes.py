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
from typing import Any, Dict, List, Optional, Tuple

from sync.agent import autonomy
from sync.agent import learning_loop
from sync.agent.experiments import is_bet
from sync.agent.writer import tier as tier_mod
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
    # Ступень полосы едет В САМОЙ политике, а не остаётся у вызывающего:
    # отбор обязан отличать «полоса в тени» от «полоса не влезла в свой
    # потолок». Нули лимитов у этих двух случаев одинаковые, а причина отказа
    # и лечение — разные: первую выпускает человек, второй помогает ступень.
    step: int = 1


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
            step=int(step),
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
        step=int(step),
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


# Ступень, с которой полоса работает, пока лестница автономии не подключена к
# прогону (autonomy.step_of — задача 26). Единица, а не ноль: ступень 0 — режим
# приёмки нового рычага, и запертая в ней полоса не наберёт закрытых наблюдений
# никогда. Кого держать в тени, решает человек, и решение приходит ступенью в
# step_by_lane, а не константой отсюда.
DEFAULT_STEP = 1


def default_step_of(lane: str) -> int:
    """Ступень полосы, пока человек не назначил ей свою.

    Единица для всех, кроме запуска. У запуска рычага записи на стороне
    агента нет вовсе — тело кампании собирает другой репозиторий, — и
    «приёмка нового рычага не заперта» про него неверно: принимать нечего.
    Ступень 1 по умолчанию означала бы, что кросс-минусовка доноров уезжает в
    кабинет тактом, в котором кампания заведомо не создана, а это ровно та
    потеря трафика, ради которой минусовка и связана с созданием.

    Ключ lane_steps панели перебивает это значение в обе стороны: решение
    выпустить полосу из тени принимает человек, и оно не должно требовать
    правки кода.
    """
    return 0 if lane == LANE_LAUNCH else DEFAULT_STEP


# Счётчики послужного списка, которые складываются при сборке ВИДОВ действий в
# ПОЛОСУ. Список закрытый и с именами из learning_loop: слот там ключуется
# видом (bidmodifier.add), а ступень выдаётся полосе, и складывать их надо
# ровно по этим полям — доли (hit_rate) складывать нельзя, они пересчитываются
# из сумм.
_SUMMED_COUNTERS = ("closed", learning_loop.SUCCESS,
                    "money_confirmed", "money_contradicted",
                    "recent_closed", "recent_improved")


def lane_records(track_record: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """Послужной список ВИДОВ действий → послужной список ПОЛОС.

    learning_loop.track_record ключуется видом действия, а свободу зарабатывает
    полоса: у сдвига бюджета и у целевой цены общая физика ошибки, общий срок
    замера и общий карман, и разделять их послужные списки значило бы держать
    обоих в тени вдвое дольше при том же объёме доказательств.

    Виды без полосы (их в карте нет) пропускаются молча — в отличие от
    lane_of, который на таком падает. Здесь читается ИСТОРИЯ, и в ней лежат
    виды снятых рычагов: уронить прогон из-за строки полугодовой давности —
    цена, несопоставимая с пользой от строгости.
    """
    out: Dict[str, Dict[str, float]] = {}
    for kind, slot in (track_record or {}).items():
        lane = LANE_OF_KIND.get(str(kind))
        if lane is None:
            continue
        target = out.setdefault(lane, {name: 0.0 for name in _SUMMED_COUNTERS})
        for name in _SUMMED_COUNTERS:
            try:
                target[name] += float((slot or {}).get(name) or 0.0)
            except (TypeError, ValueError):
                continue
    return out


# Откуда взялась ступень полосы. Едет в отчёт рядом с числом: «полоса на
# ступени 1» не отличает заработанную пробу от полосы, которую человек туда
# посадил руками, а решения по этим двум случаям разные.
STEP_EARNED = "track_record"     # выдала лестница по послужному списку
STEP_FLOOR = "default"           # послужного списка ещё нет — пол полосы
STEP_HUMAN = "config"            # назначено ключом lane_steps панели
STEP_SHADOW = "shadow"           # человек держит полосу в тени


def steps_by_lane(track_record: Optional[Dict[str, Any]] = None,
                  config: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """Ступень каждой полосы и её происхождение: {полоса: {"step", "source"}}.

    Порядок разрешения — от самого сильного источника к самому слабому:

      1. lane_steps панели. Слово человека перебивает лестницу В ОБЕ СТОРОНЫ:
         им и выпускают из тени, и им же сажают обратно, не дожидаясь, пока
         накопленная история переварит свежий провал.
      2. Тень — shadow_lanes панели и полосы ручного выпуска
         (autonomy.MANUAL_RELEASE_LANES). Пол ступени сюда НЕ применяется:
         иначе тень означала бы «работай на 1 %», то есть ничего.
      3. Лестница по послужному списку полосы (autonomy.step_of).
      4. Пол полосы (default_step_of) — пока послужного списка не хватает на
         первую ступень. Без пола лестница заперла бы агента навсегда:
         ступень 1 требует 12 закрытых наблюдений, а закрытых наблюдений не
         появится, пока полоса не применяет. Пол — не подарок, а вход в
         лестницу: рычаги С ИСТОРИЕЙ стартуют со своей ступени, рычаги без
         истории — с минимального живого следа, а те, чья ошибка не
         отматывается, стоят в тени по пункту 2.
    """
    config = config or {}
    overrides = config.get("lane_steps") or {}
    shadow_lanes = config.get("shadow_lanes") or ()
    records = lane_records(track_record)

    out: Dict[str, Dict[str, Any]] = {}
    for lane in ALL_LANES:
        if lane in overrides:
            out[lane] = {"step": int(overrides[lane]), "source": STEP_HUMAN}
            continue
        if (autonomy.is_shadow(lane, shadow_lanes)
                or lane in autonomy.MANUAL_RELEASE_LANES):
            out[lane] = {"step": 0, "source": STEP_SHADOW}
            continue
        record = records.get(lane)
        earned = autonomy.step_of(lane, record)
        floor = default_step_of(lane)
        # «Заработано» — только когда лестнице БЫЛО ЧЕМ высказаться и её ответ
        # не пришлось подпирать полом. Иначе в отчёте ступень 1 у полосы без
        # единого закрытого наблюдения выглядела бы как заслуженная проба.
        if record and earned >= floor:
            out[lane] = {"step": earned, "source": STEP_EARNED}
        else:
            out[lane] = {"step": floor, "source": STEP_FLOOR}
    return out

# Делитель ценности, ниже которого «на рубль риска» теряет смысл: действие
# дешевле рубля не бывает, а ноль в знаменателе сделал бы любой бесплатный
# пустяк лучшим решением прогона.
MIN_PRICE_RUB = 1.0


def select(
    actions: List[Dict[str, Any]],
    step_by_lane: Optional[Dict[str, int]] = None,
    weekly_spend_rub: float = 0.0,
    daily_cost_by_campaign: Optional[Dict[str, float]] = None,
    config: Optional[Dict[str, Any]] = None,
    budgets: Optional[Dict[str, Dict[str, float]]] = None,
    charged_by_object: Optional[Dict[str, float]] = None,
    risk_budget_rub: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Что из плана едет в кабинет этим тактом, а что отказано с причиной.

    Заменяет лимит действий на прогон (guardrails.cap_actions). Тот был срезом
    ПЕРВЫХ пятидесяти, а план собирается в порядке рычагов, а не ценности:
    корректировок генерится сотнями, и до бюджета очередь не доходила никогда —
    лимит работал как выбор рычага, чего никто не задумывал. Замер за 30 дней к
    26.08.2026: bidmodifier.add 74 строки, bidmodifier.set 24, schedule.set 14,
    бюджет/целевая цена/минус-фразы/площадки — ноль строк в любом статусе.

    Отбор идёт ПО ПОЛОСАМ, и полосы не конкурируют ни за слоты, ни за деньги:
    у каждой своя ступень, свой лимит и свой срок замера. Внутри полосы порядок
    — по ценности на рубль риска.

    Пять ограничителей, в этом порядке:

      1. КЛАСС ДОСТОВЕРНОСТИ. Предложение (класс 3) не применяется никогда и ни
         при какой ступени — у него нет рычага; отказ несёт свою причину, а не
         «не влезло».
      2. ОДНО ДЕЙСТВИЕ НА ОБЪЕКТ, пока рычаг учится (ступени 1–2): иначе
         неизвестно, что именно сработало. На верхней ступени снимается —
         измеримость держат заповедник и замер такта (задача 25).
      3. ЧИСЛО ОБЪЕКТОВ — только у выключений: второе выключение за прогон
         принято по экономике, которой после первого уже нет.
      4. РИСК-БЮДЖЕТ ПОЛОСЫ — доля недельного расхода кабинета по ступени.
         Платят его четыре полосы и два класса из четырёх: класс 0 не платит
         (он снимает деньги с огня, а не ставит под удар), класс 3 не
         применяется. Сумма списаний по одному объекту ограничена ценой самого
         объекта (risk.fit_into_budget + risk.object_cap).
      5. ДОЛЯ ВЫРЕЗАЕМОГО РАСХОДА — только у гигиены, вместо риск-бюджета:
         сломанный источник данных не должен вырезать кабинет за один проход.

    Цена набора считается ОДИН раз на весь такт (risk.net_risk), а не по
    действию: перенос денег внутри кабинета иначе платит обеими сторонами
    сразу. Но скидка за встречное движение денег законна ровно тогда, когда
    компенсация действительно произойдёт. Взять получателя, а донора отложить —
    это доливка по цене переноса, и никакой лимит её больше не увидит. Поэтому
    цена пересчитывается на том наборе, который реально уезжает, и отбор
    повторяется, пока набор и его цена не сойдутся: на выходе списанные цены
    равны net_risk ровно того набора, который вернулся во взятых. Пару
    определяет ЗНАК движения денег (risk._moved_rub), а не имена кампаний:
    донор и получатель могут быть в разных кабинетах и не знать друг о друге.

    weekly_spend_rub — недельный расход кабинетов прогона. Ноль означает
    «расход неизвестен», и лимит полосы падает на прежний абсолютный дефолт
    (risk.weekly_limit), а не на ноль: нулём прогон отложил бы всё до единого
    действия и выглядел бы в отчёте как исправно остановленный.

    risk_budget_rub — недельный потолок риска прогона, если человек поставил
    его руками (risk_budget_week в LOCKED_KEYS панели). Полоса берёт из него
    долю СВОЕЙ ступени относительно базовой (risk.DEFAULT_RISK_SHARE_WEEK =
    1 %), поэтому на ступени 1 ей доступен весь недельный потолок, на ступени 2
    — втрое больше, на нулевой — ничего. Без ручного значения формула совпадает
    с долей расхода один в один: базовый потолок и есть 1 % расхода. Смысл
    ручного потолка в том, чтобы не зависеть ни от какой арифметики, и полоса,
    считающая долю мимо него, этот смысл отменяла бы.

    budgets — остатки полос ОДНОГО кабинета: {полоса: {"risk_rub", "cut_rub"}}.
    Словарь дополняется по ходу; вызывающий держит по такому на каждый кабинет
    и передаёт вместе с weekly_spend_rub ЭТОГО кабинета (agent_e1.run_account).
    Один словарь на все кабинеты давал бы лимит первому по порядку, а не
    важнейшему: 27.08.2026 крупнейший кабинет прогона получил 2 действия из
    177 заявленных, потому что карман был вычерпан до него. Передать общий
    словарь, но недельный расход прогона — вернуть тот же дефект.

    charged_by_object — сквозной счёт риска по объектам (тот же словарь, что у
    risk.fit_into_budget в прогоне): потолок объекта обязан помнить, сколько по
    нему уже списано прошлыми полосами и прошлыми кабинетами.

    Отложенных нет: не прошедшее лимит становится отказом с причиной
    (rejects.LANE_LIMIT) и пересчитывается следующим тактом на свежих данных.
    Действие, посчитанное на данных дня X и применённое через три дня, — это
    применение вчерашнего расчёта к сегодняшнему кабинету.
    """
    from sync.agent import rejects
    from sync.agent.writer import risk as risk_mod

    steps = dict(step_by_lane or {})
    costs = dict(daily_cost_by_campaign or {})
    ledger = budgets if budgets is not None else {}
    charged = charged_by_object if charged_by_object is not None else {}

    lane_by_key: Dict[str, str] = {}
    by_lane: Dict[str, List[Dict[str, Any]]] = {}
    for action in actions:
        lane = lane_of(action)
        lane_by_key[str(action["idempotency_key"])] = lane
        by_lane.setdefault(lane, []).append(action)

    policies = {lane: policy_of(lane, steps.get(lane, default_step_of(lane)), config)
                for lane in by_lane}
    caps = {risk_mod.risk_object(a): risk_mod.object_cap(a, costs)
            for a in actions}

    reasons: Dict[str, str] = {}
    alive = {str(a["idempotency_key"]) for a in actions}
    taken: List[Dict[str, Any]] = []
    # Круг «цена → отбор → цена» сходится: каждый круг либо ничего не снимает и
    # заканчивается, либо снимает хотя бы одно действие. Граница по длине —
    # страховка на случай, если сходимость сломает будущая правка: бесконечный
    # цикл в ночном прогоне выглядит как зависший агент, а не как дефект.
    for _ in range(len(actions) + 1):
        prices = risk_mod.net_risk(
            [a for a in actions if str(a["idempotency_key"]) in alive], costs)
        # Копия счёта объектов: круги отбора черновые, и списывать в общий счёт
        # прогона можно только то, что уехало по-настоящему.
        round_charged = dict(charged)
        taken = []
        for lane in sorted(by_lane):
            lane_alive = [a for a in by_lane[lane]
                          if str(a["idempotency_key"]) in alive]
            taken += _select_lane(lane_alive, lane, policies[lane], prices,
                                  weekly_spend_rub, risk_budget_rub,
                                  ledger.get(lane) or {},
                                  round_charged, caps, reasons, rejects,
                                  risk_mod)
        chosen = {str(a["idempotency_key"]) for a in taken}
        if chosen == alive:
            charged.update(round_charged)
            break
        alive = chosen

    result = [{**a, "lane": lane_by_key[str(a["idempotency_key"])],
               "tier": _tier_of(a)}
              for a in taken]
    refused = [{**a, "blocked_reason": reasons.get(str(a["idempotency_key"]),
                                                   rejects.LANE_LIMIT)}
               for a in actions if str(a["idempotency_key"]) not in alive]
    _commit(ledger, result, policies)
    return result, refused


def _select_lane(actions, lane, policy, prices, weekly_spend, risk_budget,
                 spent, charged, caps, reasons, rejects, risk_mod):
    """Что полоса берёт из своих кандидатов на этом круге отбора."""
    ranked = sorted(actions, key=lambda a: _rank(a, lane, prices))

    applicable = []
    for action in ranked:
        if lane == LANE_PROPOSAL or _tier_of(action) not in tier_mod.APPLIED_TIERS:
            reasons[str(action["idempotency_key"])] = rejects.PROPOSAL
            continue
        # Тень — ПОСЛЕ предложения и до всех потолков. После, потому что у
        # класса 3 рычага нет вовсе и ступень его судьбы не меняет; до, потому
        # что «полосу не выпустил человек» не лечится ни рублём лимита, ни
        # порядком ценности, а действие в тени обязано уехать в журнал
        # намерением, а не раствориться в общем lane_limit.
        if policy.step == 0:
            reasons[str(action["idempotency_key"])] = rejects.SHADOW
            continue
        applicable.append(action)

    seen: Dict[str, int] = {}
    within_caps = []
    for action in applicable:
        obj = risk_mod.risk_object(action)
        per_object = policy.max_actions_per_object
        per_run = policy.max_objects_per_run
        if per_object is not None and seen.get(obj, 0) >= per_object:
            continue
        if per_run is not None and obj not in seen and len(seen) >= per_run:
            continue
        seen[obj] = seen.get(obj, 0) + 1
        within_caps.append(action)

    risk_left = (_risk_budget(lane, policy, weekly_spend, risk_budget)
                 - float(spent.get("risk_rub") or 0.0))
    fits, _ = risk_mod.fit_into_budget(
        within_caps,
        {str(a["idempotency_key"]): charged_price(a, lane, prices)
         for a in within_caps},
        risk_left, charged, caps)

    cut_left = _cut_budget(policy, weekly_spend)
    if cut_left is None:
        return fits
    cut_left -= float(spent.get("cut_rub") or 0.0)

    taken = []
    for action in fits:
        freed = _freed_rub(action)
        if freed > cut_left:
            continue
        cut_left -= freed
        taken.append(action)
    return taken


def _commit(ledger: Dict[str, Dict[str, float]], taken: List[Dict[str, Any]],
            policies: Dict[str, LanePolicy]) -> None:
    """Списать израсходованное полосами — после того, как набор сошёлся.

    До схождения списывать нельзя: промежуточный круг берёт действия, которые
    следующий круг снимет, и остаток полосы уехал бы в минус на чужих
    кандидатов.
    """
    for action in taken:
        lane = action["lane"]
        slot = ledger.setdefault(lane, {"risk_rub": 0.0, "cut_rub": 0.0})
        slot["risk_rub"] = (float(slot.get("risk_rub") or 0.0)
                            + float(action.get("risk_rub") or 0.0))
        if policies[lane].max_cut_share is not None:
            slot["cut_rub"] = (float(slot.get("cut_rub") or 0.0)
                               + _freed_rub(action))


def risk_budget_of(lane: str, step: int, weekly_spend: float,
                   config: Optional[Dict[str, Any]] = None,
                   risk_budget: Optional[float] = None) -> float:
    """Потолок риска полосы на её ступени — тот же, по которому шёл отбор.

    Публичная дверь к _risk_budget: отчёт прогона печатает этот потолок рядом
    со спросом полосы, и брать его копией формулы нельзя — правка ставок
    ступеней разошлась бы с отбором молча, а число в отчёте продолжало бы
    выглядеть правдой.
    """
    return _risk_budget(lane, policy_of(lane, step, config), weekly_spend,
                        risk_budget)


def _risk_budget(lane: str, policy: LanePolicy, weekly_spend: float,
                 risk_budget: Optional[float] = None) -> float:
    """Риск-бюджет полосы на прогон. Не платит риском — значит им не ограничена.

    Ноль ступени тени возвращается как есть: это решение человека, и оно
    исполняется буквально (тот же довод, что у risk.weekly_limit про нулевую
    долю в панели).
    """
    from sync.agent.writer import risk as risk_mod

    if lane not in RISK_PAYING_LANES:
        return float("inf")
    if risk_budget is None:
        return risk_mod.weekly_limit(weekly_spend, policy.risk_share)
    return (float(risk_budget) * float(policy.risk_share)
            / risk_mod.DEFAULT_RISK_SHARE_WEEK)


def _cut_budget(policy: LanePolicy, weekly_spend: float) -> Optional[float]:
    """Сколько рублей расхода полосе позволено вырезать за такт. None — не её ограничитель.

    Расход кабинета неизвестен (пробел в витрине, лаг синка) — доли от него не
    существует, и ограничителя нет: тот же довод, что у risk.weekly_limit. Ноль
    остановил бы гигиену целиком при первом же пробеле, и отчёт выглядел бы как
    у исправного агента, которому нечего резать.
    """
    if policy.max_cut_share is None:
        return None
    if float(weekly_spend or 0.0) <= 0.0:
        return None
    return float(weekly_spend) * float(policy.max_cut_share)


def _pays_risk(action: Dict[str, Any], lane: str) -> bool:
    return (lane in RISK_PAYING_LANES
            and _tier_of(action) in tier_mod.RISK_PAYING_TIERS)


def charged_price(action: Dict[str, Any], lane: str,
                  prices: Dict[str, float]) -> float:
    """Во сколько это действие обходится СВОЕЙ полосе.

    Публичное, потому что цену спрашивает не только отбор: отчёт прогона
    считает по ней спрос полосы («сколько она хотела»), и считать его копией
    этой формулы значило бы объяснять человеку решение, которого не было.
    Ноль здесь — не «бесплатно по недосмотру», а решение: полоса не платит
    риском вовсе либо класс достоверности действия его не платит.
    """
    if not _pays_risk(action, lane):
        return 0.0
    return float(prices.get(str(action["idempotency_key"]), 0.0))


def _tier_of(action: Dict[str, Any]) -> int:
    return tier_mod.tier_of(action)


def _freed_rub(action: Dict[str, Any]) -> float:
    """Сколько рублей действие снимает с кабинета за свой горизонт замера."""
    from sync.agent.writer import expectation

    exp = expectation.of(action) or {}
    return max(0.0, -float(exp.get("rub_delta") or 0.0))


def _rank(action: Dict[str, Any], lane: str, prices: Dict[str, float]):
    """Ключ сортировки полосы: сначала ценнее, при равенстве — по ключу.

    Ценность меряется в единицах своей полосы и делится на цену действия —
    отсюда «лучшее на рубль риска». Двух шкал в одной куче нет: действие,
    обещающее ПРИРОСТ лидов, и действие, обещающее СНЯТЫЕ деньги, сравниваются
    каждое со своими. Курса «рубль → лид» у отбора нет, и выдумать его здесь
    значило бы решить за портфель, который этот курс как раз и считает.

    Действие, которое риском не платит, делить не на что: оно ранжируется прямо
    своей ценностью. Так гигиена и выстраивается по вырезаемому расходу — её
    единица отбора по плану беты.

    Порядок полностью определён: при равной ценности решает ключ
    идемпотентности. Иначе один и тот же план в двух прогонах даёт разные
    срезы, и разбор беты не с чем сверять.
    """
    from sync.agent.writer import expectation

    exp = expectation.of(action) or {}
    leads = float(exp.get("leads_delta") or 0.0)
    freed = max(0.0, -float(exp.get("rub_delta") or 0.0))
    price = charged_price(action, lane, prices)
    per_rub = 1.0 / max(price, MIN_PRICE_RUB) if price > 0.0 else 1.0
    if leads > 0.0:
        family, first, second = 1, leads * per_rub, freed * per_rub
    else:
        family, first, second = 0, freed * per_rub, 0.0
    return (-family, -first, -second, str(action["idempotency_key"]))
