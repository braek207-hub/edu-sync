# -*- coding: utf-8 -*-
"""
tests/test_agent_ideas_market.py — генератор идей «спрос и рыночные поводы»
(sync/agent/ideas/market.py).

Проверяется здесь то, что у этого генератора ломается молча и дорого:

  * идея, выведенная из отсутствующего ряда. Шесть направлений кабинета фраз
    спроса не имеют вовсе (demand.DIRECTIONS_WITHOUT_SERIES), и «нет ряда» —
    это не «мало данных»: первое лечится семантикой спроса, второе временем.
    Идея из пустого ряда — выдумка с видом вывода;
  * смысловая гипотеза модели, получившая право тратить деньги. Внешних
    источников о конкурентах и кейсах у нас нет ни одного, модель выдаёт
    правдоподобный текст, и класс 2 сделал бы этот текст расходом;
  * гипотеза без устойчивого ключа. Формулировка модели плавает от прогона к
    прогону, и войди она в отпечаток — идея заводилась бы заново каждый день,
    с пустой историей и снятым отказом человека;
  * повод, растворившийся в молчании. «Поводов не нашлось» и «поводы были, но
    у направления нет ряда» ведут к разным следующим шагам.

БД не требуется: реестр подменяется двойником (фикстура store в conftest).
"""

import pytest

from sync.agent import demand
from sync.agent.experiments import HORIZON_DAYS, METRIC
from sync.agent.ideas import market, registry
from sync.agent.writer import lanes, tier
from sync.agent.writer.learning import LEARNING_COOLDOWN_DAYS

ACCOUNT = "edu-vuz"


def _ctx(**over):
    ctx = {"account": ACCOUNT}
    ctx.update(over)
    return ctx


def _demand(**over):
    """Повод спроса, который ДОЛЖЕН дать идею.

    Тест на отказ ломает ровно одно поле — тогда видно, что отказ пришёл
    именно из-за него, а не из-за случайно недостающего.
    """
    row = {
        "kind": market.KIND_DEMAND,
        "direction": "spo",
        "regime": demand.REGIME_RISE,
        "sigma": 2.6,
        "frequency": 48_000,
        "baseline_median": 31_000,
        "last_week": "2026-W34",
        "covered": False,
        "uncovered_phrases": ["колледж заочно", "техникум дистанционно"],
        "direction_cpl_rub": 1_400.0,
    }
    row.update(over)
    return row


def _hypothesis(**over):
    row = {
        "kind": market.KIND_HYPOTHESIS,
        # Устойчивый ключ гипотезы: он, а не текст, входит в адрес идеи.
        "key": "rassrochka-v-zagolovke",
        "statement": "Вынести рассрочку в заголовок объявлений СПО",
        "direction": "spo",
        "success_rule": {"metric": METRIC, "op": "<=", "value": 1_200.0},
        "needs": "замер доли заявок с вопросом о рассрочке",
    }
    row.update(over)
    return row


def _idea(rows=None, ctx=None):
    ideas = market.candidates(rows if rows is not None else [_demand()],
                              ctx or _ctx())
    assert ideas, "повод должен был дать идею"
    return ideas[0]


# ------------------------------------------------------------ спрос


def test_rising_uncovered_demand_becomes_an_idea():
    # Шаг 1 плана беты: растущий спрос, которого мы не покрываем, — повод.
    idea = _idea()
    assert idea["source"] == market.SOURCE
    assert idea["subject"]["direction"] == "spo"


def test_direction_without_a_series_yields_nothing():
    # Шаг 2 плана беты. Шесть направлений остались без ряда фраз, и их
    # вердикт — «нет ряда», а не «мало данных».
    rows = [_demand(regime=demand.REGIME_NO_SERIES, sigma=None,
                    frequency=None, baseline_median=None)]
    assert market.candidates(rows, _ctx()) == []


def test_no_series_is_refused_apart_from_low_data():
    # Две причины разные: «нет ряда» лечится добавлением фраз в семантику
    # спроса, «мало данных» — временем. Один текст на оба спрятал бы дыру в
    # семантике навсегда.
    no_series = market.scan([_demand(regime=demand.REGIME_NO_SERIES)],
                            _ctx())["skipped"]
    low_data = market.scan([_demand(regime=demand.REGIME_LOW_DATA)],
                           _ctx())["skipped"]

    assert no_series and low_data
    assert no_series[0]["reason"] != low_data[0]["reason"]


