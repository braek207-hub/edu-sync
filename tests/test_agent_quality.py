# -*- coding: utf-8 -*-
"""Качество когорты как ранний тормоз роста.

Доливка бюджета расширяет охват, и новый трафик холоднее старого. CPA этого
не показывает — заявка остаётся заявкой; деньги показывают, но через 35 дней.
Средний ML-скор оплаты меняется на следующий день после того, как когорта
испортилась, и это единственный сигнал в системе, успевающий остановить
доливку до того, как месяц бюджета уйдёт в неплатящих.
"""

from sync.agent.growth import growth_candidates
from sync.agent.quality import (MIN_QUALITY_LEADS, QUALITY_DROP_LIMIT,
                                lead_quality, lead_quality_section,
                                quality_drift)


def _facts(cid, day, leads, p_pay, scored=None):
    return {"campaign_id": cid, "fact_date": day, "eff_leads": leads,
            "sum_p_pay": p_pay, "scored_leads": leads if scored is None else scored}


def test_avg_score_is_per_scored_lead_not_per_day():
    rows = [_facts("111", "2026-08-01", 10, 3.0), _facts("111", "2026-08-02", 30, 3.0)]
    out = lead_quality(rows, "2026-08-01", "2026-08-02")
    assert out["111"]["scored_leads"] == 40
    assert out["111"]["avg_p_pay"] == 0.15     # 6.0 / 40, а не среднее из 0.3 и 0.1


def test_unscored_leads_do_not_dilute_quality():
    # 40 лидов, скор есть у 20. Отношение к eff_leads дало бы 0.15 вместо
    # честных 0.30 — и «падение качества» при любом росте доли лендов без
    # client_id. Покрытие при этом видно отдельным числом.
    rows = [_facts("111", "2026-08-01", 40, 6.0, scored=20)]
    out = lead_quality(rows, "2026-08-01", "2026-08-02")
    assert out["111"]["avg_p_pay"] == 0.30
    assert out["111"]["coverage"] == 0.5


def test_rows_outside_the_window_are_ignored():
    rows = [_facts("111", "2026-07-31", 100, 30.0), _facts("111", "2026-08-01", 10, 1.0)]
    out = lead_quality(rows, "2026-08-01", "2026-08-02")
    assert out["111"]["scored_leads"] == 10
    assert out["111"]["avg_p_pay"] == 0.10


def test_campaign_without_scores_gets_zero_not_division_error():
    rows = [_facts("111", "2026-08-01", 40, 0.0, scored=0)]
    out = lead_quality(rows, "2026-08-01", "2026-08-02")
    assert out["111"]["avg_p_pay"] == 0.0
    assert out["111"]["coverage"] == 0.0


def test_quality_drop_flags_campaign():
    before = {"111": {"avg_p_pay": 0.20, "scored_leads": 40}}
    after = {"111": {"avg_p_pay": 0.14, "scored_leads": 40}}      # −30 %
    out = quality_drift(before, after)
    assert out["111"]["flagged"] is True
    assert out["111"]["drop"] == 0.3


def test_small_drop_is_not_a_flag():
    before = {"111": {"avg_p_pay": 0.20, "scored_leads": 40}}
    after = {"111": {"avg_p_pay": 0.18, "scored_leads": 40}}      # −10 %, шум
    out = quality_drift(before, after)
    assert out["111"]["flagged"] is False


def test_growing_quality_is_never_a_flag():
    before = {"111": {"avg_p_pay": 0.10, "scored_leads": 40}}
    after = {"111": {"avg_p_pay": 0.20, "scored_leads": 40}}
    out = quality_drift(before, after)
    assert out["111"]["flagged"] is False
    assert out["111"]["drop"] < 0


def test_thin_cohort_is_not_judged():
    # Пять лидов дадут разброс среднего скора больше любого порога.
    before = {"111": {"avg_p_pay": 0.20, "scored_leads": 5}}
    after = {"111": {"avg_p_pay": 0.10, "scored_leads": 5}}
    out = quality_drift(before, after)
    assert out["111"]["flagged"] is False
    assert out["111"]["reason"] == "мало наблюдений"


