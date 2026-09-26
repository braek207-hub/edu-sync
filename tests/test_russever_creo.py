# -*- coding: utf-8 -*-
"""Напоминание о смене креативов РосСеверЭкспо: фазы, тишина, лимиты Директа."""
import datetime as dt

import pytest

from sync.russever.cities import CITY, L, phase
from sync.russever.copy import TEXT_LIMIT, TITLE_LIMIT, texts, titles
from sync.russever_creo_alert import ORDER, build_message, city_state


def states(day: dt.date):
    return [city_state(s, day, None) for s in ORDER]


def test_phases_switch_by_date():
    C = CITY["magadan"]           # 2–13 сентября, 12 дней работы
    assert phase(C, dt.date(2026, 9, 1))[0] == "анонс"
    # На длинной выставке «последние дни» в первый же день — неправда,
    # поэтому до последней недели идёт фаза «идёт» (FINAL_DAYS = 6).
    assert phase(C, dt.date(2026, 9, 2))[0] == "идёт"
    assert phase(C, dt.date(2026, 9, 6))[0] == "идёт"
    assert phase(C, dt.date(2026, 9, 7))[0] == "финал"
    assert phase(C, dt.date(2026, 9, 13))[0] == "финал"


def test_short_expo_starts_in_final():
    C = CITY["lensk"]             # 7–11 октября, 5 дней — финал с первого дня
    assert phase(C, dt.date(2026, 10, 6))[0] == "анонс"
    assert phase(C, dt.date(2026, 10, 7))[0] == "финал"
    assert phase(C, dt.date(2026, 10, 11))[0] == "финал"


def test_quiet_when_nothing_changes():
    # 11.09: у всех городов фаза та же, что вчера и завтра.
    assert build_message(dt.date(2026, 9, 11), states(dt.date(2026, 9, 11))) is None


def test_transition_day_asks_to_change():
    day = dt.date(2026, 9, 2)      # Магадан открывается: анонс → идёт
    msg = build_message(day, states(day))
    assert msg is not None
    assert "МЕНЯТЬ СЕЙЧАС — Магадан" in msg
    assert "Уже открыто" in msg
    # Кампании города названы — иначе напоминание не довести до дела.
    assert "713748690" in msg


def test_day_before_transition_warns():
    day = dt.date(2026, 9, 15)     # 16.09 открывается Новый Уренгой
    msg = build_message(day, states(day))
    assert msg and "ЗАВТРА 16.09 — Новый Уренгой" in msg


def test_closing_reported_once():
    after = dt.date(2026, 9, 14)   # день после Магадана и Салехарда
    msg = build_message(after, states(after))
    assert msg and "выставка закончилась 13.09" in msg
    later = dt.date(2026, 9, 15)
    assert "закончилась" not in (build_message(later, states(later)) or "")


@pytest.mark.parametrize("slug", list(CITY))
def test_copy_fits_direct_limits(slug):
    C = CITY[slug]
    day = C["старт"] - dt.timedelta(days=1)
    while day <= C["конец"]:
        _, T = titles(C, day)
        _, TX, LINKS = texts(C, day)
        for t in T:
            assert L(t) <= TITLE_LIMIT, (slug, day, t)
        for t in TX:
            assert L(t, True) <= TEXT_LIMIT, (slug, day, t)
        for a, b in LINKS:
            assert L(a) <= 30 and L(b) <= 60, (slug, day, a, b)
        day += dt.timedelta(days=1)


def test_message_fits_telegram():
    day = dt.date(2026, 9, 1)
    while day <= dt.date(2026, 10, 15):
        msg = build_message(day, states(day)) or ""
        assert len(msg) <= 4000, (day, len(msg))
        day += dt.timedelta(days=1)
