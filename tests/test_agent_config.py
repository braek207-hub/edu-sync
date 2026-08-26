# -*- coding: utf-8 -*-
"""Панель настроек агента (sync/agent/config.py).

Смысл слоя: параметры, которыми регулируется темп и осторожность, живут в
одном месте и меняются без правки кода. Пресет задаёт всё разом, отдельный
параметр можно переопределить поверх пресета.

Главный инвариант: защиту НЕЛЬЗЯ ослабить через настройки. Красные линии,
порог наблюдений и объёмная линия обвала не переопределяются ни пресетом, ни
вручную — иначе первая же «агрессивная» настройка снимает то, ради чего
контур построен.
"""

import pytest

from sync.agent.config import (
    DEFAULTS,
    LOCKED_KEYS,
    PRESETS,
    describe,
    resolve,
)


def test_defaults_match_the_code_constants():
    # Панель обязана начинать с того, что уже стоит в коде: иначе её
    # появление молча меняет поведение агента.
    from sync.agent.portfolio import EXPLORATION_SHARE
    from sync.agent.writer.budget import BUDGET_COOLDOWN_DAYS, MAX_WRITE_STEP
    from sync.agent.writer.switch import MAX_SUSPENDS_PER_RUN

    assert DEFAULTS["explore_share"] == EXPLORATION_SHARE
    assert DEFAULTS["budget_cooldown_days"] == BUDGET_COOLDOWN_DAYS
    assert DEFAULTS["max_write_step"] == MAX_WRITE_STEP
    assert DEFAULTS["max_suspends_per_run"] == MAX_SUSPENDS_PER_RUN


def test_preset_sets_everything_at_once():
    conservative = resolve(preset="conservative")
    aggressive = resolve(preset="aggressive")
    assert conservative["max_write_step"] < aggressive["max_write_step"]
    assert conservative["budget_cooldown_days"] > aggressive["budget_cooldown_days"]
    assert conservative["explore_share"] < aggressive["explore_share"]


def test_override_wins_over_preset():
    cfg = resolve(preset="conservative", overrides={"explore_share": 0.12})
    assert cfg["explore_share"] == 0.12


def test_protection_cannot_be_weakened_through_settings():
    # Ни пресетом, ни руками: попытка отклоняется с причиной, а значение
    # остаётся кодовым.
    with pytest.raises(ValueError, match="не переопределяется"):
        resolve(overrides={"red_line_tolerance": 5.0})
    assert "red_line_tolerance" in LOCKED_KEYS


def test_unknown_key_is_refused_not_ignored():
    # Опечатка в имени параметра — не «настройка по умолчанию», а ошибка:
    # молча проигнорированная настройка выглядит как применённая.
    with pytest.raises(ValueError, match="неизвестный параметр"):
        resolve(overrides={"explor_share": 0.1})


def test_value_outside_the_allowed_range_is_refused():
    with pytest.raises(ValueError, match="вне допустимого диапазона"):
        resolve(overrides={"explore_share": 0.9})
    with pytest.raises(ValueError, match="вне допустимого диапазона"):
        resolve(overrides={"p_sign_budget": 0.3})


def test_unknown_preset_is_refused():
    with pytest.raises(ValueError, match="неизвестный пресет"):
        resolve(preset="ultra")


def test_autonomy_values_are_closed_set():
    assert resolve(overrides={"autonomy": "suggest_only"})["autonomy"] == "suggest_only"
    with pytest.raises(ValueError, match="вне допустимого диапазона"):
        resolve(overrides={"autonomy": "yolo"})


def test_describe_lists_every_parameter_with_its_source():
    # Отчёт прогона обязан показывать, ОТКУДА взято каждое значение:
    # «пресет», «переопределено», «дефолт» — иначе непонятно, что менялось.
    rows = describe(preset="balanced", overrides={"explore_share": 0.12})
    by_key = {r["key"]: r for r in rows}
    assert by_key["explore_share"]["source"] == "override"
    assert by_key["max_write_step"]["source"] in ("preset", "default")
    assert all("value" in r for r in rows)
    assert set(by_key) == set(DEFAULTS)


def test_monthly_budget_cap_is_empty_until_the_owner_names_it():
    # Общий бюджет — деньги владельца: пока потолок месяца не задан, агент
    # рост только предлагает (sync/agent/portfolio.py::account_budget).
    # Пустое значение обязано быть законным, а не «настройкой по умолчанию,
    # которая случайно работает».
    assert DEFAULTS["monthly_budget_cap_rub"] is None
    assert resolve()["monthly_budget_cap_rub"] is None
    assert resolve(preset="balanced")["monthly_budget_cap_rub"] is None
    cfg = resolve(overrides={"monthly_budget_cap_rub": 3_000_000.0})
    assert cfg["monthly_budget_cap_rub"] == 3_000_000.0


def test_monthly_budget_cap_out_of_scale_is_refused():
    # Опечатка в порядке величины — самая дешёвая ошибка в этой настройке и
    # самая дорогая по последствиям.
    with pytest.raises(ValueError, match="вне допустимого диапазона"):
        resolve(overrides={"monthly_budget_cap_rub": 5_000_000_000.0})


def test_every_preset_is_valid():
    # Пресет, который сам не проходит валидацию, — мина: он применится в
    # первый же раз, когда его выберут.
    for name in PRESETS:
        resolve(preset=name)


def test_risk_share_is_in_spec_and_bounded():
    # Недельный риск-бюджет — доля расхода кабинета, и доля регулируется
    # панелью. Дефолт равен константе кода: появление ручки само по себе
    # поведения не меняет.
    from sync.agent.writer.risk import DEFAULT_RISK_SHARE_WEEK

    assert DEFAULTS["risk_share_week"] == DEFAULT_RISK_SHARE_WEEK
    assert resolve()["risk_share_week"] == DEFAULT_RISK_SHARE_WEEK
    assert resolve(overrides={"risk_share_week": 0.03})["risk_share_week"] == 0.03
    # 6 % недельного расхода под непроверенными изменениями — предел, за
    # которым «настройка темпа» становится снятием защиты.
    with pytest.raises(ValueError, match="вне допустимого диапазона"):
        resolve(overrides={"risk_share_week": 0.10})
    with pytest.raises(ValueError, match="вне допустимого диапазона"):
        resolve(overrides={"risk_share_week": -0.01})


def test_risk_budget_week_stays_locked():
    # Доля — ручка панели, АБСОЛЮТНЫЙ потолок — нет: он перебивает долю
    # (risk.weekly_limit), то есть отключает саму связь риска с расходом.
    # Такое ставит человек руками, а не пресет.
    assert "risk_budget_week" in LOCKED_KEYS
    assert "risk_budget_week" not in DEFAULTS
    with pytest.raises(ValueError, match="не переопределяется"):
        resolve(overrides={"risk_budget_week": 500_000.0})
