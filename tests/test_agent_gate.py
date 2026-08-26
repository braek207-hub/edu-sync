# -*- coding: utf-8 -*-
"""Гейт данных пишущих прогонов (sync/agent/gate.py).

Витринную часть (mart_gate) покрывают тесты сторожа (facts_gate — тонкая
обёртка над ней); здесь — слой проверок ИСТОЧНИКА поверх витринного гейта
и сборка полного гейта для прямого применения (agent_e1).
"""

from datetime import date, timedelta

import sync.agent.gate as gate

TODAY = date(2026, 8, 24)


def _fresh_mart():
    """Витрина, широкая и свежая: 14 дней по 5 кампаний."""
    days = {(TODAY - timedelta(days=i)).isoformat(): 5 for i in range(1, 15)}
    return {"days": days, "campaigns_total": 5}


def _green_gate():
    out = gate.mart_gate(_fresh_mart(), TODAY)
    assert out["status"] == "GREEN"
    return out


def _rows(costs):
    """Строки источника: по одному дню на элемент costs, последний — свежайший."""
    n = len(costs)
    return [{"date": (TODAY - timedelta(days=n - i)).isoformat(), "cost": c}
            for i, c in enumerate(costs)]


def test_sum_mismatch_between_source_and_mart_turns_gate_red(monkeypatch):
    # Витрина собрана не из тех строк, что сейчас отдаёт источник, — свежесть
    # и ширина этого не видят (даты у битой витрины в порядке).
    monkeypatch.setattr(gate.agent_db, "load_direct_rows",
                        lambda a, b: _rows([100.0] * 10))
    monkeypatch.setattr(gate.agent_db, "mart_cost_total", lambda a, b: 500.0)
    out = gate.with_source_checks(_green_gate(), include_volume=False)
    assert out["status"] == "RED"
    assert "sums" in out["reason"]


def test_matching_sums_keep_gate_green(monkeypatch):
    monkeypatch.setattr(gate.agent_db, "load_direct_rows",
                        lambda a, b: _rows([100.0] * 10))
    monkeypatch.setattr(gate.agent_db, "mart_cost_total", lambda a, b: 1000.0)
    out = gate.with_source_checks(_green_gate(), include_volume=True)
    assert out["status"] == "GREEN"
    assert out["reason"] == ""


def test_volume_anomaly_gates_only_the_planner(monkeypatch):
    # Обвал/всплеск дневного объёма гейтует ПЛАНИРОВЩИКА (его оценки риска и
    # базы считаны по этим дням), но не сторожа: для сторожа обвал расхода —
    # возможное СЛЕДСТВИЕ вредного изменения, и замораживать откат по нему
    # нельзя.
    costs = [100.0] * 9 + [10000.0]
    monkeypatch.setattr(gate.agent_db, "load_direct_rows",
                        lambda a, b: _rows(costs))
    monkeypatch.setattr(gate.agent_db, "mart_cost_total",
                        lambda a, b: sum(costs))
    watchdog_view = gate.with_source_checks(_green_gate(), include_volume=False)
    planner_view = gate.with_source_checks(_green_gate(), include_volume=True)
    assert watchdog_view["status"] == "GREEN"
    assert planner_view["status"] == "RED"
    assert "volume" in planner_view["reason"]


def test_red_mart_gate_skips_source_checks(monkeypatch):
    # Пустая/протухшая витрина уже красная — источник не запрашивается вовсе.
    def _boom(*a, **k):
        raise AssertionError("источник не должен запрашиваться")
    monkeypatch.setattr(gate.agent_db, "load_direct_rows", _boom)
    red = gate.mart_gate({"days": {}, "campaigns_total": 0}, TODAY)
    out = gate.with_source_checks(red, include_volume=True)
    assert out["status"] == "RED"


def test_data_gate_builds_mart_over_its_window(monkeypatch):
    seen = {}

    def _breadth(date_from, date_to):
        seen["window"] = (date_from, date_to)
        return {"days": {}, "campaigns_total": 0}

    monkeypatch.setattr(gate.agent_db, "load_mart_day_breadth", _breadth)
    out = gate.data_gate(TODAY, window_days=14)
    assert out["status"] == "RED"
    assert seen["window"] == ((TODAY - timedelta(days=14)).isoformat(),
                              TODAY.isoformat())


def _mart_including_today():
    """Витрина, в которой ЕСТЬ сегодняшний день.

    Так выглядит боевая витрина в момент прогона записи: расчёт (agent_e0,
    09:30 МСК) уже записал сегодняшние строки, запись (agent_e1, 11:00 МСК)
    их видит. Прежняя фикстура начиналась со вчера — и потому ни один тест
    не мог увидеть, как гейт судит о неполном дне.
    """
    days = {(TODAY - timedelta(days=i)).isoformat(): 5 for i in range(0, 15)}
    return {"days": days, "campaigns_total": 5}


def _rows_through_today(costs):
    """Строки источника, последняя из которых — СЕГОДНЯ."""
    n = len(costs)
    return [{"date": (TODAY - timedelta(days=n - 1 - i)).isoformat(), "cost": c}
            for i, c in enumerate(costs)]


def test_partial_today_does_not_turn_the_planner_gate_red(monkeypatch):
    # Главный случай: сегодня в витрине есть, но день прошёл наполовину.
    # Судить по нему об аномалии объёма — значит краснеть КАЖДЫЙ день в
    # момент записи, то есть запретить запись навсегда.
    costs = [900_000.0] * 14 + [89_000.0]
    monkeypatch.setattr(gate.agent_db, "load_direct_rows",
                        lambda a, b: _rows_through_today(costs))
    monkeypatch.setattr(gate.agent_db, "mart_cost_total", lambda a, b: sum(costs))
    green = gate.mart_gate(_mart_including_today(), TODAY)
    assert green["latest_fact_date"] == TODAY.isoformat()
    out = gate.with_source_checks(green, include_volume=True, today=TODAY)
    assert out["status"] == "GREEN", out["reason"]


def test_collapse_on_the_last_complete_day_still_turns_gate_red(monkeypatch):
    # Обратная сторона: обвал ВЧЕРА (день полный) обязан краснеть, иначе
    # исключение сегодняшнего дня превратилось бы в отключение проверки.
    costs = [900_000.0] * 13 + [10_000.0] + [89_000.0]
    monkeypatch.setattr(gate.agent_db, "load_direct_rows",
                        lambda a, b: _rows_through_today(costs))
    monkeypatch.setattr(gate.agent_db, "mart_cost_total", lambda a, b: sum(costs))
    out = gate.with_source_checks(gate.mart_gate(_mart_including_today(), TODAY),
                                  include_volume=True, today=TODAY)
    assert out["status"] == "RED"
    assert "volume" in out["reason"]


def test_data_gate_passes_today_into_source_checks(monkeypatch):
    # data_gate знает сегодняшнюю дату; если он её не пробросит, исключение
    # неполного дня не сработает именно там, где оно нужно, — в боевом
    # прогоне записи.
    costs = [900_000.0] * 14 + [89_000.0]
    monkeypatch.setattr(gate.agent_db, "load_mart_day_breadth",
                        lambda a, b: _mart_including_today())
    monkeypatch.setattr(gate.agent_db, "load_direct_rows",
                        lambda a, b: _rows_through_today(costs))
    monkeypatch.setattr(gate.agent_db, "mart_cost_total", lambda a, b: sum(costs))
    assert gate.data_gate(TODAY)["status"] == "GREEN"
