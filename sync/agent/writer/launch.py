# -*- coding: utf-8 -*-
"""
sync/agent/writer/launch.py — полоса запуска: билдер как рычаг агента (Ф14).

Все прочие рычаги правят существующий объект: ставку, бюджет, цель,
расписание. Запуск создаёт объект, которого не было, и потому устроен иначе.

**Отправить кампанию агент не может.** Тело кампании — группы, ключи,
объявления — собирает другой репозиторий («EDU кампании»), и `campaigns.add`
из edu-sync невыразим в принципе: у агента нет ни групп, ни текстов. Поэтому
действие `campaign.create` несёт НАРЯД (sync/agent/build_order.py), а не
запрос к API, и allow-лист записи его не пропускает — с отдельной причиной,
чтобы отказ читался как устройство, а не как недоделка
(guardrails.BUILDER_REASON).

**Созданная кампания всегда на паузе.** Создание и запуск — два разных
решения, и между ними стоит человек. Это держится с двух сторон: наряд везёт
`state: SUSPENDED`, а билдер сразу после `campaigns.add` шлёт
`campaigns.suspend` (direct/upload.py, событие journal `campaign_suspended`).
Одна сторона тут ненадёжна: наряд можно залить руками, а билдера — позвать
без наряда.

**Кросс-минусовка едет одним тактом с созданием — и только с ним.** Новая
кампания и её доноры торгуются на одном аукционе одного рекламодателя, и
разрыв между «создали» и «заминусовали» стоит денег обеим. Но обратный
разрыв дороже: донор, заминусованный под кампанию, которой нет, теряет
рабочий трафик молча — в кабинете всё выглядит исправным. Поэтому минусовка
связана с созданием явным полем (LAUNCH_LINK_KEY), и `drop_unlaunched`
снимает её, если создание не поехало.

**Откат запуска — пауза и возврат доноров.** Удаление объектов запрещено
инвариантом, поэтому отменить создание нельзя; отменяется то, что отменимо:
показы новой кампании и минусовка у доноров. Возврат считается вычитанием из
СВЕЖЕГО списка (negatives.remove_added), а не восстановлением снимка: между
тактами список правят руками, и снимок снёс бы фразы человека.

Модуль чистый: ни базы, ни сети. Свежее состояние доноров подаётся аргументом
(negatives.fetch_negatives), как и у остальных рычагов.
"""

import hashlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sync.agent import build_order, segments
from sync.agent.writer import exposure, expectation, negatives, switch

LAUNCH_KIND = "campaign.create"

# Состояние, в котором кампания обязана появиться в кабинете.
STATE_SUSPENDED = "SUSPENDED"

# Поле, которым кросс-минусовка связана со своим запуском. Не «пометка для
# отчёта»: по нему drop_unlaunched отличает минусовку связки от обычной
# гигиены, а отчёт прогона — свои отказы от чужих.
LAUNCH_LINK_KEY = "launch_order_id"

DAYS_IN_WEEK = 7.0

