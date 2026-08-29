# -*- coding: utf-8 -*-
"""
tests/test_agent_ideas_master.py — генератор идей «Мастер кампаний»
(sync/agent/ideas/master.py).

Единственный генератор, чей повод — сама кампания, а не находка внутри неё:
9,63 % расхода EDU (2 390 391 ₽ за окно решений, замер 29.08.2026) идут мимо
витрины настроек, потому что campaigns.get не отдаёт Мастер кампаний. Проверяем
то, что у такого генератора ломается молча и дорого:

  * РЕКОМЕНДАЦИЯ ВМЕСТО ДЕЙСТВИЯ. Записи в Мастер кампаний нет ни у API
    Директа, ни у агента. Идея обязана выйти классом 3 и полосой 7 и не нести
    нагрузки рычага: реестр отвергает предложение с нагрузкой (registry
    _check_action), потому что нагрузка у него означает не «есть чем
    применить», а перепутанный класс;
  * ДЕФЕКТ КОДА, ПЕРЕОДЕТЫЙ В РЕКОМЕНДАЦИЮ. Кампания, которую API ОТДАЁТ, —
    не Мастер, а недоезд до витрины: 25.08.2026 таких оказалось 12 из 15
    «слепых». Предложение по ней увело бы разбор в ручной труд и оставило бы
    баг синка жить;
  * ПРИДУМАННОЕ ОЖИДАНИЕ. Очередь реестра сравнивает ценность на рубль
    проверки (registry.rank), и правдоподобное число у идеи, которая его не
    считала, вынесло бы её вперёд посчитанных. Кампания дешевле кабинетной
    цены оставляет ожидание пустым;
  * ПОРОГ В РУБЛЯХ ВМЕСТО ДОЛИ. Абсолютный порог менял бы смысл вместе с
    размером кабинета — та же болезнь, от которой полосы отказались от общего
    лимита в рублях;
  * АДРЕС С ЧИСЛАМИ. Числа в subject пересчитываются каждым прогоном: войди
    они в отпечаток, идея заводилась бы заново каждое утро — с пустой историей
    и снятым отказом человека.

БД не требуется: реестр подменяется двойником (фикстура store в conftest).
"""

import pytest

from sync.agent.experiments import METRIC
from sync.agent.ideas import master, registry
from sync.agent.master import MASTER_KIND
from sync.agent.writer import lanes, tier

ACCOUNT = "edu-vse"
CAMPAIGN = "705571231"


def _ctx(**over):
    ctx = {"account": ACCOUNT}
    ctx.update(over)
    return ctx


def _card(**over):
    """Карточка, которая ДОЛЖНА дать предложение.

    Числа взяты с прода (замер 29.08.2026, окно решений): Мастер
    «vsekolledzhi_postupi / Общее / МК / МСК» — 1 256 175 ₽ расхода и 623
    эффективных лида, то есть 2 016 ₽ за лид. Тест на отказ ломает ровно одно
    поле — тогда видно, что отказ пришёл именно из-за него, а не из-за
    случайно недостающего.
    """
    row = {
        "account": ACCOUNT,
        "campaign_id": CAMPAIGN,
        "campaign_name": "vsekolledzhi_postupi  / Общее / МК / МСК",
        "direction": "other",
        "cost_rub": 1_256_175.0,
        "clicks": 21_105.0,
        "leads": 691.0,
        "eff_leads": 623.0,
        "revenue_rub": 532_000.0,
        "window_days": 28,
        "account_cost_rub": 12_776_230.0,
        "share_of_account": 0.0983,
        "base_cpl_rub": 1_500.0,
        "api": None,
    }
    row.update(over)
    return row


def test_master_campaign_becomes_a_proposal_for_a_human():
    """Полоса 7, класс 3, нагрузки рычага нет — и реестр это принимает."""
    found = master.scan([_card()], _ctx())

    assert found["skipped"] == []
    idea = found["ideas"][0]
    assert idea["lane"] == lanes.LANE_PROPOSAL
    assert idea["tier"] == tier.TIER_PROPOSAL
    assert "action" not in idea
    assert idea["subject"] == {"kind": MASTER_KIND, "campaign_id": CAMPAIGN}
    assert idea["success_rule"] == {"metric": METRIC, "op": "<=",
                                    "value": 1_500.0,
                                    "comparison": "vs_account"}
    assert idea["detail"]["needs"] == master.NEEDS_HUMAN_HANDS


def test_registry_accepts_the_proposal(store):
    """Реестр принимает идею как есть: класс, полоса и критерий ему понятны.

    Проверка не формальная — она ловит расхождение генератора с реестром,
    из-за которого порция падала бы целиком в бою и молча в тесте генератора.
    """
    saved = registry.upsert(master.candidates([_card()], _ctx()))

    assert len(saved) == 1
    assert saved[0]["action"] is None
    assert saved[0]["source"] == master.SOURCE
    # Идея не снимается как «не окупает проверку»: смету агент не считает —
    # проверку проводит человек в интерфейсе кабинета.
    assert saved[0]["status"] == registry.STATUS_NEW