def test_campaign_new_in_the_second_window_is_not_judged():
    # Сравнивать не с чем: до доливки кампании не было. Пустое «до» обязано
    # читаться как отсутствие наблюдений, а не как падение с нуля.
    out = quality_drift({}, {"111": {"avg_p_pay": 0.10, "scored_leads": 40}})
    assert out["111"]["flagged"] is False
    assert out["111"]["reason"] == "мало наблюдений"


def test_coverage_collapse_is_reported_apart_from_quality():
    # Ingest поведения встал: скор пропал у половины лидов. Средний скор при
    # этом не шелохнулся — судить кампанию по нему нельзя, а знать о поломке
    # надо, поэтому это отдельная строка отчёта и НЕ повод тормозить рост.
    before = {"111": {"avg_p_pay": 0.20, "scored_leads": 40, "leads": 40, "coverage": 1.0}}
    after = {"111": {"avg_p_pay": 0.20, "scored_leads": 20, "leads": 40, "coverage": 0.5}}
    out = quality_drift(before, after)
    assert out["111"]["flagged"] is False
    assert out["111"]["coverage_drop"] == 0.5


def _window_rows():
    return [
        # До доливки: 40 лидов со скором, средний 0.20.
        _facts("111", "2026-08-01", 40, 8.0),
        # После: те же 40 лидов, средний 0.12 — минус 40 %.
        _facts("111", "2026-08-15", 40, 4.8),
        # Кампания без падения — в секцию попасть не должна.
        _facts("222", "2026-08-01", 40, 8.0),
        _facts("222", "2026-08-15", 40, 7.6),
    ]


def test_section_names_the_campaign_and_carries_drift_for_growth():
    section = lead_quality_section(_window_rows(), "2026-08-01", "2026-08-14",
                                   "2026-08-15", "2026-08-28")
    assert [f["campaign_id"] for f in section["flagged"]] == ["111"]
    row = section["flagged"][0]
    assert row["avg_p_pay_before"] == 0.20
    assert row["avg_p_pay_after"] == 0.12
    assert row["drop"] == 0.4
    # Ровно та карта, которую ждёт growth.py: ключ campaign_id, поле flagged.
    assert section["drift"]["111"]["flagged"] is True
    assert section["drift"]["222"]["flagged"] is False


def test_section_is_printed_even_when_nothing_fell():
    section = lead_quality_section([], "2026-08-01", "2026-08-14",
                                   "2026-08-15", "2026-08-28")
    # Отсутствие секции неотличимо от отсутствия падений, а различать их нужно.
    assert section["flagged"] == []
    assert section["coverage_alerts"] == []
    assert section["drift"] == {}


def test_section_drift_stops_the_growth_candidate():
    """Сквозная проверка: витрина фактов → тормоз → список усиления.

    Форма карты падения проверяется не на выдуманном словаре, а на том,
    что реально отдаёт quality_drift: разъедься эти двое ключами, тормоз
    молча перестал бы срабатывать — growth.py читает только "flagged".
    """
    section = lead_quality_section(_window_rows(), "2026-08-01", "2026-08-14",
                                   "2026-08-15", "2026-08-28")
    portfolio = {"accounts": {"acc": {"lambda": 1.0, "moves": {
        "111": {"direction": "vpo", "cost_28d": 100_000.0, "target_28d": 150_000.0,
                "marginal_roi_vs_lambda": 2.0, "step_capped": False,
                "limit_binding": True},
    }}}}
    headroom = {"111": {"headroom_share": 0.5, "verdict": "есть куда расти"}}

    out = growth_candidates(portfolio, headroom, {}, expansion=[],
                            quality_drift=section["drift"])

    assert out["candidates"] == []
    assert out["skipped_by_quality"] == 1


def test_thresholds_are_named_not_inlined():
    assert QUALITY_DROP_LIMIT == 0.2
    assert MIN_QUALITY_LEADS == 20
