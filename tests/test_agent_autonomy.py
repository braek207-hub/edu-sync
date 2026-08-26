# -*- coding: utf-8 -*-
"""
tests/test_agent_autonomy.py — лестница ступеней риска.

Данные — литералы в форме слота learning_loop.track_record: модуль ничего не
читает и не решает за полосу, он переводит послужной список в ступень.
"""

import pytest

from sync.agent import autonomy


# ------------------- ступени и доли


def test_shares_of_four_steps():
    assert autonomy.share_of(0) == 0.0
    assert autonomy.share_of(1) == 0.01
    assert autonomy.share_of(2) == 0.03
    assert autonomy.share_of(3) == 0.06


def test_steps_are_declared_in_order():
    assert [s.step for s in autonomy.STEPS] == [0, 1, 2, 3]


def test_unknown_step_raises_instead_of_granting_share():
    # Молчаливое приведение к ближайшей ступени выдало бы долю расхода по
    # ошибке вызова — здесь дешевле упасть, чем разрешить трату.
    with pytest.raises(ValueError):
        autonomy.share_of(4)
    with pytest.raises(ValueError):
        autonomy.share_of(-1)


# ------------------- четыре теста спеки


def test_unseen_lane_starts_in_shadow():
    assert autonomy.step_of("launch", {}) == 0
    assert autonomy.share_of(0) == 0.0


def test_twelve_closed_at_sixty_percent_reaches_step_one():
    record = {"closed": 12, "improved": 8, "worsened": 4,
              "money_confirmed": 0, "money_contradicted": 0}
    assert autonomy.step_of("tuning", record) == 1


def test_money_contradiction_blocks_step_two():
    record = {"closed": 30, "improved": 20, "worsened": 10,
              "money_confirmed": 2, "money_contradicted": 18}
    assert autonomy.step_of("allocation", record) == 1


def test_recent_failures_demote_by_one_step():
    record = {"closed": 50, "improved": 35, "worsened": 15,   # 70 % накопленных
              "money_confirmed": 12, "money_contradicted": 6,
              "recent_closed": 12, "recent_improved": 4}      # 33 % < 40 %
    assert autonomy.step_of("tuning", record) == 2            # было бы 3


# ------------------- края лестницы


def test_empty_and_missing_record_give_shadow():
    assert autonomy.step_of("tuning", {}) == 0
    assert autonomy.step_of("tuning", None) == 0


def test_closed_below_threshold_stays_in_shadow():
    record = {"closed": 11, "improved": 11, "worsened": 0}
    assert autonomy.step_of("tuning", record) == 0


def test_hit_rate_below_threshold_stays_in_shadow():
    record = {"closed": 40, "improved": 20, "worsened": 20}
    assert autonomy.step_of("tuning", record) == 0


def test_money_confirmation_opens_step_two():
    record = {"closed": 30, "improved": 20, "worsened": 10,
              "money_confirmed": 12, "money_contradicted": 4}
    assert autonomy.step_of("allocation", record) == 2


def test_silent_money_checkpoint_caps_the_lane_at_step_one():
    # Ни одного проверенного деньгами успеха — сторож второго чекпоинта ещё не
    # отработал. Это не свидетельство ПРОТИВ полосы (в тень она не уходит), но
    # и не подтверждение: ступени 2 и 3 требуют, чтобы деньги высказались.
    record = {"closed": 30, "improved": 20, "worsened": 10}
    assert autonomy.step_of("allocation", record) == 1

    # Объём наблюдений молчание не лечит: полоса с идеальными заявками и
    # неотработавшим сторожем остаётся на 1 %, продолжая нарабатывать ему
    # материал. Заперта она при этом не будет — применения на 1 % идут.
    brilliant = {"closed": 200, "improved": 190, "worsened": 10}
    assert autonomy.step_of("allocation", brilliant) == 1


def test_top_step_demands_a_higher_hit_rate_than_the_lower_ones():
    # 6 % недельного расхода — ≈ 342 тыс ₽ под непроверенными изменениями.
    # 60 % попаданий хватает на 1 % и 3 %, но не на верхнюю ступень.
    record = {"closed": 50, "improved": 31, "worsened": 19,   # 62 %
              "money_confirmed": 20, "money_contradicted": 5}
    assert autonomy.step_of("tuning", record) == 2
    assert autonomy.step_of("tuning", {**record, "improved": 34}) == 3  # 68 %


def test_top_step_needs_forty_eight_closed():
    record = {"closed": 47, "improved": 40, "worsened": 7,
              "money_confirmed": 30, "money_contradicted": 2}
    assert autonomy.step_of("tuning", record) == 2


def test_top_step_reached_with_full_record():
    record = {"closed": 48, "improved": 34, "worsened": 14,
              "money_confirmed": 25, "money_contradicted": 5}
    assert autonomy.step_of("tuning", record) == 3


# ------------------- падение мгновенное


def test_demotion_never_pushes_into_shadow():
    # Ступень 1 при свежем провале остаётся ступенью 1: тень не даёт применять,
    # применений нет — не будет и новых наблюдений, и полоса заперлась бы.
    record = {"closed": 12, "improved": 8, "worsened": 4,
              "recent_closed": 12, "recent_improved": 2}
    assert autonomy.step_of("tuning", record) == 1


def test_short_recent_window_is_not_evidence():
    # Меньше двенадцати свежих закрытых — судить не по чему, ступень держится.
    record = {"closed": 48, "improved": 34, "worsened": 14,
              "money_confirmed": 25, "money_contradicted": 5,
              "recent_closed": 5, "recent_improved": 1}
    assert autonomy.step_of("tuning", record) == 3


def test_recent_success_does_not_promote():
    # Свежая серия попаданий не заменяет накопленный объём наблюдений.
    record = {"closed": 12, "improved": 8, "worsened": 4,
              "recent_closed": 12, "recent_improved": 12}
    assert autonomy.step_of("tuning", record) == 1


# ------------------- тень задаётся снаружи


def test_shadow_lane_stays_at_zero_with_brilliant_record():
    record = {"closed": 200, "improved": 180, "worsened": 20,
              "money_confirmed": 150, "money_contradicted": 5}
    assert autonomy.step_of("launch", record, shadow_lanes={"launch"}) == 0
    assert autonomy.step_of("tuning", record, shadow_lanes={"launch"}) == 3


def test_shadow_membership_is_an_argument_not_a_module_constant():
    # Список теневых полос — решение человека, оно живёт в конфиге и меняется
    # без правки кода. Модуль не имеет права знать его наизусть.
    record = {"closed": 200, "improved": 180, "worsened": 20,
              "money_confirmed": 150, "money_contradicted": 5}
    assert autonomy.step_of("launch", record) == 3
    assert autonomy.is_shadow("launch", {"launch", "suspend"}) is True
    assert autonomy.is_shadow("launch", None) is False