def test_low_data_verdict_is_not_a_reason_to_act():
    # «Мало данных» — это отсутствие вердикта, а не вердикт «норма».
    rows = [_demand(regime=demand.REGIME_LOW_DATA, sigma=None)]
    assert market.candidates(rows, _ctx()) == []


def test_normal_and_falling_demand_are_not_expansion_reasons():
    # Спад — повод пересмотреть ОЖИДАНИЯ от кампании (demand.py), а не
    # заводить под него новую семантику; норма — не повод вовсе.
    for regime in (demand.REGIME_NORMAL, demand.REGIME_FALL):
        assert market.candidates([_demand(regime=regime, sigma=-2.4)],
                                 _ctx()) == []


def test_covered_demand_is_not_an_idea_for_this_generator():
    # Спрос, который мы уже покрываем, — повод для перелива бюджета
    # (portfolio.py), а не для новой семантики. Заводить под него идею значит
    # предлагать человеку то, что агент и так делает каждым тактом.
    assert market.candidates([_demand(covered=True)], _ctx()) == []


def test_demand_without_uncovered_phrases_is_refused():
    # «Не покрываем» без единой названной фразы — утверждение без предмета:
    # непонятно, что именно заводить.
    rows = [_demand(uncovered_phrases=[])]
    result = market.scan(rows, _ctx())

    assert result["ideas"] == []
    assert any("фраз" in row["reason"] for row in result["skipped"])


def test_demand_idea_carries_the_phrases_it_is_built_on():
    assert (_idea()["detail"]["uncovered_phrases"]
            == ["колледж заочно", "техникум дистанционно"])


def test_demand_idea_carries_the_measured_regime():
    # Основание идеи — замер, и он едет вместе с ней: без сигмы и базы
    # человек на экране видит утверждение «спрос растёт» без доказательства.
    detail = _idea()["detail"]
    assert detail["regime"] == demand.REGIME_RISE
    assert detail["sigma"] == 2.6 and detail["baseline_median"] == 31_000


# --------------------------------------------------- гипотезы модели


def test_llm_hypothesis_is_always_a_proposal():
    # Шаг 3 плана беты. Внешних источников о конкурентах и кейсах у нас нет
    # ни одного: модель выдаёт правдоподобный текст, и класс 2 дал бы этому
    # тексту право тратить деньги.
    for idea in market.candidates([_hypothesis()], _ctx()):
        assert idea["tier"] == tier.TIER_PROPOSAL
        assert idea["lane"] == lanes.LANE_PROPOSAL


def test_proposal_states_what_it_needs_to_become_testable():
    # Шаг 4 плана беты. Предложение без этого — тупик: человек видит идею и
    # не знает, чего ей не хватает, чтобы стать проверяемой.
    idea = market.candidates([_hypothesis()], _ctx())[0]
    assert idea["detail"]["needs"]


def test_hypothesis_without_a_stable_key_is_refused():
    # Формулировка модели плавает от прогона к прогону. Войди она в адрес —
    # идея заводилась бы заново каждый день, с пустой историей и снятым
    # отказом человека.
    result = market.scan([_hypothesis(key="")], _ctx())

    assert result["ideas"] == []
    assert any("ключ" in row["reason"] for row in result["skipped"])


def test_hypothesis_identity_survives_a_reworded_statement():
    first = registry._prepare(market.candidates([_hypothesis()], _ctx())[0])
    reworded = _hypothesis(statement="Рассрочка — в первый заголовок СПО")
    second = registry._prepare(market.candidates([reworded], _ctx())[0])

    assert first["idea_id"] == second["idea_id"]


def test_hypothesis_without_a_checkable_criterion_is_refused():
    # Гипотеза без машинно проверяемого критерия — текст: её нельзя ни
    # закрыть, ни засчитать, и она осталась бы в реестре навсегда.
    result = market.scan([_hypothesis(success_rule=None)], _ctx())

    assert result["ideas"] == []
    assert any("критери" in row["reason"] for row in result["skipped"])


def test_hypothesis_criterion_must_be_machine_checkable():
    vague = _hypothesis(success_rule={"metric": "станет лучше"})
    result = market.scan([vague], _ctx())

    assert result["ideas"] == []
    assert any("критери" in row["reason"] for row in result["skipped"])


