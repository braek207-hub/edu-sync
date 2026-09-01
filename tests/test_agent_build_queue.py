# -*- coding: utf-8 -*-
"""
tests/test_agent_build_queue.py — очередь нарядов билдеру со стороны агента
(sync/agent/build_queue.py, docs/BUILD-ORDER-QUEUE.md).

Проверяется здесь то, что у обмена между двумя репозиториями ломается молча и
дорого:

  * тело наряда, переписанное ПОСЛЕ того, как билдер его взял. Половина
    кампании собралась бы по одному составу фраз, а кросс-минусовка доноров —
    по другому, и обе наши кампании остались бы в одном аукционе;
  * ответ «собрал» без campaign_id. Наблюдать нечего, откатывать нечего, а
    статус говорит, что всё хорошо;
  * наблюдение, заведённое от дня СОЗДАНИЯ кампании. Кампания создаётся на
    паузе, и горизонт замера съели бы дни простоя;
  * наряд, воскресший из закрытого статуса очередным прогоном генератора: в
    кабинете появилась бы вторая кампания на тот же наряд.

БД не требуется: проверяются чистые функции слоя (queue_row, merge_queued,
accept_row, fail_row, observation) — тот же приём, что и в
tests/test_agent_build_order.py.
"""

import pytest

from sync.agent import build_order, build_queue, experiments
from sync.agent.writer import launch


ACCOUNT = "account1-506453-ln8s"


def _order(**over):
    order = {
        "order_id": "consolidate-vpo",
        "idea_id": "d96b5cf53b8073c1c6d122e5",
        "kind": build_order.KIND_CONSOLIDATE,
        "account": ACCOUNT,
        "level_slug": "vpo_consolidate",
        "campaign_name": "vpo / consolidate / consolidate-vpo",
        "direction": "vpo",
        "queries": [
            {"phrase": "колледж заочно москва", "donor_campaign_id": "111",
             "cost_rub": 18_400.0, "conversions": 12},
        ],
        "donor_negatives": [
            {"campaign_id": "111", "phrases": ["колледж заочно москва"]},
        ],
        "campaign": {"weekly_budget": 70_000, "target_cpa": 1_600,
                     "counter_id": 98_627_983, "goal_id": 360_811_375},
        "window_days": 30,
        "horizon_days": 30,
        "success_rule": {"metric": "cpa_rub", "comparison": "vs_donors",
                         "threshold": 1_600.0},
    }
    order.update(over)
    return order


def _built(**over):
    """Строка наряда, на который билдер уже ответил «собрал и включил»."""
    row = {**build_queue.queue_row(_order()),
           "status": build_queue.STATUS_TAKEN}
    row = build_queue.accept_row(row, campaign_id="555",
                                 started_on="2026-09-03")
    row.update(over)
    return row


# ------------------------------------------------------------- постановка


def test_queue_row_carries_the_checked_order_not_the_raw_one():
    # Билдер собирает ровно то, что проверил валидатор. Разъедься эти два
    # текста — проверка была бы о другом наряде.
    row = build_queue.queue_row(_order(queries=[
        {"phrase": "  Колледж   Заочно Москва ", "donor_campaign_id": "111",
         "cost_rub": 18_400.0, "conversions": 12}]))
    assert row["order_json"]["queries"][0]["phrase"] == "колледж заочно москва"


def test_broken_order_is_refused_at_the_sender():
    # Наряд с дырой, доехавший до очереди, ждал бы исполнения, которого ему
    # нельзя дать, и выглядел бы для человека исправным.
    with pytest.raises(ValueError):
        build_queue.queue_row(_order(donor_negatives=[]))


def test_queued_order_is_refreshed_by_a_later_run():
    # Состав фраз плавает каждым прогоном, и билдеру обязан достаться
    # последний.
    old = build_queue.queue_row(_order())
    fresh = build_queue.queue_row(_order(campaign_name="vpo / new / consolidate-vpo"))
    merged = build_queue.merge_queued(old, fresh)
    assert merged["campaign_name"] == "vpo / new / consolidate-vpo"


def test_taken_order_cannot_be_rewritten():
    taken = {**build_queue.queue_row(_order()),
             "status": build_queue.STATUS_TAKEN}
    with pytest.raises(build_queue.OrderFrozen):
        build_queue.merge_queued(taken, build_queue.queue_row(_order()))


def test_closed_order_is_not_resurrected_by_a_generator_run():
    # Иначе в кабинете появилась бы вторая кампания на тот же наряд.
    done = _built()
    merged = build_queue.merge_queued(done, build_queue.queue_row(_order()))
    assert merged["status"] == build_queue.STATUS_BUILT
    assert merged["campaign_id"] == "555"


# ------------------------------------------------------------------ ответ


def test_built_without_campaign_id_is_refused():
    taken = {**build_queue.queue_row(_order()),
             "status": build_queue.STATUS_TAKEN}
    with pytest.raises(ValueError):
        build_queue.accept_row(taken, campaign_id="")


