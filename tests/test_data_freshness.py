# -*- coding: utf-8 -*-
from datetime import date, timedelta

from sync.data_freshness import (
    SOURCES,
    Finding,
    Source,
    SourceState,
    check_freshness,
    check_sources,
    check_volume,
)

TODAY = date(2026, 8, 2)
LEADS = Source("edunetwork", "crm_leads", max_lag_days=2)


def days(n: int) -> date:
    return TODAY - timedelta(days=n)


def week(count: int) -> dict[date, int]:
    """Ровная неделя по `count` строк в день (сегодня и семь дней назад)."""
    return {days(i): count for i in range(0, 10)}


# ---------- свежесть ----------


def test_flags_the_real_edu_outage_of_2026_07_29():
    """Настоящий случай: лиды встали 29 июля, обнаружено 2 августа на четвёртый день."""
    state = SourceState(LEADS, date(2026, 7, 29), {days(i): 0 for i in range(0, 4)})
    finding = check_freshness(state, TODAY)
    assert finding is not None and finding.is_critical
    assert "2026-07-29" in finding.message


def test_yesterday_is_fresh_enough():
    assert check_freshness(SourceState(LEADS, days(1)), TODAY) is None


def test_lag_within_allowance_is_silent():
    """max_lag_days=2 у витрин, которые приезжают с суточной задержкой."""
    assert check_freshness(SourceState(LEADS, days(2)), TODAY) is None


def test_lag_over_allowance_fires():
    assert check_freshness(SourceState(LEADS, days(3)), TODAY).is_critical


def test_empty_table_is_critical():
    finding = check_freshness(SourceState(LEADS, None), TODAY)
    assert finding is not None and finding.is_critical


def test_strict_source_requires_yesterday():
    """У витрин без задержки (max_lag_days=1) позавчерашний максимум — уже тревога."""
    strict = Source("lime", "lime_stats")
    assert check_freshness(SourceState(strict, days(1)), TODAY) is None
    assert check_freshness(SourceState(strict, days(2)), TODAY).is_critical


# ---------- объём ----------


def test_volume_hole_is_critical():
    """Поток идёт, но за позавчера пусто — это дыра, а не просадка."""
    rows = week(200)
    rows[days(2)] = 0
    finding = check_volume(SourceState(LEADS, days(1), rows), TODAY)
    assert finding is not None and finding.is_critical


def test_volume_halved_is_warning():
    rows = week(200)
    rows[days(2)] = 60
    finding = check_volume(SourceState(LEADS, days(1), rows), TODAY)
    assert finding is not None and finding.level == "warning"


def test_normal_volume_is_silent():
    assert check_volume(SourceState(LEADS, days(1), week(200)), TODAY) is None


def test_ordinary_fluctuation_is_silent():
    """Минус 30 % бывает и по-настоящему — порог ловит обвал, а не колебание."""
    rows = week(200)
    rows[days(2)] = 140
    assert check_volume(SourceState(LEADS, days(1), rows), TODAY) is None


def test_low_volume_table_is_silent():
    """Витрины на единицы строк в день шумят — молчим до volume_floor."""
    rows = week(4)
    rows[days(2)] = 0
    assert check_volume(SourceState(LEADS, days(1), rows), TODAY) is None


def test_yesterday_is_not_used_as_the_measured_day():
    """Вчерашний день у части источников ещё дозаливается — по нему не судим."""
    rows = week(200)
    rows[days(1)] = 10
    assert check_volume(SourceState(LEADS, days(1), rows), TODAY) is None


def test_empty_baseline_is_silent():
    """Новая витрина без истории не должна ронять прогон."""
    assert check_volume(SourceState(LEADS, days(1), {days(2): 0}), TODAY) is None


def test_baseline_ignores_the_hole_itself():
    """Один уже случившийся провал не должен опускать порог для следующего."""
    rows = week(200)
    rows[days(5)] = 0
    rows[days(2)] = 0
    assert check_volume(SourceState(LEADS, days(1), rows), TODAY).is_critical


# ---------- вместе ----------


def test_stale_source_reports_once():
    """У оборвавшегося потока объём провален по той же причине — второе сообщение лишнее."""
    state = SourceState(LEADS, date(2026, 7, 29), week(0))
    findings = check_sources([state], TODAY)
    assert len(findings) == 1
    assert "отставание" in findings[0].message


def test_healthy_sources_produce_nothing():
    state = SourceState(LEADS, days(1), week(200))
    assert check_sources([state], TODAY) == []


def test_registry_covers_all_four_dashboards():
    assert {s.dashboard for s in SOURCES} == {"lime", "edunetwork", "bjorn", "polinarepik"}


def test_registry_has_no_duplicate_tables():
    tables = [s.table for s in SOURCES]
    assert len(tables) == len(set(tables))


def test_finding_level_drives_criticality():
    assert Finding("critical", LEADS, "x").is_critical
    assert not Finding("warning", LEADS, "x").is_critical