ORPHAN_REASON = (
    "кросс-минусовка донора без своего запуска: кампании, ради которой "
    "вынесены фразы, не будет, а донор перестал бы по ним торговаться"
)


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _number(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


# ------------------------------------------------------------- создание


def _idempotency_key(order_id: str) -> str:
    """Ключ действия — только order_id, без даты и без состава фраз.

    Состав фраз обновляется каждым прогоном генератора (реестр обновляет
    нагрузку идеи), и войди он в ключ — журнал считал бы каждый прогон новым
    решением, а заливка заводила бы вторую кампанию к той же идее. Кампания в
    кабинете одна, и решение о ней одно.
    """
    return hashlib.sha256(f"launch:{order_id}".encode("utf-8")).hexdigest()[:32]


def build(order: Dict[str, Any]) -> Dict[str, Any]:
    """Наряд → действие запуска. Негодный наряд — ValueError с причиной.

    Проверка здесь, а не «потом на применении»: наряд с дырой, доехавший до
    реестра, ждал бы применения, которого ему нельзя дать, и висел бы в
    очереди предложений как исправный.
    """
    checked = build_order.validate(order)
    budget = float(checked["campaign"]["weekly_budget"])
    horizon = float(checked["horizon_days"])
    return {
        "action_kind": LAUNCH_KIND,
        # Объекта ещё нет, и адресовать действие нечем, кроме наряда. Имя
        # кампании сюда не годится: по нему считается риск-объект, а имя
        # длиной до 200 символов склеивало бы отчёты.
        "object_level": "campaign",
        "object_id": checked["order_id"],
        "exposure": exposure.launch_exposure(
            budget, horizon, what=f"кампания {checked['campaign_name']}"),
        "direct_type": "CAMPAIGN_CREATE",
        "key": "launch",
        "payload": {
            # Пауза объявлена и здесь, и у получателя (direct/upload.py шлёт
            # campaigns.suspend сразу после создания). Дублирование намеренное:
            # наряд заливают и руками.
            "state": STATE_SUSPENDED,
            "CampaignName": checked["campaign_name"],
            "order": checked,
        },
        # Прежнего состояния нет: объекта не существовало. Пустой словарь, а
        # не отсутствие ключа, — чтобы «не читали» и «нечего читать» не
        # выглядели одинаково для сторожа отката.
        "previous_state": {},
        "idempotency_key": _idempotency_key(checked["order_id"]),
    }


# ------------------------------------------------------------ связка такта


def donor_ids(create: Dict[str, Any]) -> List[str]:
    """Кампании-доноры наряда: у кого прогон обязан прочитать минус-фразы.

    Отдельной функцией, потому что список нужен ДО сборки связки: свежее
    чтение кабинета заказывается одним запросом на все кампании такта, и
    доноры запуска обязаны попасть в тот же запрос, а не во второй.
    """
    order = (create.get("payload") or {}).get("order") or {}
    donors = {_text(item.get("campaign_id"))
              for item in order.get("donor_negatives") or ()}
    donors.discard("")
    return sorted(donors)


def unread_donors(create: Dict[str, Any],
                  actual_by_campaign: Dict[str, Dict[str, Any]]) -> List[str]:
    """Доноры наряда, чей список минус-фраз этим тактом прочитать не удалось.

    Отдельно от build_all, потому что нужна вызывающему для журнала: связка
    без чтения донора не едет целиком, и прогон обязан назвать причину, а не
    отчитаться пустым набором.
    """
    return [donor for donor in donor_ids(create)
            if actual_by_campaign.get(donor) is None]


def build_all(create: Dict[str, Any],
              actual_by_campaign: Dict[str, Dict[str, Any]],
              ) -> List[Dict[str, Any]]:
    """Действие запуска + кросс-минусовка доноров → связка одного такта.

    Связка едет ЦЕЛИКОМ или не едет вовсе: пустой список, если хоть один
    донор не прочитан (unread_donors). Это не «минусовка неполная», а запуск
    без защиты от самоконкуренции: новая кампания перебивала бы ставку
    донору, а донор ей. Такой наряд ждёт следующего такта со свежим чтением.

    Донор, у которого нужные фразы УЖЕ стоят, действия не порождает и связку
    не ломает: цель связки — состояние кабинета, а не число запросов к API.

    actual_by_campaign — свежее чтение (negatives.fetch_negatives), не
    витрина: список правят руками между тактами, и объединение обязано
    строиться поверх того, что стоит в кабинете СЕЙЧАС.
    """
    order = (create.get("payload") or {}).get("order") or {}
    order_id = _text(order.get("order_id"))
    window_days = int(_number(order.get("window_days")) or 0) or 1
    target_cpa = _number((order.get("campaign") or {}).get("target_cpa"))

    desired: Dict[str, List[str]] = {}
    for item in order.get("donor_negatives") or ():
        campaign_id = _text(item.get("campaign_id"))
        if campaign_id:
            desired[campaign_id] = list(item.get("phrases") or ())

    cut_cost: Dict[str, float] = {}
    cut_conversions: Dict[str, float] = {}
    for query in order.get("queries") or ():
        donor = _text(query.get("donor_campaign_id"))
        if not donor:
            continue
        cut_cost[donor] = cut_cost.get(donor, 0.0) + (_number(query.get("cost_rub")) or 0.0)
        cut_conversions[donor] = (cut_conversions.get(donor, 0.0)
                                  + (_number(query.get("conversions")) or 0.0))

    if unread_donors(create, actual_by_campaign):
        return []

    actions, refused = negatives.diff_negatives(
        desired, actual_by_campaign, cut_cost=cut_cost,
        window_days=window_days, cut_conversions=cut_conversions,
        baseline_cpa=target_cpa)
    if refused:
        # Донор оказался не текстовой кампанией: минус-фразы ему не поставить
        # вовсе. Не отказ рычага, а негодный донор — и запуск с ним рядом
        # торговался бы против него всё время своей жизни.
        return []

    return [create] + [{**action, LAUNCH_LINK_KEY: order_id}
                       for action in actions]


def drop_unlaunched(actions: Sequence[Dict[str, Any]],
                    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Из набора убирает кросс-минусовку, чей запуск в нём не едет.

    Сторож нужен ПОСЛЕ отбора полос, а не вместо него: минусовка живёт в
    полосе гигиены, запуск — в полосе запуска, и решения о них принимаются
    независимо, каждое по своей ступени и своему лимиту. Полоса запуска стоит
    в тени (lanes.default_step_of), значит в норме связка не едет никогда, а
    минусовка её доноров прошла бы гигиеной — и донор молча перестал бы
    торговаться по фразам, которые больше некому обслуживать.

    Действия без признака связки не рассматриваются вовсе: обычная гигиена
    сама по себе полна, и сторож связки, начав отбирать чужое, стал бы вторым
    отбором со своим мнением о деньгах.
    """
    launched = {_text((a.get("payload") or {}).get("order", {}).get("order_id"))
                for a in actions if a.get("action_kind") == LAUNCH_KIND}
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for action in actions:
        link = _text(action.get(LAUNCH_LINK_KEY))
        if link and link not in launched:
            # Действие целиком, а не сводка о нём: снятое здесь едет в тот же
            # список отказов прогона, что и всё остальное, и отчёт обязан
            # уметь напечатать его теми же полями.
            dropped.append({**action, "blocked_reason": ORPHAN_REASON})
            continue
        kept.append(action)
    return kept, dropped


# ----------------------------------------------------------------- откат


def rollback(applied: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Применённая связка → действия отката.

    Две половины, и вторая обязательна. Без неё откат оставляет доноров
    заминусованными по фразам, которые больше некому обслуживать: кабинет
    теряет трафик, а журнал считает изменение отменённым.

    Пауза ставится только из состояния ON: у выключенной кампании выключать
    нечего, и повторное выключение рельса всё равно отклонит
    (guardrails._check_suspend). Кампании нет вовсе (наряд уехал, завести не
    вышло) — доноры всё равно возвращаются: их минусовка уже применена.

    Что снимать у донора, считается по СВЕЖЕМУ чтению минус фразы этого
    наряда, а не восстановлением previous_state: между применением и откатом
    список правят руками.
    """
    actions: List[Dict[str, Any]] = []

    campaign_id = _text(applied.get("campaign_id"))
    state = _text(applied.get("state"))
    if campaign_id and state == "ON":
        actions.append({
            "action_kind": switch.SWITCH_KIND,
            "object_level": "campaign",
            "object_id": campaign_id,
            "exposure": exposure.whole_object_exposure(
                "откат запуска: под ударом весь расход новой кампании"),
            "direct_type": "CAMPAIGN_STATE",
            "key": "suspend",
            "payload": {"CampaignId": int(campaign_id)},
            "previous_state": {"State": "ON"},
            "idempotency_key": _rollback_key("suspend", campaign_id),
        })

    actual = applied.get("actual_by_campaign") or {}
    for action in applied.get("negatives") or ():
        payload = action.get("payload") or {}
        donor = _text(payload.get("CampaignId"))
        added = list(payload.get("AddedPhrases") or ())
        current = ((actual.get(donor) or {}).get("negative_keywords")
                   or (action.get("payload", {}).get("NegativeKeywords") or {}).get("Items")
                   or ())
        removal = negatives.remove_added_action(
            donor, list(current), added, restores=_restores(action))
        if removal is not None:
            actions.append(removal)
    return actions


def _restores(cut: Dict[str, Any]) -> Dict[str, float]:
    """Числа отсечения, которое отменяет снятие: рубли в день и лиды в день.

    Оба берутся у САМОГО применённого действия, а не пересчитываются: обещание
    отката обязано быть зеркалом того обещания, с которым отсечение уезжало в
    кабинет, иначе замер сравнил бы факт с числом, которого никто не давал.
    Лиды лежат в payload со знаком минус (отсечение теряло) — здесь знак
    переворачивается, и делятся они на срок замера, потому что заявлены за
    весь срок, а зеркало собирается из дневных величин.
    """
    own = cut.get("exposure") or {}
    daily = _number(own.get("cut_daily_rub"))
    if daily is None:
        daily = _number(own.get("daily_rub"))
    payload = cut.get("payload") or {}
    lost = _number(payload.get(expectation.LEADS_KEY)) or 0.0
    days = _number(payload.get(expectation.DAYS_KEY)) or 0.0
    return {"restored_daily_rub": daily or 0.0,
            "restored_conversions_per_day": (-lost / days) if days > 0 else 0.0}


def _rollback_key(what: str, campaign_id: str) -> str:
    raw = f"launch-rollback:{what}:{campaign_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ------------------------------------------- настройки кампании от доноров


def donor_goal_ids(settings: Optional[Dict[str, Any]]) -> List[int]:
    """Цели оптимизации донора: сперва витрина, затем сырой формат кабинета.

    Публична, потому что этой же парой источников пользуется генератор выноса
    (consolidate): доноры с разными целями не сводятся в одну кампанию, и
    разводить их нужно ДО экономики группы — тем же чтением настроек, каким
    наряд потом соберёт кампанию, иначе развод и сборка разойдутся.
    """
    settings = settings if isinstance(settings, dict) else {}
    return (_vitrina_goal_ids(settings)
            or segments.goal_ids_from_campaign(settings))


def donor_counter_ids(settings: Optional[Dict[str, Any]]) -> List[int]:
    """Счётчики Метрики донора: сперва витрина, затем сырой формат кабинета."""
    settings = settings if isinstance(settings, dict) else {}
    return _vitrina_counter_ids(settings) or _counter_ids(settings)


def campaign_from_donors(donors: Sequence[Dict[str, Any]], *,
                         donor_cpa: float, window_days: float,
                         ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Доноры → настройки новой кампании. Не сошлись — (None, причина).

    Счётчик и цель не выдумываются и не берутся из панели: новая кампания
    обязана мерить успех ровно тем же, чем меряют доноры. Другая цель — и
    вердикт «побили донорскую цену конверсии» сравнивал бы разные события,
    то есть не был бы вердиктом вовсе.

    Доноры разошлись в цели — запуск отменяется, а не усредняется: это два
    разных смысла конверсии в одном кабинете, и выбирать между ними по
    большинству нельзя. Тот же ответ, что у неизвестной доли сегмента в
    exposure: неизвестное — не среднее. Штатно расхождение разводит сам
    генератор ещё до экономики группы (consolidate._split_measurement, тем же
    чтением настроек — donor_goal_ids/donor_counter_ids); здесь — последний
    рубеж на случай, если доноры пришли другим путём.

    Недельный лимит — то, что доноры по этим фразам УЖЕ тратят. Трафик
    переезжает, а не появляется, и лимит выше их расхода означал бы, что
    вместе с выносом мы молча увеличили ставку на направление.

    Гео наряд не задаёт — решение Павла 01.09.2026: билдер ставит свой
    дефолт (МСК), а другое гео человек выставляет в кабинете сам.

    Настройки доноров приходят витриной edu_campaign_settings (см.
    edu_direct_settings.py: strategy/targeting/meta). Сырой формат
    campaigns.get (TextCampaign.BiddingStrategy) остаётся читаемым запасным
    путём: наряд может собираться и из свежего чтения кабинета.
    """
    if not donors:
        return None, "запуск без доноров: настройки кампании взять не из чего"

    window = _number(window_days)
    if not window or window <= 0:
        return None, ("окно наблюдения доноров неизвестно: недельный лимит "
                      "новой кампании не из чего считать")

    goals: set = set()
    counters: set = set()
    cost = 0.0
    for donor in donors:
        settings = donor.get("settings") or {}
        own_goals = donor_goal_ids(settings)
        own_counters = donor_counter_ids(settings)
        # Проверка по КАЖДОМУ донору, а не по объединению: донор без цели —
        # это донор, чья настройка не прочитана, и согласие остальных о нём
        # ничего не говорит. Объединение спрятало бы расхождение ровно там,
        # где оно опаснее всего: у молчащего.
        if not own_goals:
            return None, (f"у донора {donor.get('donor_campaign_id')} не прочитана "
                          "цель оптимизации: стратегия AVERAGE_CPA без цели "
                          "невозможна, и кампания встала бы на ручных ставках "
                          "уже после заливки")
        if not own_counters:
            return None, (f"у донора {donor.get('donor_campaign_id')} не прочитан "
                          "счётчик Метрики: без него кампания не отдаёт конверсий")
        goals.update(int(goal) for goal in own_goals)
        counters.update(int(counter) for counter in own_counters)
        cost += _number(donor.get("cost_rub")) or 0.0

    if len(goals) > 1:
        return None, (f"доноры оптимизируются на разные цели ({sorted(goals)}): "
                      "у новой кампании не может быть двух смыслов конверсии")
    if len(counters) > 1:
        return None, (f"доноры считают разными счётчиками ({sorted(counters)}): "
                      "сводить их в одну кампанию нечем")
    cpa = _number(donor_cpa)
    if not cpa or cpa <= 0:
        return None, "цена конверсии доноров неизвестна: целевую назначить не от чего"

    return {
        "weekly_budget": int(round(cost / window * DAYS_IN_WEEK)),
        "target_cpa": int(round(cpa)),
        "counter_id": counters.pop(),
        "goal_id": goals.pop(),
    }, None


def _vitrina_goal_ids(settings: Dict[str, Any]) -> List[int]:
    """Цели оптимизации из витрины edu_campaign_settings.

    Витрина (edu_direct_settings.py) хранит их в strategy.{search,network,
    package}.goalIds и strategy.priorityGoals — тот же состав источников, что
    у сырого формата в segments.goal_ids_from_campaign, только после
    нормализации. Пустой ответ — «это не витрина», и вызывающий пробует
    сырой формат.
    """
    strategy = settings.get("strategy")
    if not isinstance(strategy, dict):
        return []
    out: List[int] = []
    for key in ("search", "network", "package"):
        block = strategy.get(key)
        if not isinstance(block, dict):
            continue
        for goal in block.get("goalIds") or ():
            try:
                out.append(int(goal))
            except (TypeError, ValueError):
                continue
    for goal in strategy.get("priorityGoals") or ():
        try:
            out.append(int(goal))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def _vitrina_counter_ids(settings: Dict[str, Any]) -> List[int]:
    """Счётчики Метрики из витрины (meta.counterIds).

    Поле появилось вместе с нарядами: до них витрина счётчик не хранила, и
    campaign_from_donors в проде отказывал КАЖДОМУ выносу с «не прочитан
    счётчик» — формат теста (сырой campaigns.get) разошёлся с форматом боя.
    """
    meta = settings.get("meta")
    if not isinstance(meta, dict):
        return []
    out: List[int] = []
    for value in meta.get("counterIds") or ():
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def _counter_ids(settings: Dict[str, Any]) -> List[int]:
    """Счётчики Метрики кампании из её настроек.

    Ходим по тем же типам кампаний, что и segments.goal_ids_from_campaign:
    цель и счётчик лежат в одной ветке ответа, и знать про разные наборы
    типов две функции не должны.
    """
    out: List[int] = []
    for type_key in segments._CAMPAIGN_TYPES:
        block = (settings.get(type_key) or {}).get("CounterIds") or {}
        for value in block.get("Items") or ():
            try:
                out.append(int(value))
            except (TypeError, ValueError):
                continue
    return sorted(set(out))