def test_the_same_campaign_lands_in_the_same_row_tomorrow(store):
    """Отпечаток идеи выведен из адреса, а не из чисел.

    Расход и цена лида пересчитываются каждым прогоном. Войди они в subject —
    назавтра завелась бы вторая строка, с пустой историей и снятым отказом
    человека.
    """
    first = registry.upsert(master.candidates([_card()], _ctx()))
    second = registry.upsert(master.candidates(
        [_card(cost_rub=1_400_000.0, eff_leads=700.0)], _ctx()))

    assert first[0]["idea_id"] == second[0]["idea_id"]
    assert len(store.table) == 1


def test_campaign_the_api_returns_gets_no_proposal():
    """API отдал кампанию — предложения нет, чинить надо синк.

    Замер 25.08.2026: 12 из 15 «слепых» кампаний API отдавал, и списывать их
    на Мастер значило оставить дефект синка жить под видом «зоны, которую
    лечит человек».
    """
    found = master.scan([_card(api={"campaign_type": "TEXT_CAMPAIGN"})], _ctx())

    assert found["ideas"] == []
    assert found["skipped"][0]["reason"] == master.REASON_API_VISIBLE


def test_overpay_is_the_expectation_and_cheap_campaign_has_none():
    """Ожидание — переплата против цены кабинета, растянутая на горизонт.

    1 256 175 ₽ против 623 лидов по 1 500 ₽ — переплата 321 675 ₽ за 28 дней.
    Кампания ДЕШЕВЛЕ кабинетной цены ожидания не получает: выгоду правки из
    этих чисел не вывести, а правдоподобное число вынесло бы идею вперёд
    посчитанных.
    """
    expensive = master.scan([_card()], _ctx())["ideas"][0]
    overpay = 1_256_175.0 - 623 * 1_500.0
    assert expensive["expected_rub"] == pytest.approx(
        overpay / 28 * master.HORIZON_WITH_LEARNING, rel=1e-6)
    assert expensive["test_cost_rub"] is None

    cheap = master.scan([_card(base_cpl_rub=3_000.0)], _ctx())["ideas"][0]
    assert cheap["expected_rub"] is None


def test_zero_effective_leads_puts_the_whole_spend_at_stake():
    """Ноль эффективных лидов — переплатой становится весь расход.

    Это не преувеличение, а ровно то, что случилось: кабинет заплатил и не
    получил ни одного лида, который считается эффективным.
    """
    idea = master.scan([_card(eff_leads=0.0)], _ctx())["ideas"][0]

    assert idea["expected_rub"] == pytest.approx(
        1_256_175.0 / 28 * master.HORIZON_WITH_LEARNING, rel=1e-6)
    assert idea["detail"]["cpl_rub"] is None


def test_small_campaign_is_below_the_share_threshold():
    """Порог — доля кабинета, а не рубли, и он настраивается."""
    small = _card(cost_rub=50_000.0, share_of_account=0.004)

    assert master.scan([small], _ctx())["skipped"][0]["reason"] == \
        master.REASON_SMALL
    # Ручка панели настроек опускает порог — и та же кампания проходит.
    loosened = _ctx(config={master.MIN_SHARE_KEY: 0.001})
    assert len(master.scan([small], loosened)["ideas"]) == 1


def test_account_without_lead_price_gets_no_invented_threshold():
    """Нет цены лида кабинета — нет и критерия успеха.

    Придуманный порог закрыл бы идею по мерке, которой никто не назначал.
    """
    found = master.scan([_card(base_cpl_rub=None)], _ctx())

    assert found["ideas"] == []
    assert found["skipped"][0]["reason"] == master.REASON_NO_PRICE


def test_refusals_are_named_not_silent():
    """Каждая отбракованная карточка возвращается с причиной.

    «Мастера в кабинете нет», «Мастер есть, но мелкий» и «это не Мастер, а
    дефект синка» ведут к трём разным следующим шагам, и по пустому счётчику
    они неразличимы.
    """
    found = master.scan([
        _card(campaign_id="", account=""),
        _card(campaign_id="1", cost_rub=0.0, share_of_account=0.0),
        _card(campaign_id="2", api={"campaign_type": "TEXT_CAMPAIGN"}),
    ], _ctx(account=""))

    assert found["ideas"] == []
    assert [s["reason"] for s in found["skipped"]] == [
        master.REASON_NO_ADDRESS,
        master.REASON_NO_SPEND,
        master.REASON_API_VISIBLE,
    ]
