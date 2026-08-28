# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_lanes.py — полосы действий вместо общего лимита прогона.

Данные — литералы в форме действия движка записи (action_kind + payload):
модуль ничего не читает и никуда не пишет, он отвечает на два вопроса —
в какой полосе действие и что этой полосе положено на её ступени.
"""

import pytest

from sync.agent import autonomy, config, rejects
from sync.agent.writer import (expectation, guardrails, lanes, risk, switch,
                               tier)


# ------------------- пять тестов спеки (§Ф11, задача 4)


def test_every_allowed_kind_has_exactly_one_lane():
    for kind in guardrails.ALLOWED_ACTION_KINDS:
        lane = lanes.lane_of({"action_kind": kind, "payload": {}})
        assert lane in lanes.ALL_LANES, kind


def test_exploration_flag_wins_over_kind():
    action = {"action_kind": "budget.set", "payload": {"exploration": {"rub": 5000}}}
    assert lanes.lane_of(action) == lanes.LANE_EXPLORATION


def test_hygiene_pays_no_risk_but_is_capped_by_cut_share():
    p = lanes.policy_of(lanes.LANE_HYGIENE, step=1)
    assert p.risk_share == 0.0
    assert p.max_cut_share is not None and 0.0 < p.max_cut_share <= 0.10


def test_shadow_step_gives_no_budget_to_any_lane():
    for lane in lanes.ALL_LANES:
        assert lanes.policy_of(lane, step=0).risk_share == 0.0


def test_panel_keys_survive_the_move_to_lanes():
    assert "max_suspends_per_run" in config.SPEC          # стал политикой полосы 4
    assert "max_actions_per_run" not in config.SPEC       # такого ключа и не было
    assert config.resolve(overrides={"max_suspends_per_run": 1})["max_suspends_per_run"] == 1


# ------------------- карта видов действий


def test_seven_lanes_and_no_duplicates():
    assert len(lanes.ALL_LANES) == 7
    assert len(set(lanes.ALL_LANES)) == 7


def test_kinds_land_in_the_lane_the_plan_names():
    def lane(kind):
        return lanes.lane_of({"action_kind": kind, "payload": {}})

    assert lane("negative.add") == lanes.LANE_HYGIENE
    assert lane("placement.exclude") == lanes.LANE_HYGIENE
    assert lane("bidmodifier.add") == lanes.LANE_TUNING
    assert lane("bidmodifier.set") == lanes.LANE_TUNING
    assert lane("schedule.set") == lanes.LANE_TUNING
    assert lane("budget.set") == lanes.LANE_ALLOCATION
    assert lane("budget.set_daily") == lanes.LANE_ALLOCATION
    assert lane("tcpa.set") == lanes.LANE_ALLOCATION
    assert lane("campaign.suspend") == lanes.LANE_SUSPEND


def test_future_kinds_already_know_their_lane():
    # Ф14–Ф15 приносят рычаги; карта полос знает их заранее, чтобы новый вид
    # не появился без лимита и без цены.
    def lane(kind):
        return lanes.lane_of({"action_kind": kind, "payload": {}})

    assert lane("campaign.create") == lanes.LANE_LAUNCH
    assert lane("campaign.resume") == lanes.LANE_LAUNCH
    assert lane("negative.remove_added") == lanes.LANE_HYGIENE
    assert lane("goal.set") == lanes.LANE_ALLOCATION
    assert lane("strategy.set") == lanes.LANE_ALLOCATION
    assert lane("geo.set") == lanes.LANE_ALLOCATION
    assert lane("audience.add") == lanes.LANE_TUNING


def test_map_knows_more_kinds_than_the_allow_list_passes():
    # Карта идёт впереди allow-листа: вид входит в ALLOWED_ACTION_KINDS только
    # вместе со своим рычагом и тестом, а полосу знает заранее.
    assert "campaign.create" in lanes.LANE_OF_KIND
    assert "campaign.create" not in guardrails.ALLOWED_ACTION_KINDS


def test_unknown_kind_raises_instead_of_passing_for_free():
    # Вид без полосы не имеет ни лимита, ни цены. Молчаливое «пусть будет
    # предложением» пропустило бы его мимо всех ограничителей бесплатно.
    with pytest.raises(ValueError):
        lanes.lane_of({"action_kind": "budget.multiply_by_two", "payload": {}})


def test_recommendation_without_a_lever_goes_to_proposals():
    # Мастер кампаний и смысловые гипотезы: рычага записи нет, аппарат тот же.
    action = {"action_kind": "proposal.campaign_master", "payload": {}}
    assert lanes.lane_of(action) == lanes.LANE_PROPOSAL


# ------------------- политики полос


def test_only_four_lanes_pay_the_risk_share():
    paying = {lanes.LANE_TUNING, lanes.LANE_ALLOCATION,
              lanes.LANE_SUSPEND, lanes.LANE_LAUNCH}
    for step in (1, 2, 3):
        for lane in lanes.ALL_LANES:
            share = lanes.policy_of(lane, step=step).risk_share
            if lane in paying:
                assert share == autonomy.share_of(step), (lane, step)
            else:
                # Гигиена высвобождает деньги, разведка платит из кармана
                # explore_share, предложения не применяются.
                assert share == 0.0, (lane, step)


def test_measure_days_match_the_plan_table():
    days = {lane: lanes.policy_of(lane, step=1).measure_days for lane in lanes.ALL_LANES}
    assert days[lanes.LANE_HYGIENE] == 3
    assert days[lanes.LANE_TUNING] == 7
    assert days[lanes.LANE_ALLOCATION] == 14
    assert days[lanes.LANE_SUSPEND] == 14
    assert days[lanes.LANE_EXPLORATION] == 14
    assert days[lanes.LANE_LAUNCH] == 30


def test_shadow_step_applies_nothing_at_all():
    # Ноль риск-доли запирает только те полосы, которые риском платят.
    # Гигиена риска не платит — в тени её обязан держать счётчик объектов.
    for lane in lanes.ALL_LANES:
        assert lanes.policy_of(lane, step=0).max_objects_per_run == 0, lane
    assert lanes.policy_of(lanes.LANE_HYGIENE, step=0).max_cut_share == 0.0


def test_proposal_lane_never_applies_at_any_step():
    for step in (0, 1, 2, 3):
        assert lanes.policy_of(lanes.LANE_PROPOSAL, step=step).max_objects_per_run == 0


def test_suspend_cap_comes_from_the_panel():
    default = lanes.policy_of(lanes.LANE_SUSPEND, step=1)
    assert default.max_objects_per_run == switch.MAX_SUSPENDS_PER_RUN

    tuned = lanes.policy_of(lanes.LANE_SUSPEND, step=1,
                            config={"max_suspends_per_run": 2})
    assert tuned.max_objects_per_run == 2


def test_one_action_per_object_until_the_lever_is_proven():
    for step in (1, 2):
        assert lanes.policy_of(lanes.LANE_TUNING, step=step).max_actions_per_object == 1
        assert lanes.policy_of(lanes.LANE_ALLOCATION, step=step).max_actions_per_object == 1
    # На верхней ступени измеримость держат заповедник и замер такта, а не
    # искусственная редкость правок (задача 7, шаг 3).
    assert lanes.policy_of(lanes.LANE_TUNING, step=3).max_actions_per_object is None
    assert lanes.policy_of(lanes.LANE_ALLOCATION, step=3).max_actions_per_object is None


def test_hygiene_is_not_rationed_by_object():
    # Класс 0 вносится весь и сразу; его единственный ограничитель — рубли.
    p = lanes.policy_of(lanes.LANE_HYGIENE, step=1)
    assert p.max_actions_per_object is None
    assert p.max_objects_per_run is None


def test_unknown_lane_raises():
    with pytest.raises(ValueError):
        lanes.policy_of("budgets", step=1)


def test_unknown_step_raises():
    with pytest.raises(ValueError):
        lanes.policy_of(lanes.LANE_TUNING, step=4)


# ------------------- отбор лучшего на объекте (задача 7)


BIG_COST = {str(i): 1_000_000.0 for i in range(300)}
BIG_COST.update({f"c{i}": 1_000_000.0 for i in range(300)})
BIG_COST.update({f"b{i}": 1_000_000.0 for i in range(300)})
RICH = 10_000_000.0          # недельный расход кабинета в тестах отбора


def _act(kind, campaign, key, leads=1.0, rub=0.0, daily=10.0, days=7,
         **extra):
    """Действие в форме движка записи с ЗАЯВЛЕННЫМ ожиданием.

    Ожидание заявлено в payload, а не считается моделью: отбор ранжирует по
    тому обещанию, с которым действие уедет в кабинет.
    """
    action = {
        "action_kind": kind,
        "object_level": "campaign",
        "object_id": campaign,
        "direct_type": "SEGMENT",
        "key": key,
        "idempotency_key": f"{kind}|{campaign}|{key}",
        "exposure": {"daily_rub": daily, "basis": "тест"},
        "payload": {
            expectation.LEADS_KEY: leads,
            expectation.RUB_KEY: rub,
            expectation.BASIS_KEY: "тест",
            expectation.DAYS_KEY: days,
        },
    }
    action.update(extra)
    return action


def _tuning(campaign, segment, leads=1.0, daily=10.0):
    return _act("bidmodifier.set", campaign, segment, leads=leads, daily=daily)


def _budget(campaign, leads=5.0, rub=0.0, daily=100.0, **extra):
    action = _act("budget.set", campaign, "campaign", leads=leads, rub=rub,
                  daily=daily, days=14)
    action.update(extra)
    return action


def _cut(campaign, cut_rub=1_000.0, days=3):
    """Отсечение класса 0: основание арифметической формы, риском не платит."""
    return _act("negative.add", campaign, "campaign", leads=0.0,
                rub=-cut_rub, daily=cut_rub / days, days=days,
                evidence={"cost_rub": cut_rub * 10, "conversions": 0,
                          "baseline_cpa": 100.0, "window_days": 30})


def _select(actions, steps=None, spend=RICH, **kw):
    return lanes.select(actions, steps or {}, weekly_spend_rub=spend,
                        daily_cost_by_campaign=BIG_COST, **kw)


def test_best_action_per_object_wins_by_value_per_risk():
    """Уберите этот тест — и на объекте снова победит ПЕРВЫЙ, а не лучший.

    Порядок сборки плана — не порядок ценности: рычаг обходит сегменты в
    порядке справочника. Пока рычаг учится, на объект берётся одно действие,
    и это одно обязано быть лучшим по ценности на рубль риска.
    """
    weak = _tuning("c1", "AGE_25", leads=0.4)
    strong = _tuning("c1", "TABLET", leads=3.0)
    taken, refused = _select([weak, strong], {lanes.LANE_TUNING: 1})
    assert [a["key"] for a in taken] == ["TABLET"]
    assert [a["key"] for a in refused] == ["AGE_25"]


def test_lanes_do_not_compete_for_slots():
    """Регрессия дефекта d36d1c3 на новом механизме.

    Замер за 30 дней к 26.08.2026: в журнале только bidmodifier.add (74),
    bidmodifier.set (24), schedule.set (14); бюджетных действий, целевой цены,
    минус-фраз и площадок — НОЛЬ строк ни в одном статусе. Причина была не в
    политике: корректировок генерится сотнями, лимит прогона был один на всех,
    и до бюджета очередь не доходила никогда.
    """
    actions = [_tuning(f"c{i}", "SEG", leads=1.0) for i in range(200)]
    actions += [_budget(f"b{i}", leads=5.0) for i in range(20)]
    taken, _ = _select(actions, {lanes.LANE_TUNING: 1, lanes.LANE_ALLOCATION: 1})
    kinds = {a["action_kind"] for a in taken}
    assert "budget.set" in kinds, "бюджет снова вытеснен корректировками"
    assert "bidmodifier.set" in kinds


def test_top_step_allows_many_segments_per_campaign():
    # Изоляция «одно на объект» нужна, пока рычаг учится: неизвестно, что
    # сработало. На доказанном классе измеримость держат заповедник и замер
    # такта, а не искусственная редкость правок.
    actions = [_tuning("c1", f"SEG{i}", leads=1.0) for i in range(5)]
    taken, refused = _select(actions, {lanes.LANE_TUNING: lanes.TOP_STEP})
    assert len(taken) == 5 and refused == []


def test_arithmetic_tier_does_not_consume_lane_budget():
    """Класс 0 вносится ВЕСЬ и СРАЗУ — это главное обещание разделения полос.

    Утверждение о прошлом не ставит деньги под удар, оно снимает их с огня.
    Триста таких отсечений не занимают ни рубля риск-бюджета и не стоят в
    очереди позади корректировок.
    """
    cuts = [_cut(str(i)) for i in range(300)]
    taken, refused = _select(cuts, {lanes.LANE_HYGIENE: 1})
    assert refused == []
    assert len(taken) == 300
    assert all(a["risk_rub"] == 0.0 for a in taken)
    assert all(a["tier"] == tier.TIER_ARITHMETIC for a in taken)


def test_hygiene_is_capped_by_cut_share_of_account_spend():
    """Предохранитель сломанных данных: гигиена не режет кабинет за проход.

    Риском она не платит по построению, значит ограничитель у неё один и он в
    рублях — доля вырезаемого расхода кабинета за такт. При недельном расходе
    5,7 млн ₽ (замер 26.08.2026) это 285 000 ₽.
    """
    cuts = [_cut(str(i), cut_rub=200_000.0) for i in range(10)]
    taken, refused = _select(cuts, {lanes.LANE_HYGIENE: 1}, spend=5_700_000.0)
    cut_rub = -sum(a["payload"][expectation.RUB_KEY] for a in taken)
    assert cut_rub <= 5_700_000.0 * lanes.HYGIENE_MAX_CUT_SHARE
    assert refused, "предохранитель сломанных данных не сработал"


def test_refused_actions_carry_a_reason_and_no_deferred_status():
    # «Отложенного» как состояния нет: действие либо едет, либо отказано с
    # причиной и пересчитывается следующим тактом на свежих данных.
    actions = [_budget(f"b{i}", leads=1.0, daily=1_000_000.0) for i in range(50)]
    _, refused = _select(actions, {lanes.LANE_ALLOCATION: 1}, spend=1_000.0)
    assert refused
    assert all(a["blocked_reason"] == rejects.LANE_LIMIT for a in refused)
    assert all("deferred" not in a for a in refused)


def test_shadow_lane_writes_nothing_at_all():
    # Ступень 0 — режим приёмки рычага: агент пишет «сделал бы X», не делая.
    actions = [_budget("b1"), _tuning("c1", "SEG")]
    taken, refused = _select(actions, {lanes.LANE_ALLOCATION: 0,
                                       lanes.LANE_TUNING: 0})
    assert taken == []
    assert len(refused) == 2


def test_proposal_is_refused_by_its_own_reason_not_by_the_limit():
    # «Рычага нет» и «не влезло» — разные состояния. Слив их в одну причину,
    # мы читали бы отчёт как «полоса мала», хотя предложение не применяется
    # никогда и ни при какой ступени.
    action = {"action_kind": "proposal.campaign_master", "object_level": "campaign",
              "object_id": "c1", "key": "мк", "idempotency_key": "p1", "payload": {}}
    taken, refused = _select([action], {})
    assert taken == []
    assert refused[0]["blocked_reason"] == rejects.PROPOSAL


def test_transfer_pair_travels_whole_or_pays_full_price():
    """Уберите этот тест — и кабинет получит доливку по цене переноса.

    net_risk даёт скидку за встречное движение денег: донор и получатель
    вместе платят разрыв окупаемостей ОДИН раз. Скидка законна ровно тогда,
    когда компенсация действительно происходит. Взять получателя, а донора
    отложить — значит выдать скидку и не получить компенсации.
    """
    donor = _budget("b1", leads=-1.0, rub=-100_000.0, daily=20_000.0,
                    marginal_roi=1.0)
    taker = _budget("b2", leads=8.0, rub=+100_000.0, daily=20_000.0,
                    marginal_roi=1.1)
    full = risk.action_risk(taker, BIG_COST)

    # Бюджета хватает только на одну сторону из двух.
    taken, refused = _select([donor, taker], {lanes.LANE_ALLOCATION: 1},
                             spend=full * 100.0)
    keys = {a["object_id"] for a in taken}
    if keys == {"b2"}:
        assert taken[0]["risk_rub"] == pytest.approx(full), (
            "получатель поехал со скидкой за компенсацию, которой не будет")
    else:
        assert keys in ({"b1", "b2"}, {"b1"}, set())


def test_transfer_pair_keeps_its_discount_when_both_sides_travel():
    # Обратная половина: пара едет целиком — скидка заслужена, и перенос не
    # платит обеими сторонами сразу (дефект 8б: полоса перераспределения
    # заявила 921 690 ₽ на 48 действиях при неизменной сумме кабинета).
    donor = _budget("b1", leads=-1.0, rub=-100_000.0, daily=20_000.0,
                    marginal_roi=1.0)
    taker = _budget("b2", leads=8.0, rub=+100_000.0, daily=20_000.0,
                    marginal_roi=1.1)
    taken, refused = _select([donor, taker], {lanes.LANE_ALLOCATION: 1})
    assert {a["object_id"] for a in taken} == {"b1", "b2"} and refused == []
    charged = sum(a["risk_rub"] for a in taken)
    gross = sum(risk.action_risk(a, BIG_COST) for a in (donor, taker))
    assert charged < gross, "перенос снова платит обеими сторонами"


def test_selection_is_deterministic():
    # Один и тот же план обязан давать один и тот же срез, иначе разбор беты
    # не с чем сверять.
    actions = [_tuning(f"c{i}", "SEG", leads=1.0) for i in range(20)]
    first, _ = _select(list(actions), {lanes.LANE_TUNING: 1})
    second, _ = _select(list(reversed(actions)), {lanes.LANE_TUNING: 1})
    assert [a["idempotency_key"] for a in first] == \
           [a["idempotency_key"] for a in second]


def test_lane_budget_is_per_account_and_sums_to_the_run():
    # Карман полосы у каждого кабинета свой, и доля считается от расхода
    # ЭТОГО кабинета (agent_e1.run_account: account_share). Поэтому карманы
    # складываются ровно в тот один, что был бы посчитан на прогон целиком:
    # объём изменений за прогон не растёт, а порядок обхода перестаёт решать,
    # кому достанется лимит.
    first = [_budget("b1", leads=5.0, daily=1_000_000.0)]
    second = [_budget("b2", leads=5.0, daily=1_000_000.0)]
    price = risk.action_risk(first[0], BIG_COST)
    own_spend = price * 100.0

    taken1, _ = _select(first, {lanes.LANE_ALLOCATION: 1},
                        spend=own_spend, budgets={})
    taken2, refused2 = _select(second, {lanes.LANE_ALLOCATION: 1},
                               spend=own_spend, budgets={})
    assert len(taken1) == 1
    assert len(taken2) == 1 and refused2 == []

    # Одна тетрадь на оба кабинета — и второй голодает независимо от того,
    # насколько он крупный: карман вычерпан порядком обхода. Замер
    # 27.08.2026: крупнейший кабинет прогона получил 2 действия из 177
    # заявленных, потому что до него дошла очередь третьим.
    shared: dict = {}
    _select(first, {lanes.LANE_ALLOCATION: 1}, spend=own_spend, budgets=shared)
    starved_taken, starved_refused = _select(
        second, {lanes.LANE_ALLOCATION: 1}, spend=own_spend, budgets=shared)
    assert starved_taken == [] and len(starved_refused) == 1


def test_cap_actions_is_gone():
    # Лимит прогона удалён целиком, а не оставлен мёртвым рядом: пока он есть,
    # его можно позвать, и он снова начнёт выбирать за нас рычаг.
    assert not hasattr(guardrails, "cap_actions")
    assert not hasattr(guardrails, "MAX_ACTIONS_PER_RUN")
