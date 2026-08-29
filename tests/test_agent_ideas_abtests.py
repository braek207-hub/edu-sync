# -*- coding: utf-8 -*-
"""
tests/test_agent_ideas_abtests.py — генератор идей «A/B-тест по каталогу
типов» (sync/agent/ideas/abtests.py).

Проверяется здесь то, что у генератора тестов ломается молча и дорого:

  * тест, который не померить. Кампания, которой за допустимый срок не
    набрать объёма на сравнение плеч, даёт не эксперимент, а трату с
    отчётом — и вердикт по ней будет вынесен на шуме;
  * сравнение «было/стало» на одной кампании. Смену цели или стратегии так
    судить нельзя: эффект неотличим от сезона и переобучения. База сравнения
    обязана быть названа в самой идее, иначе её выберут задним числом — ту,
    что даст нужный ответ;
  * тест, сбрасывающий обучение, поверх свежего сброса. Он измерит
    переобучение, а не себя;
  * кампания заповедника. Она — линейка, которой меряют всё остальное;
    тронув её, агент теряет базу сравнения для всего кабинета;
  * каталог, разъехавшийся с рычагами. Тип теста, чей рычаг ещё не написан,
    предлагать нельзя: идея встанет в реестр и будет отвергаться каждым
    тактом.

БД не требуется: реестр подменяется двойником (фикстура store в conftest).
"""

import pytest

from sync.agent import power
from sync.agent.experiments import HORIZON_DAYS
from sync.agent.ideas import abtests, limits, registry
from sync.agent.writer import guardrails, lanes, tier
from sync.agent.writer.learning import LEARNING_COOLDOWN_DAYS

ACCOUNT = "edu-vuz"
CAMPAIGN = "555"

# Темп, которого хватает на сравнение плеч за горизонт ставки: порог
# power.AB_MIN_EFF_LEADS делится на срок замера, а не назначается заново.
FAST_DAILY_LEADS = power.AB_MIN_EFF_LEADS / float(HORIZON_DAYS)


def _ctx(**over):
    ctx = {"account": ACCOUNT, "holdout_ids": {"999"}}
    ctx.update(over)
    return ctx


def _campaign(**over):
    """Кампания, которая ДОЛЖНА получить тесты.

    Тест на отказ ломает ровно одно поле — тогда видно, что отказ пришёл
    именно из-за него, а не из-за случайно недостающего.
    """
    row = {
        "campaign_id": CAMPAIGN,
        "direction": "vpo",
        "eff_leads": FAST_DAILY_LEADS * 28.0,
        "cost_rub": 560_000.0,
        "window_days": 28,
        "in_holdout": False,
        # Связывает ли лимит расход. Три значения: ключа нет — состояние
        # кабинета не снято, False — не связывает, True — связывает.
        "limit_binds": True,
        # Сколько дней назад обучение стратегии сбрасывалось. Плановый
        # черновик звал поле days_since_budget_change; сбрасывает обучение не
        # только бюджет (writer/learning.py), и поле названо по смыслу.
        "days_since_learning_reset": 60,
    }
    row.update(over)
    return row


def _kinds(rows=None, ctx=None):
    return {i["subject"]["test_kind"]
            for i in abtests.candidates(rows or [_campaign()], ctx or _ctx())}


def _idea(test_kind=None, rows=None, ctx=None):
    ideas = abtests.candidates(rows or [_campaign()], ctx or _ctx())
    assert ideas, "кампания должна была получить тесты"
    if test_kind is None:
        return ideas[0]
    picked = [i for i in ideas if i["subject"]["test_kind"] == test_kind]
    assert picked, "тест " + test_kind + " не предложен"
    return picked[0]


# ------------------------------------------------------------- измеримость


def test_unmeasurable_test_is_not_offered():
    # Шаг 1 плана беты. 0.2 лида в день — 2.8 лида за горизонт ставки: на
    # таком объёме вердикт выносится на шуме.
    slow = _campaign(eff_leads=0.2 * 28.0)
    assert abtests.candidates([slow], _ctx()) == []


def test_unmeasurable_campaign_is_refused_with_a_named_reason():
    # Молчащий отказ неотличим от «поводов не нашлось», и первый же вопрос
    # «почему тестов нет» превращается в археологию по коду.
    slow = _campaign(eff_leads=0.2 * 28.0)
    skipped = abtests.scan([slow], _ctx())["skipped"]

    assert skipped and all(row["reason"] for row in skipped)
    assert any("плеч" in row["reason"] for row in skipped)


