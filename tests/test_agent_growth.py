# -*- coding: utf-8 -*-
"""Кандидаты на усиление: что агент предлагает УСИЛИТЬ на каждом такте.

Агент, умеющий только резать, честно ведёт кабинет к «эффективно и мало».
Здесь проверяется вторая половина: такт обязан назвать, что усилить, и во
сколько рублей это оценивается — РАЗДЕЛЬНО по рычагам, потому что доливка
бюджета работает только там, где лимит связывает расход (9 кампаний из 62,
docs/AGENT-AUDIT-2026-08-23.md:214), а остальным нужна цена конверсии.
"""

from sync.agent.growth import growth_candidates


def _move(cost, target, roi, *, capped=False, binding=True, direction="vpo"):
    return {"direction": direction, "cost_28d": cost, "target_28d": target,
            "marginal_roi_vs_lambda": roi, "step_capped": capped,
            "limit_binding": binding}


PORTFOLIO = {"accounts": {"acc": {"lambda": 1.0, "moves": {
    "111": _move(100_000.0, 150_000.0, 2.0, capped=True),
    "222": _move(100_000.0, 90_000.0, 0.8),
}}}}
HEADROOM = {"111": {"headroom_share": 0.5, "verdict": "есть куда расти"},
            "222": {"headroom_share": 0.02, "verdict": "выкуплен"}}
DEMAND = {"vpo": {"regime": "подъём"}, "spo": {"regime": "норма"}}


def test_campaign_with_room_and_economics_is_candidate():
    out = growth_candidates(PORTFOLIO, HEADROOM, DEMAND, expansion=[])
    ids = [c["campaign_id"] for c in out["candidates"]]
    assert "111" in ids
    assert "222" not in ids


def test_step_capped_campaigns_counted():
    out = growth_candidates(PORTFOLIO, HEADROOM, DEMAND, expansion=[])
    assert out["capped_by_step"] == 1


def test_rising_directions_listed():
    out = growth_candidates(PORTFOLIO, HEADROOM, DEMAND, expansion=[])
    assert out["directions_rising"] == ["vpo"]
    # Кандидат обязан знать, растёт ли его рынок: «усилить» на спаде и на
    # подъёме — разные по риску решения.
    assert out["candidates"][0]["direction_rising"] is True


def test_room_rub_counts_only_profitable_with_headroom():
    out = growth_candidates(PORTFOLIO, HEADROOM, DEMAND, expansion=[])
    # 111: цель выше факта на 50 000 и упёрлась в кап — запас считается по ней.
    assert out["room_rub_total"] == 50_000.0


def test_expansion_candidates_carried_through():
    out = growth_candidates(PORTFOLIO, HEADROOM, DEMAND,
                            expansion=[{"query": "колледж заочно", "headroom": 3_000.0}])
    sources = {c["source"] for c in out["candidates"]}
    assert "expansion" in sources


def test_empty_inputs_give_empty_answer_not_crash():
    out = growth_candidates({"accounts": {}}, {}, {}, expansion=[])
    assert out["candidates"] == []
    assert out["room_rub_total"] == 0.0
    assert out["room_rub_budget"] == 0.0
    assert out["room_rub_tcpa"] == 0.0


# --------------- рычаг: два разных числа, а не одно

def test_lever_is_budget_only_when_the_limit_binds_spend():
    out = growth_candidates(PORTFOLIO, HEADROOM, DEMAND, expansion=[])
    assert out["candidates"][0]["lever"] == "budget"


def test_campaign_without_binding_limit_gets_the_price_lever():
    """Лимит не связывает расход — поднятый потолок не купит ни одного показа.

    Единственный рычаг такой кампании — целевая цена конверсии. Назвать её
    кандидатом «на доливку бюджета» значило бы записать в план освоения
    деньги, которые кабинет физически не выберет.
    """
    portfolio = {"accounts": {"acc": {"lambda": 1.0, "moves": {
        "111": _move(100_000.0, 150_000.0, 2.0, binding=False),
    }}}}
    out = growth_candidates(portfolio, HEADROOM, DEMAND, expansion=[])
    assert out["candidates"][0]["lever"] == "tcpa"


