# -*- coding: utf-8 -*-
"""Недельный риск выдаётся долями по дням, а не целиком первому прогону.

Недельный лимит ограничивал неделю, но не прогон: один запуск вправе был
занять его весь. После перехода на дельта-цены действия подешевели в разы, и
понедельничный прогон стал выбирать почти весь недельный риск (в репетиции —
41 715 ₽ из 50 000 ₽), оставляя шесть дней без бюджета. Важное, замеченное в
среду, ждало бы следующей недели за спиной у неважного, замеченного в
понедельник, — и решала бы это не важность, а порядок сортировки.
"""

import sync.agent_e1 as agent_e1
from sync.agent.writer.risk import (
    DAYS_IN_WEEK,
    DEFAULT_WEEKLY_RISK_RUB,
    paced_allowance,
)


def test_first_day_gets_a_share_not_the_whole_week():
    allowance = paced_allowance(50_000.0, "2026-08-24", "2026-08-24")

    assert allowance == 50_000.0 / DAYS_IN_WEEK


def test_unspent_budget_rolls_forward():
    # Понедельник не тронули — во вторник доля больше: остаток тот же,
    # делителей меньше. Неистраченное не сгорает.
    monday = paced_allowance(50_000.0, "2026-08-24", "2026-08-24")
    tuesday = paced_allowance(50_000.0, "2026-08-25", "2026-08-24")

    assert tuesday > monday


def test_last_day_of_week_may_take_the_whole_remainder():
    # Воскресенье — последний день: копить дальше не для чего.
    assert paced_allowance(12_000.0, "2026-08-30", "2026-08-24") == 12_000.0


def test_day_outside_the_week_never_widens_the_allowance():
    # Дата за пределами недели — рассогласование часовых поясов или
    # заигравшийся аргумент --today. Делитель не должен становиться нулём
    # или отрицательным: это открыло бы весь остаток разом или, хуже,
    # перевернуло бы знак.
    assert paced_allowance(50_000.0, "2026-09-10", "2026-08-24") == 50_000.0
    assert paced_allowance(50_000.0, "2026-08-01", "2026-08-24") == 50_000.0 / DAYS_IN_WEEK


def test_exhausted_week_gives_nothing_to_share():
    assert paced_allowance(0.0, "2026-08-26", "2026-08-24") == 0.0


# --- недельный потолок прогона: доля расхода, ручной абсолют, пробел --------

def test_run_weekly_limit_is_a_share_of_the_run_spend(monkeypatch):
    # Расход берётся тем же справочником, которым считается цена действия:
    # сумма дневных темпов × 7. 100 000 ₽/день → 700 000 ₽ в неделю, 1 % = 7 000 ₽.
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit",
                        lambda wk, default_rub: default_rub)

    limit = agent_e1.weekly_risk_limit(
        "2026-08-24", {"1": 60_000.0, "2": 40_000.0}, {"risk_share_week": 0.01})

    assert limit == 7_000.0


def test_manual_weekly_budget_row_overrides_the_share(monkeypatch):
    # Строка в edu_agent_risk_budget — ручное решение человека
    # (risk_budget_week в LOCKED_KEYS). Оно не обязано сходиться с долей.
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit",
                        lambda wk, default_rub: 12_345.0)

    assert agent_e1.weekly_risk_limit(
        "2026-08-24", {"1": 100_000.0}, {"risk_share_week": 0.01}) == 12_345.0


def test_empty_spend_directory_falls_back_to_the_absolute_default(monkeypatch):
    # Пустой справочник — пробел в витрине или лаг синка. Ноль здесь отложил
    # бы все действия прогона, и отчёт был бы неотличим от исправной остановки.
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit",
                        lambda wk, default_rub: default_rub)

    assert agent_e1.weekly_risk_limit("2026-08-24", {}, None) == DEFAULT_WEEKLY_RISK_RUB