def test_volume_threshold_is_the_one_from_power_module():
    # Ровно на пороге — проходит, вдвое ниже — нет. Порог не переписан здесь:
    # он читается из power.py, и разъехаться копиям негде.
    exact = _campaign(eff_leads=FAST_DAILY_LEADS * 28.0)
    thin = _campaign(eff_leads=0.2 * 28.0)

    assert abtests.candidates([exact], _ctx())
    assert abtests.candidates([thin], _ctx()) == []


def test_horizon_of_a_slower_campaign_is_longer_not_denied():
    # Кампания медленнее эталона всё ещё измерима — ей просто нужен больший
    # срок. Отказ вместо срока стоил бы агенту всех тестов на кампаниях
    # среднего размера.
    slower = _campaign(eff_leads=FAST_DAILY_LEADS * 28.0 * 0.6)
    ideas = abtests.candidates([slower], _ctx())

    assert ideas
    assert ideas[0]["horizon_days"] > HORIZON_DAYS


def test_horizon_limit_is_a_setting_not_a_constant():
    # Предел терпения — вопрос сезона и кабинета, а не арифметики: ручка, а
    # не константа в коде. Пара показывает, что отказ пришёл именно из-за
    # предела, а не по другой причине.
    slow = _campaign(eff_leads=FAST_DAILY_LEADS * 28.0 * 0.1)

    assert abtests.candidates([slow], _ctx()) == []
    assert abtests.candidates(
        [slow], _ctx(config={limits.MAX_HORIZON_KEY: 400}))


# ------------------------------------------------------------ база сравнения


def test_every_test_names_its_comparison_base():
    # Шаг 3 плана беты. Смену цели и стратегии нельзя судить как «было/стало»
    # на одной кампании: эффект конфаундится сезоном и переобучением.
    for idea in abtests.candidates([_campaign()], _ctx()):
        assert idea["success_rule"]["comparison"] in abtests.COMPARISON_BASES


def test_holdout_gives_the_did_comparison():
    assert (_idea()["success_rule"]["comparison"]
            == abtests.COMPARISON_DID_VS_HOLDOUT)


def test_without_holdout_a_paired_campaign_is_the_base():
    # Заповедника у кабинета может не быть (задача 25 его только формирует).
    # Пара того же направления — законная вторая база.
    row = _campaign(pair_campaign_id="777")
    idea = _idea(rows=[row], ctx=_ctx(holdout_ids=set()))

    assert idea["success_rule"]["comparison"] == abtests.COMPARISON_PAIRED
    assert idea["detail"]["comparison_object_id"] == "777"


def test_without_any_base_no_test_is_offered():
    # Ни заповедника, ни пары — сравнивать не с чем, и «было/стало» здесь
    # запрещено. Тест без базы сравнения даст цифру, но не вывод.
    result = abtests.scan([_campaign()], _ctx(holdout_ids=set()))

    assert result["ideas"] == []
    assert any("сравн" in row["reason"] for row in result["skipped"])


def test_before_after_is_never_a_comparison_base():
    assert "before_after" not in abtests.COMPARISON_BASES


# ---------------------------------------------------------------- кулдаун


def test_learning_resetting_test_respects_cooldown():
    # Шаг 2 плана беты.
    fresh = _campaign(days_since_learning_reset=3)
    ideas = abtests.candidates([fresh], _ctx())

    assert ideas, "не сбрасывающие обучение тесты кулдаун не запирает"
    assert all(not i["subject"]["resets_learning"] for i in ideas)


def test_cooldown_window_is_the_learning_one():
    # Окно не своё: тот же кулдаун, что у денежных ручек и у обучения
    # стратегии. Две копии одного порога разъехались бы при первой правке.
    just_out = _campaign(days_since_learning_reset=LEARNING_COOLDOWN_DAYS)
    assert any(i["subject"]["resets_learning"]
               for i in abtests.candidates([just_out], _ctx()))


def test_unknown_reset_date_blocks_resetting_tests():
    # «Не знаем, когда сбрасывали» — не то же самое, что «давно». Подмена
    # первого вторым предложила бы сбрасывающий тест поверх свежего сброса.
    unknown = _campaign(days_since_learning_reset=None)
    assert all(not i["subject"]["resets_learning"]
               for i in abtests.candidates([unknown], _ctx()))


def test_resetting_class_comes_from_the_learning_module():
    # Класс не объявлен в каталоге руками: он спрошен у writer/learning.py по
    # рычагу. Иначе таблица типов и правило кабинета разъедутся молча.
    kinds = {i["subject"]["test_kind"]: i["subject"]["resets_learning"]
             for i in abtests.candidates([_campaign()], _ctx())}

    assert kinds["tcpa"] is True
    assert kinds["placements"] is False
    # Расписание в таблице типов плана стоит как несбрасывающее, а
    # writer/learning.py считает его «unknown» и потому сбрасывающим:
    # временного таргетинга в списке справки нет, но объём показов он меняет,
    # и записать его в безопасные значило бы выдать незнание за знание.
    # Правда — в модуле рычагов, а не в черновике плана.
    assert kinds["schedule"] is True


