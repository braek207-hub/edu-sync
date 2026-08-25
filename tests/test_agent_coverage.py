# -*- coding: utf-8 -*-
"""Слепая доля: сколько расхода идёт мимо настроек, которые агент читает."""

from sync.agent.coverage import blind_share, blind_spend

WINDOW = ("2026-08-01", "2026-08-28")


def _fact(campaign_id, cost, name=None, day="2026-08-02"):
    return {"fact_date": day, "campaign_id": campaign_id, "cost": cost,
            "campaign_name": name or f"campaign-{campaign_id}"}


def test_share_counts_cost_not_campaigns():
    facts = [_fact("111", 850_000.0), _fact("222", 150_000.0)]
    out = blind_spend(facts, [{"campaign_id": "111"}], *WINDOW)
    assert out["cost_total"] == 1_000_000.0
    assert out["cost_blind"] == 150_000.0
    assert out["blind_share"] == 0.15
    assert out["campaigns_blind"] == 1


def test_all_covered_gives_zero_share():
    facts = [_fact("111", 100.0)]
    out = blind_spend(facts, [{"campaign_id": "111"}], *WINDOW)
    assert out["blind_share"] == 0.0
    assert out["sample"] == []


def test_no_settings_at_all_is_full_blindness():
    # Пустая витрина настроек — это не «всё видно», а «не видно ничего».
    facts = [_fact("111", 100.0)]
    out = blind_spend(facts, [], *WINDOW)
    assert out["blind_share"] == 1.0


def test_zero_cost_window_does_not_divide_by_zero():
    out = blind_spend([_fact("111", 0.0)], [], *WINDOW)
    assert out["cost_total"] == 0.0
    assert out["blind_share"] == 0.0


def test_sample_is_ordered_by_cost():
    facts = [_fact("111", 10.0, "мелкая"), _fact("222", 900.0, "крупная")]
    out = blind_spend(facts, [], *WINDOW)
    assert [s["campaign_name"] for s in out["sample"]] == ["крупная", "мелкая"]


def test_days_outside_window_are_ignored():
    facts = [_fact("111", 500.0, day="2026-07-01"), _fact("222", 100.0)]
    out = blind_spend(facts, [], *WINDOW)
    assert out["cost_total"] == 100.0


def test_settings_accepted_as_mapping_from_db_loader():
    # agent_db.load_campaign_settings_raw() (sync/agent/db.py:329) отдаёт
    # СЛОВАРЬ campaign_id -> settings, а не список строк. Такт зовёт счётчик
    # именно им, поэтому обе формы должны читаться одинаково.
    facts = [_fact("111", 850_000.0), _fact("222", 150_000.0)]
    out = blind_spend(facts, {"111": {"BudgetWeekly": 1000}}, *WINDOW)
    assert out["cost_blind"] == 150_000.0
    assert out["campaigns_blind"] == 1


# --------------------------- ядро: готовый расход без сырых фактов


def test_core_counts_the_same_share_from_ready_cost():
    # Такт записи приходит со своим агрегатом расхода (средний дневной × 28),
    # фактов у него нет. Доля обязана считаться тем же кодом, иначе два такта
    # однажды напечатают разные числа под одним именем.
    out = blind_share({"111": 850_000.0, "222": 150_000.0}, [{"campaign_id": "111"}])
    assert out["cost_total"] == 1_000_000.0
    assert out["blind_share"] == 0.15
    assert out["campaigns_blind"] == 1


def test_core_sample_without_names_keeps_ids():
    # Имён кампаний у такта записи нет — образец обязан остаться полезным.
    out = blind_share({"222": 150_000.0}, {})
    assert out["sample"] == [{"campaign_id": "222", "campaign_name": "",
                              "cost": 150_000.0}]