def test_room_is_split_by_lever_and_the_split_sums_to_the_total():
    """Задача 11 подаёт в рост кабинета room_rub_budget, а не total.

    Подать total значит вырастить кабинет на сумму, часть которой доедет
    только после эскалации цены — то есть не в этом такте и не этим рычагом.
    """
    portfolio = {"accounts": {"acc": {"lambda": 1.0, "moves": {
        "111": _move(100_000.0, 150_000.0, 2.0),
        "333": _move(50_000.0, 70_000.0, 1.4, binding=False),
    }}}}
    headroom = dict(HEADROOM, **{"333": {"headroom_share": 0.4,
                                         "verdict": "есть куда расти"}})
    out = growth_candidates(portfolio, headroom, DEMAND, expansion=[])

    assert out["room_rub_budget"] == 50_000.0
    assert out["room_rub_tcpa"] == 20_000.0
    assert out["room_rub_total"] == 70_000.0


def test_expansion_money_stays_out_of_both_lever_sums():
    # Недополученная выгода запроса — не рубли, которые кабинет освоит
    # доливкой: у него сначала должна появиться своя группа.
    out = growth_candidates(PORTFOLIO, HEADROOM, DEMAND,
                            expansion=[{"query": "колледж заочно", "headroom": 3_000.0}])
    assert out["room_rub_total"] == 50_000.0
    assert out["room_rub_budget"] + out["room_rub_tcpa"] == 50_000.0


def test_solver_hitting_the_step_cap_is_a_candidate_on_its_own():
    """Кап шага — самостоятельный повод: солвер хотел дать больше, чем можно.

    Замера недобора при этом может не быть вовсе (сети, мало показов), и
    молчать о такой кампании нельзя — это и есть «что усилить».
    """
    portfolio = {"accounts": {"acc": {"lambda": 1.0, "moves": {
        "444": _move(100_000.0, 150_000.0, 2.0, capped=True),
    }}}}
    out = growth_candidates(portfolio, {"444": {"verdict": "неопределённо",
                                                "headroom_share": None}},
                            DEMAND, expansion=[])
    assert [c["source"] for c in out["candidates"]] == ["step_cap"]


def test_measured_absence_of_room_is_not_a_candidate_even_at_the_cap():
    # «Выкуплен» — это ЗАМЕР: показов больше нет, деньгам некуда идти.
    portfolio = {"accounts": {"acc": {"lambda": 1.0, "moves": {
        "222": _move(100_000.0, 150_000.0, 2.0, capped=True),
    }}}}
    out = growth_candidates(portfolio, HEADROOM, DEMAND, expansion=[])
    assert out["candidates"] == []
    assert out["room_rub_total"] == 0.0


# --------------- качество когорты (задача 14) — необязательный тормоз

def test_quality_drift_absent_does_not_filter_anybody():
    """Задача 14 ещё не сделана: параметр приходит None каждый такт.

    None обязан значить «тормоза нет», а не «качество упало у всех»: иначе
    список усиления сегодня был бы пуст без единой причины.
    """
    out = growth_candidates(PORTFOLIO, HEADROOM, DEMAND, expansion=[],
                            quality_drift=None)
    assert [c["campaign_id"] for c in out["candidates"]] == ["111"]
    assert out["skipped_by_quality"] == 0


def test_flagged_quality_drop_removes_the_candidate():
    out = growth_candidates(PORTFOLIO, HEADROOM, DEMAND, expansion=[],
                            quality_drift={"111": {"flagged": True, "drop": 0.3}})
    assert out["candidates"] == []
    assert out["skipped_by_quality"] == 1
    # Деньги снятого кандидата не считаются доступными: усиливать его нельзя.
    assert out["room_rub_total"] == 0.0


def test_unflagged_quality_row_leaves_the_candidate_alone():
    out = growth_candidates(PORTFOLIO, HEADROOM, DEMAND, expansion=[],
                            quality_drift={"111": {"flagged": False, "drop": 0.05}})
    assert [c["campaign_id"] for c in out["candidates"]] == ["111"]
    assert out["skipped_by_quality"] == 0