def test_budget_increase_needs_a_binding_limit():
    # Замер августа 2026: лимит связывал расход у 9 кампаний из 62. Прибавка
    # остальным не меняет ничего — и рычаг её потом отвергнет
    # (budget.NOT_APPLICABLE_UP_REASON). Тест, который рычаг отвергнет, —
    # обещание проверки, которой не будет.
    loose = _campaign(limit_binds=False)
    assert "budget_up" not in _kinds([loose])
    assert "budget_down" in _kinds([loose])


def test_unknown_limit_state_does_not_pass_for_binding():
    # «Не знаем, связывает ли» — не то же самое, что «связывает»: подмена
    # предложила бы прибавку вслепую.
    unknown = _campaign()
    unknown.pop("limit_binds")
    assert "budget_up" not in _kinds([unknown])


def test_budget_test_step_stays_within_the_safe_delta():
    # Шаг бюджетного теста — ровно тот, что стратегию не сбивает. Больше — и
    # тест мерил бы переобучение вместо себя.
    idea = _idea("budget_up")
    assert idea["subject"]["resets_learning"] is False
    assert idea["detail"]["change"]["step"] <= abtests.BUDGET_STEP


# ------------------------------------------------------------- заповедник


def test_holdout_campaign_gets_no_tests():
    # Шаг 4 плана беты. Заповедник — линейка, которой меряют всё остальное.
    assert abtests.candidates([_campaign(in_holdout=True)], _ctx()) == []


def test_campaign_listed_in_holdout_ids_gets_no_tests():
    # Заповедник задаётся кабинетом (db.load_holdout_ids), а не полем строки:
    # доверять только флагу значило бы тестировать заповедник при первом же
    # пропуске поля в сборке связок.
    assert abtests.candidates([_campaign()],
                              _ctx(holdout_ids={CAMPAIGN})) == []


# -------------------------------------------------------------- каталог


def test_catalogue_offers_only_released_levers():
    # Из задач 21–24 не написан рычаг аудиторий (23). Идея с ненаписанным
    # рычагом встала бы в реестр и отвергалась каждым тактом.
    for idea in abtests.candidates([_campaign()], _ctx()):
        lever = idea["detail"]["change"]["lever"]
        assert lever in guardrails.ALLOWED_ACTION_KINDS


def test_catalogue_keeps_the_unreleased_kinds_listed():
    # Каталог закрытый и полный: тип, чей рычаг ещё не написан, из него не
    # выкидывается — иначе при появлении рычага о нём никто не вспомнит.
    assert {"goal", "strategy", "audience", "geo"} <= set(abtests.CATALOGUE)


def test_unreleased_kind_is_skipped_with_a_named_reason():
    skipped = abtests.scan([_campaign()], _ctx())["skipped"]
    kinds = {row.get("test_kind") for row in skipped}

    # Рычаги задач 21–24 написаны все четыре, и в отбраковке «рычага нет»
    # остался один тип — креативы: тексты и объявления собирает ДРУГОЙ
    # репозиторий, и рычага записи у агента для них не будет вовсе. Важно, что
    # отбраковка именована: тип, выпавший молча, неотличим от забытого.
    assert "creatives" in kinds
    assert not ({"goal", "strategy", "audience", "geo"} & kinds)
    assert all(row["reason"] for row in skipped)


def test_the_goal_test_became_a_candidate_with_its_lever():
    # Каталог обещал тесты смены цели, стратегии, аудиторий и географии с
    # самого начала, но до задач 21–24 обещать их было нечем. Появился рычаг —
    # тип обязан выйти из отбраковки в кандидаты; иначе каталог остался бы
    # списком намерений.
    assert {"goal", "strategy", "audience", "geo"} <= _kinds()


def test_creative_test_waits_for_the_builder_order():
    # Тексты и креативы — наряд билдеру (Ф14), а не запись в API. Рычага у
    # него нет вовсе, и обещать его видом действия нельзя.
    assert abtests.CATALOGUE["creatives"]["lever"] is None
    assert "creatives" not in _kinds()


# ------------------------------------------------------------- приоритет


def test_learning_reset_cost_lowers_priority():
    # Шаг 5 плана беты: дешёвый тест идёт первым. Цена переобучения — это
    # недели, в которые кабинет работает хуже, и она не в смете, а в порядке.
    assert _idea()["subject"]["resets_learning"] is False