def test_hypothesis_needs_is_filled_even_when_the_row_is_silent():
    # Модель о своём дефиците молчит охотнее, чем о своей идее. Молчание не
    # должно превращать предложение в тупик — общий дефицит известен и без
    # неё: внешних источников о рынке у нас нет ни одного.
    idea = market.candidates([_hypothesis(needs=None)], _ctx())[0]
    assert idea["detail"]["needs"]


# ------------------------------------------------------------ форма идеи


def test_demand_idea_is_a_proposal_until_the_builder_order_exists():
    # Рычага у «завести новую семантику» нет: наряд билдеру — задача 17, а
    # campaign.create в allow-листе записи отсутствует. Класс 2 требует
    # нагрузки рычага (registry._check_action), и выдать её сейчас можно было
    # бы только выдумав контракт наряда до того, как он написан.
    idea = _idea()
    assert idea["tier"] == tier.TIER_PROPOSAL
    assert idea["lane"] == lanes.LANE_PROPOSAL
    assert "action" not in idea


def test_demand_idea_names_what_it_needs_to_become_a_bet():
    assert "17" in _idea()["detail"]["needs"] or "наряд" in _idea()["detail"]["needs"]


def test_needs_lives_outside_the_identity():
    # Дефицит идеи меняется: сегодня ей нужен наряд билдеру, завтра он
    # появится. Войди «чего не хватает» в адрес — идея сменила бы
    # идентификатор в день, когда дефицит закрыт, и отказ человека на ней
    # был бы снят молча. Поэтому в detail, а не в subject.
    assert "needs" not in _idea()["subject"]


def test_horizon_covers_learning_of_a_fresh_campaign():
    # Новая семантика едет новой кампанией или группой, а свежая стратегия
    # учится заново. Горизонт, кончающийся раньше, судил бы её по неделям
    # переобучения.
    assert _idea()["horizon_days"] >= HORIZON_DAYS + LEARNING_COOLDOWN_DAYS


def test_success_rule_beats_the_direction_price():
    # Порог не выдуман: это цена, по которой направление покупает лиды
    # СЕЙЧАС. Новая семантика, которая её не побила, не окупила захода.
    rule = _idea()["success_rule"]
    assert rule["metric"] == METRIC and rule["op"] in registry.COMPARISONS
    assert rule["value"] == 1_400.0


def test_demand_without_a_direction_price_is_refused():
    # Критерий не от чего отмерить — а критерий, придуманный реестром за
    # генератора, закрыл бы идею по порогу, которого никто не назначал.
    result = market.scan([_demand(direction_cpl_rub=None)], _ctx())

    assert result["ideas"] == []
    assert any("цен" in row["reason"] for row in result["skipped"])


def test_price_of_the_test_is_not_invented():
    # Объём нового спроса в лидах неизвестен: у фраз, которых в кабинете нет,
    # нет и истории. Смета «на глаз» вынесла бы идею вперёд посчитанных —
    # незнание оказалось бы сильнейшим аргументом очереди (registry.rank).
    idea = _idea()
    assert idea["test_cost_rub"] is None and idea["expected_rub"] is None


def test_unknown_row_kind_is_refused_not_guessed():
    result = market.scan([{"kind": "что-то новое", "direction": "spo"}],
                         _ctx())

    assert result["ideas"] == []
    assert result["skipped"]


def test_order_is_deterministic():
    rows = [_hypothesis(), _demand()]
    first = [i["subject"] for i in market.candidates(rows, _ctx())]
    second = [i["subject"] for i in market.candidates(rows, _ctx())]

    assert first == second


# ------------------------------------------------- проверка у получателя


def test_demand_idea_is_accepted_by_the_registry():
    row = registry._prepare(_idea())
    assert row["source"] == market.SOURCE
    assert row["detail"]["uncovered_phrases"]


def test_hypothesis_is_accepted_by_the_registry():
    row = registry._prepare(market.candidates([_hypothesis()], _ctx())[0])
    assert row["subject"]["key"] == "rassrochka-v-zagolovke"


def test_ideas_survive_a_real_upsert(store):
    rows = registry.upsert(market.candidates([_demand(), _hypothesis()],
                                             _ctx()))
    assert len(rows) == 2
    assert all(r["status"] == registry.STATUS_NEW for r in rows)
    assert len(store.table) == 2
