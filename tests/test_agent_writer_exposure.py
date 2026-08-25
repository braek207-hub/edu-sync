# -*- coding: utf-8 -*-
"""Цена риска по каждому рычагу: дельта изменения, а не весь объект."""

from sync.agent.writer import exposure


def test_segment_correction_costs_its_share_of_the_object_not_all_of_it():
    # Корректировка «−43 %» на сегменте, который берёт 4 % расхода кампании,
    # ставит под удар проценты от расхода, а не всю кампанию. Прежняя модель
    # брала за неё 38 876 ₽ из 50 000 недельного лимита — 78 % бюджета риска
    # за одно касание, отсюда один-два действия в неделю.
    exp = exposure.bid_modifier_exposure(-43, segment_share=0.04)

    at_risk, _ = exposure.daily_rub(exp, object_daily_cost=5554.0)

    assert at_risk < 5554.0 * 0.05
    # Проверяем и абсолют: 4 % сегмента × сдвиг 86 % от 5 554 ₽/день.
    assert round(at_risk, 1) == round(5554.0 * 0.04 * 0.86, 1)


def test_unknown_segment_share_means_the_whole_object():
    # «Долю установить не удалось» обязано стоить как весь объект: молчаливая
    # подстановка средней доли была бы дырой в гарантии — не отличить
    # «сегмент маленький» от «мы не знаем, какой он».
    exp = exposure.bid_modifier_exposure(-43, segment_share=None)

    at_risk, basis = exposure.daily_rub(exp, object_daily_cost=5554.0)

    assert at_risk == 5554.0
    assert "неизвестн" in basis


def test_half_correction_puts_the_whole_segment_at_risk():
    # Сдвиг на 50 % и больше перестраивает сегмент целиком: доля упирается
    # в единицу и дальше не растёт — цена не может превысить сам сегмент.
    exp = exposure.bid_modifier_exposure(-70, segment_share=0.30)

    at_risk, _ = exposure.daily_rub(exp, object_daily_cost=1000.0)

    assert at_risk == 300.0


def test_budget_move_costs_the_difference_not_the_whole_spend():
    # Прежний расход уже был и не становится сомнительным оттого, что потолок
    # подняли: под ударом ровно прирост.
    exp = exposure.budget_exposure(target_rub_per_day=1200.0,
                                   current_rub_per_day=1000.0)

    at_risk, _ = exposure.daily_rub(exp, object_daily_cost=1000.0)

    assert at_risk == 200.0


def test_budget_cut_costs_what_stops_being_spent():
    exp = exposure.budget_exposure(target_rub_per_day=700.0,
                                   current_rub_per_day=1000.0)

    at_risk, _ = exposure.daily_rub(exp, object_daily_cost=1000.0)

    assert at_risk == 300.0


def test_target_cpa_move_costs_its_relative_shift():
    # Цель 1 200 → 1 000 ₽ — сдвиг на шестую часть; с запасом эластичности
    # под ударом четверть расхода кампании, а не весь.
    exp = exposure.tcpa_exposure(target_cpa=1000.0, current_cpa=1200.0)

    at_risk, _ = exposure.daily_rub(exp, object_daily_cost=1000.0)

    assert round(at_risk) == 250


def test_unknown_current_target_means_the_whole_object():
    exp = exposure.tcpa_exposure(target_cpa=1000.0, current_cpa=0.0)

    at_risk, _ = exposure.daily_rub(exp, object_daily_cost=1000.0)

    assert at_risk == 1000.0


def test_negatives_cost_exactly_the_traffic_they_cut():
    # Минус-фраза не разгоняет ставки и не двигает бюджет — она убирает
    # конкретный поток, и цена ошибки равна этому потоку.
    exp = exposure.traffic_cut_exposure(120.0, "минус-фразы (10)")

    at_risk, basis = exposure.daily_rub(exp, object_daily_cost=5000.0)

    assert at_risk == 120.0
    assert "минус-фразы" in basis


def test_suspend_costs_the_whole_campaign():
    exp = exposure.whole_object_exposure("выключение кампании")

    at_risk, _ = exposure.daily_rub(exp, object_daily_cost=5000.0)

    assert at_risk == 5000.0


def test_schedule_costs_the_average_shift_over_the_day():
    # Профиль «одна ночная ставка −48 %» не стоит как сплошное понижение:
    # часы без правки входят в среднее нулями.
    percents = {str(h): 0 for h in range(24)}
    percents["3"] = -48

    exp = exposure.schedule_exposure(percents)
    at_risk, _ = exposure.daily_rub(exp, object_daily_cost=1000.0)

    assert round(at_risk) == 40


def test_action_without_exposure_still_costs_the_whole_object():
    # Рычаг, который экспозицию не заполняет, обязан остаться под прежней
    # гарантией: неизвестная дельта — это весь объект, а не ноль.
    at_risk, basis = exposure.daily_rub(None, object_daily_cost=800.0)

    assert at_risk == 800.0
    assert "весь объект" in basis