def test_built_without_start_date_is_a_legal_answer():
    # Кампания создаётся на паузе: дня первой открутки в момент сборки ещё
    # не существует.
    taken = {**build_queue.queue_row(_order()),
             "status": build_queue.STATUS_TAKEN}
    row = build_queue.accept_row(taken, campaign_id="555")
    assert row["status"] == build_queue.STATUS_BUILT
    assert row["started_on"] is None


def test_start_date_can_arrive_with_a_second_answer():
    taken = {**build_queue.queue_row(_order()),
             "status": build_queue.STATUS_TAKEN}
    paused = build_queue.accept_row(taken, campaign_id="555")
    live = build_queue.accept_row(paused, campaign_id="555",
                                  started_on="2026-09-03")
    assert live["started_on"] == "2026-09-03"


def test_failure_without_a_reason_is_refused():
    # Молча закрытый наряд неотличим от наряда, до которого не дошли руки.
    taken = {**build_queue.queue_row(_order()),
             "status": build_queue.STATUS_TAKEN}
    with pytest.raises(ValueError):
        build_queue.fail_row(taken, "")


def test_illegal_transition_raises_instead_of_passing_silently():
    done = _built()
    with pytest.raises(build_queue.IllegalTransition):
        build_queue.fail_row(done, "передумали")


# ------------------------------------------------------------ наблюдение


def test_no_observation_before_the_campaign_starts_spending():
    taken = {**build_queue.queue_row(_order()),
             "status": build_queue.STATUS_TAKEN}
    assert build_queue.observation(taken) is None
    assert build_queue.observation(
        build_queue.accept_row(taken, campaign_id="555")) is None


def test_observation_window_starts_on_the_first_spending_day():
    # Не на дне создания: между созданием и включением стоит человек, и
    # горизонт замера съели бы дни простоя.
    assert build_queue.observation(_built())["started_on"] == "2026-09-03"


def test_observation_addresses_the_campaign_the_builder_returned():
    watch = build_queue.observation(_built())
    assert watch["object_id"] == "555"
    assert watch["object_level"] == "campaign"


def test_observation_is_measured_against_the_holdout():
    # Своей истории у новой кампании нет вовсе: «до и после» физически
    # невозможно, и контроль остаётся один — заповедник.
    watch = build_queue.observation(_built())
    assert watch["mechanism"] == "vs_holdout"
    assert watch["params"]["control"] == "holdout"


def test_observation_is_class_b_not_a():
    # Контроль есть, но кампания попала в опыт решением агента, а не жребием.
    assert build_queue.observation(_built())["reliability_class"] == "B"


def test_observation_judges_by_the_metric_written_in_the_order():
    # Своя метрика отчёта была бы оценкой задним числом.
    watch = build_queue.observation(_built())
    assert watch["metric"] == "cpa_rub"


def test_observation_horizon_is_the_one_from_the_order():
    assert build_queue.observation(_built())["horizon_days"] == 30


def test_stake_is_the_donor_money_that_moves_not_a_new_pocket():
    # Недельный лимит равен тому, что доноры по этим фразам уже тратят:
    # 70 000 / 7 × 30 дней горизонта.
    watch = build_queue.observation(_built())
    assert watch["stake_rub"] == pytest.approx(300_000.0)
    assert watch["stake_source"] == build_queue.STAKE_SOURCE


def test_observation_id_matches_the_launch_action_key():
    # Стань campaign.create применимым рычагом — посмертная запись сторожа
    # легла бы в ЭТУ ЖЕ строку, а не завела вторую.
    action = launch.build(build_order.validate(_order()))
    watch = build_queue.observation(_built())
    assert watch["experiment_id"] == experiments.experiment_id_for(
        action["idempotency_key"])
    assert watch["idempotency_key"] == action["idempotency_key"]


def test_observation_names_the_donor_campaigns():
    # Без них разбор не сможет отличить переезд трафика от нового спроса.
    assert build_queue.observation(_built())["params"]["donor_campaign_ids"] == ["111"]


def test_success_criterion_repeats_the_order_not_the_report():
    text = build_queue.success_criterion(build_order.validate(_order()))
    assert "1600" in text and "донор" in text


# ------------------------------------------------------- жизненный цикл


def test_lifecycle_has_no_way_back_from_built_to_queued():
    with pytest.raises(build_queue.IllegalTransition):
        build_queue.check_transition(build_queue.STATUS_BUILT,
                                     build_queue.STATUS_QUEUED)


def test_failed_order_may_return_to_the_queue():
    # Причину устранили — наряд возвращается; это решение человека.
    build_queue.check_transition(build_queue.STATUS_FAILED,
                                 build_queue.STATUS_QUEUED)


def test_cancelled_is_final():
    for target in (build_queue.STATUS_QUEUED, build_queue.STATUS_BUILT):
        with pytest.raises(build_queue.IllegalTransition):
            build_queue.check_transition(build_queue.STATUS_CANCELLED, target)


def test_open_and_closed_statuses_cover_the_lifecycle():
    # Статус, не попавший ни в одну половину, выпал бы и из очереди билдера,
    # и из истории — и наряд завис бы, не будучи нигде виден.
    assert (set(build_queue.OPEN_STATUSES) | set(build_queue.CLOSED_STATUSES)
            == set(build_queue.LEGAL_TRANSITIONS))