def test_order_is_deterministic():
    # Генератор детерминирован, и очередь обязана быть детерминирована вместе
    # с ним: иначе на одних данных человек видит разный экран.
    first = [i["subject"]["test_kind"]
             for i in abtests.candidates([_campaign()], _ctx())]
    second = [i["subject"]["test_kind"]
              for i in abtests.candidates([_campaign()], _ctx())]

    assert first == second


# ---------------------------------------------------------- форма идеи


def test_idea_is_a_proposal_until_the_payload_can_be_built():
    # Нагрузку теста (лимит, цель CPA, расписание) строит ТОЛЬКО такт записи
    # от живого состояния кабинета: writer/budget.diff_budget требует
    # fetch_budget_state. Расчётный такт собрать её не может, не соврав о
    # текущем состоянии, — значит идея едет человеку, а не в кабинет.
    idea = _idea()
    assert idea["tier"] == tier.TIER_PROPOSAL
    assert idea["lane"] == lanes.LANE_PROPOSAL
    assert "action" not in idea


def test_idea_names_the_lever_and_the_direction_of_the_change():
    change = _idea("budget_up")["detail"]["change"]
    assert change["lever"] and change["direction"] == "up"


def test_subject_addresses_the_campaign_and_the_test_kind():
    subject = _idea("tcpa")["subject"]
    assert subject["campaign_id"] == CAMPAIGN
    assert subject["test_kind"] == "tcpa"


def test_subject_does_not_carry_floating_numbers():
    # В адрес входит только то, что не плавает: смета и срок считаются каждым
    # прогоном, и войди они в отпечаток — идея заводилась бы заново каждый
    # день, с пустой историей и снятым отказом человека.
    subject = _idea()["subject"]
    assert not {"horizon_days", "test_cost_rub", "expected_rub"} & set(subject)


def test_same_test_keeps_its_identity_between_runs():
    first = registry._prepare(_idea())
    slower = _campaign(eff_leads=FAST_DAILY_LEADS * 28.0 * 0.8)
    second = registry._prepare(_idea(rows=[slower]))

    assert first["idea_id"] == second["idea_id"]
    assert first["horizon_days"] != second["horizon_days"]


def test_volume_at_risk_is_told_but_is_not_the_price_of_the_check():
    # Под ударом теста — весь расход кампании за срок замера, а не дельта
    # рычага: проигравший тест портит кампанию целиком на всё это время. Но
    # СМЕТОЙ реестра это число не является: смета — списание риск-бюджета
    # полосы, а предложение риск-бюджетом не платит вовсе. Стояло оно в
    # колонке сметы — и экран предложений показывал 108 946 589 ₽ заявленной
    # цены проверки при нулевой заявленной выгоде (замер 29.08.2026, 113 живых
    # идей abtest). Теперь объём под ударом виден человеку в detail, а колонка
    # сметы пуста рядом с пустым ожиданием — обе величины или ни одной.
    idea = _idea()
    daily = 560_000.0 / 28.0

    assert idea["expected_rub"] is None
    assert idea["test_cost_rub"] is None
    assert idea["detail"]["campaign_cost_at_risk_rub"] == pytest.approx(
        daily * idea["horizon_days"])


def test_test_without_an_expectation_is_accepted_by_the_registry():
    # Прямая проверка того, ради чего смета убрана: контракт реестра
    # (ideas/limits.unpaired_reason) отвергает строку со сметой при
    # непосчитанном ожидании, и порция генератора падала бы целиком — 113
    # находок за такт вместо экрана предложений.
    assert registry._prepare(_idea())["test_cost_rub"] is None


def test_success_rule_is_machine_checkable():
    rule = _idea()["success_rule"]
    assert rule["metric"] and rule["op"] in registry.COMPARISONS
    assert rule["value"] > 0


def test_success_rule_measures_the_metric_of_the_horizon():
    # Метрика теста — та же, которой сторож судит ставки (experiments.METRIC):
    # оплаты за две недели не дозревают, и судить тест по ним значило бы
    # закрывать его недозревшей когортой.
    assert _idea()["success_rule"]["metric"] == abtests.METRIC


# ------------------------------------------------- проверка у получателя


def test_idea_is_accepted_by_the_registry():
    row = registry._prepare(_idea())
    assert row["source"] == abtests.SOURCE
    assert row["detail"]["change"]["lever"]


def test_idea_survives_a_real_upsert(store):
    rows = registry.upsert(abtests.candidates([_campaign()], _ctx()))
    assert rows and all(r["status"] == registry.STATUS_NEW for r in rows)
    assert len(store.table) == len(rows)
