# -*- coding: utf-8 -*-
"""
tests/test_agent_ideas_consolidate.py — генератор идей «вынос доказанных
связок в отдельную кампанию» (sync/agent/ideas/consolidate.py).

Проверяется здесь то, что у этого генератора ломается молча и дорого:

  * идея выноса без кросс-минусовки доноров. Она выглядит нормальной ровно до
    запуска: две наши кампании начинают торговаться друг с другом на одном
    аукционе, и вынос вместо экономии даёт удорожание;
  * связки двух направлений в одной кампании — вердикта не будет ни по
    одному, а деньги потрачены;
  * вынос, которому не набрать объёма на вердикт: это не эксперимент, а
    трата с отчётом;
  * idea_id, зависящий от состава доноров. Состав плавает каждый прогон, и
    такая идея заводилась бы заново каждый день — с пустой историей и снятым
    отказом человека;
  * идея не проходит РЕЕСТР. Поля по отдельности выглядят правильными, тесты
    зелёные, а registry._prepare отвергает порцию целиком. Поэтому идея
    гоняется и через _prepare, и через upsert с подменённым доступом к базе.

БД не требуется: реестр подменяется двойником (конвенция
tests/test_agent_ideas_registry.py).
"""

import pytest

from sync.agent import power
from sync.agent.db import CRM_MATURITY_WINDOW_DAYS
from sync.agent.experiments import HORIZON_DAYS as STAKE_HORIZON_DAYS
from sync.agent.ideas import consolidate, registry
from sync.agent.portfolio import GROWTH_LAMBDA_MARGIN
from sync.agent.writer import lanes, negatives, tier

# λ кабинета — фикстура, а не порог: сам порог (запас ×GROWTH_LAMBDA_MARGIN)
# берётся из portfolio.py.
LAMBDA = 0.71
ACCOUNT = "edu-vuz"


def _ctx(**over):
    ctx = {"account": ACCOUNT, "lambda": LAMBDA,
           "value_per_payment_rub": 40_000.0}
    ctx.update(over)
    return ctx


def _q(phrase="колледж заочно", campaign="111", direction="spo",
       p_pay=12.0, **over):
    """Связка-донор, которая ДОЛЖНА пройти отбор.

    Тест на отказ ломает ровно одно поле — тогда видно, что отказ пришёл
    именно из-за него, а не из-за случайно недостающего.

    p_pay по умолчанию таков, что двух связок хватает на порог значимости
    power.MIN_EXPECTED_PAYMENTS за окно наблюдения.
    """
    row = {
        "phrase": phrase,
        "campaign_id": campaign,
        "direction": direction,
        "romi": 2.6,
        "cost_rub": 30_000.0,
        "conversions": 30.0,
        "p_pay_sum": p_pay,
        "window_days": 28,
    }
    row.update(over)
    return row


def _pair():
    """Две связки одного направления из РАЗНЫХ кампаний-доноров."""
    return [_q(phrase="колледж заочно", campaign="111"),
            _q(phrase="поступить в колледж", campaign="222")]


def _idea(rows=None, ctx=None):
    ideas = consolidate.candidates(rows or _pair(), ctx or _ctx())
    assert ideas, "связки должны были дать идею выноса"
    return ideas[0]


# ------------------------------------------------------- кросс-минусовка


def test_consolidation_idea_carries_donor_negatives():
    # Шаг 1 плана беты. Без минусовки у доноров вынос — не тест, а
    # гарантированное удорожание.
    assert _idea()["detail"]["cannibalization"]["donor_negatives"]


def test_donor_negatives_name_every_donor_campaign():
    plan = _idea()["detail"]["cannibalization"]["donor_negatives"]
    assert {row["campaign_id"] for row in plan} == {"111", "222"}


def test_donor_negatives_carry_the_phrases_being_moved():
    plan = _idea()["detail"]["cannibalization"]["donor_negatives"]
    moved = {phrase for row in plan for phrase in row["phrases"]}
    assert moved == {"колледж заочно", "поступить в колледж"}


def test_phrase_that_cannot_be_minused_is_refused_not_moved():
    # Фраза с операторами языка запросов рычагом минусовки не ставится
    # (negatives.phrase_is_valid). Вынести её значило бы оставить донора
    # торговаться против новой кампании навсегда.
    rows = _pair() + [_q(phrase='"кавычки в фразе"', campaign="333")]
    result = consolidate.scan(rows, _ctx())

    moved = {q["phrase"] for q in result["ideas"][0]["detail"]["queries"]}
    assert '"кавычки в фразе"' not in moved
    assert any("кросс-минусовк" in row["reason"] for row in result["skipped"])


def test_cannibalization_names_the_lever_it_will_use():
    # План минусовки без названного рычага — обещание разобраться потом.
    assert (_idea()["detail"]["cannibalization"]["lever"]
            == negatives.NEGATIVE_KIND)


# --------------------------------------------------------- направления


def test_consolidation_does_not_mix_directions():
    # Шаг 2 плана беты.
    rows = _pair() + [_q(phrase="вуз заочно", campaign="333", direction="vpo"),
                      _q(phrase="высшее дистанционно", campaign="444",
                         direction="vpo")]
    ideas = consolidate.candidates(rows, _ctx())

    assert len(ideas) == 2
    for idea in ideas:
        directions = {idea["subject"]["direction"]}
        assert len(directions) == 1


def test_each_direction_gets_its_own_address():
    rows = _pair() + [_q(phrase="вуз заочно", campaign="333", direction="vpo"),
                      _q(phrase="высшее дистанционно", campaign="444",
                         direction="vpo")]
    ideas = consolidate.candidates(rows, _ctx())

    assert {i["subject"]["direction"] for i in ideas} == {"spo", "vpo"}
    assert len({registry.idea_id(i["source"], i["subject"], i["account"])
                for i in ideas}) == 2


# --------------------------------------------------------------- объём


def test_below_power_threshold_no_consolidation():
    # Шаг 3 плана беты. Порог — power.MIN_EXPECTED_PAYMENTS, не своё число.
    thin = [_q(phrase="колледж заочно", campaign="111", p_pay=0.05),
            _q(phrase="поступить в колледж", campaign="222", p_pay=0.05)]
    assert consolidate.candidates(thin, _ctx()) == []


def test_thin_direction_is_refused_with_a_named_reason():
    thin = [_q(p_pay=0.05), _q(phrase="поступить в колледж", campaign="222",
                               p_pay=0.05)]
    result = consolidate.scan(thin, _ctx())

    assert result["ideas"] == []
    assert any("не набрать" in row["reason"] for row in result["skipped"])


def test_power_threshold_is_the_one_from_power_module():
    # Ровно на пороге объём проходит: генератор не занижает и не завышает
    # чужой порог собственным запасом.
    window = 28
    per_query = power.MIN_EXPECTED_PAYMENTS / 2.0
    rows = [_q(campaign="111", p_pay=per_query, window_days=window),
            _q(phrase="поступить в колледж", campaign="222",
               p_pay=per_query, window_days=window)]
    assert consolidate.candidates(rows, _ctx())


def test_slow_accumulation_is_not_worth_a_quarter():
    # Эксперимент, которому на вердикт нужен больше чем предел, соревнуется
    # уже не с донорами, а с сезоном.
    slow = [_q(p_pay=0.6), _q(phrase="поступить в колледж", campaign="222",
                              p_pay=0.6)]
    assert consolidate.candidates(slow, _ctx()) == []


def test_horizon_limit_is_a_setting_not_a_constant():
    # Предел терпения — вопрос сезона и кабинета, а не арифметики: у одного
    # направления квартал ожидания бессмыслен, у другого нормален. Значит
    # ручка, а не константа в коде.
    #
    # Связки подобраны так, что вердикт набирается за ~200 дней: при пределе
    # по умолчанию идея отвергается, при поднятом — проходит. Отвергается
    # ИМЕННО из-за предела, а не по другой причине, — это и показывает пара.
    slow = [_q(p_pay=1.75), _q(phrase="поступить в колледж", campaign="222",
                               p_pay=1.75)]

    assert consolidate.candidates(slow, _ctx()) == []
    assert consolidate.candidates(
        slow, _ctx(config={consolidate.MAX_HORIZON_KEY: 400}))


# ---------------------------------------------------------- срок и цена


def test_consolidation_states_budget_and_horizon():
    # Шаг 4 плана беты.
    idea = _idea()
    assert idea["test_cost_rub"] > 0 and idea["horizon_days"] >= 21


def test_horizon_covers_both_accumulation_and_crm_maturity():
    # Горизонт, кончающийся в день последнего клика, судил бы кампанию по
    # недозревшей когорте: оплата приходит позже лида.
    idea = _idea()
    assert idea["horizon_days"] >= STAKE_HORIZON_DAYS + CRM_MATURITY_WINDOW_DAYS


def test_test_cost_is_the_money_the_campaign_will_live_on():
    # Цена теста — дневной темп доноров, растянутый на горизонт.
    idea = _idea()
    daily = (30_000.0 + 30_000.0) / 28.0
    assert idea["test_cost_rub"] == pytest.approx(daily * idea["horizon_days"],
                                                  rel=1e-6)


def test_direction_without_a_price_of_a_payment_is_not_proposed_at_all():
    # Раньше идея заводилась с пустым ожиданием: ноль был бы выдумкой, и
    # оставляли None. Но смета выноса настоящая — это деньги, которые новая
    # кампания проживёт за горизонт, — и строка выходила сметой без выгоды,
    # чего реестр теперь не принимает (ideas/limits.unpaired_reason): порция
    # генератора падала бы целиком. Отсев с названной причиной: «кабинету
    # нечем оценить оплату» и «вынос ничего не обещает» лечатся по-разному.
    ctx = _ctx()
    ctx.pop("value_per_payment_rub")

    found = consolidate.scan(_pair(), ctx)

    assert found["ideas"] == []
    assert [r["reason"] for r in found["skipped"]] == [
        consolidate.GROUP_REASON_NO_VALUE_PER_PAYMENT]


# -------------------------------------------------------- окупаемость


def test_barely_above_lambda_is_not_worth_a_separate_campaign():
    thin = [_q(romi=LAMBDA * 1.01), _q(phrase="поступить в колледж",
                                       campaign="222", romi=LAMBDA * 1.01)]
    assert consolidate.candidates(thin, _ctx()) == []


def test_margin_threshold_comes_from_portfolio():
    exact = LAMBDA * GROWTH_LAMBDA_MARGIN
    rows = [_q(romi=exact), _q(phrase="поступить в колледж", campaign="222",
                               romi=exact)]
    assert consolidate.candidates(rows, _ctx())


def test_lambda_absent_means_silence_not_a_default():
    ctx = _ctx()
    ctx.pop("lambda")
    result = consolidate.scan(_pair(), ctx)

    assert result["ideas"] == []
    assert all("λ" in row["reason"] for row in result["skipped"])


def test_payments_are_not_substituted_by_conversions():
    # Порог значимости считает ожидаемые оплаты. Подставить сюда конверсии
    # значило бы мерить другое и вынести кампанию под вердикт, которого
    # порогу не хватает.
    rows = [dict(_q(), p_pay_sum=None), dict(_q(campaign="222"),
                                             p_pay_sum=None)]
    result = consolidate.scan(rows, _ctx())

    assert result["ideas"] == []
    assert all("p_pay" in row["reason"] for row in result["skipped"])


# ------------------------------------------------------------- адрес


def test_subject_carries_no_donor_list():
    # Состав доноров плавает каждым прогоном. Войди он в адрес — idea_id
    # менялся бы каждый день: пустая история и снятый отказ человека.
    subject = _idea()["subject"]
    assert set(subject) == {"kind", "direction"}


def test_identity_survives_a_changed_donor_set():
    one = _idea()
    two = _idea(rows=_pair() + [_q(phrase="колледж после 9", campaign="333")])

    assert (registry.idea_id(one["source"], one["subject"], one["account"])
            == registry.idea_id(two["source"], two["subject"], two["account"]))


def test_evidence_follows_the_donor_set():
    # И обратное: доказательства обязаны догонять состав, иначе человек
    # принимает сегодняшнее решение по вчерашнему обоснованию.
    two = _idea(rows=_pair() + [_q(phrase="колледж после 9", campaign="333")])
    assert len(two["detail"]["queries"]) == 3


# ------------------------------------------------------------- класс


def test_consolidation_without_donor_settings_stays_a_proposal():
    # Счётчик и цель новой кампании берутся у доноров. Не прочитаны — наряд
    # не собирается, и вынос остаётся предложением человеку. Молча уйти в
    # ставку он не вправе: применять было бы нечем.
    idea = _idea()
    assert idea["tier"] == tier.TIER_PROPOSAL
    assert idea["lane"] == lanes.LANE_PROPOSAL
    assert "action" not in idea
    # Причина названа, а не подразумевается: на экране «генератор молчит» и
    # «генератор нашёл, но не хватило настроек донора» — разные новости.
    assert idea["detail"]["launch_refusal"]


def _settings(goal=360_811_375, counter=98_627_983):
    return {"TextCampaign": {
        "CounterIds": {"Items": [counter]},
        "BiddingStrategy": {"Search": {"AverageCpa": {
            "GoalId": goal, "AverageCpa": 1_600_000_000}}}}}


def test_consolidation_with_donor_settings_carries_an_order():
    # Ф14: рычаг у выноса появился — наряд билдеру. Идея перестаёт быть
    # предложением и несёт нагрузку в колонке action: такт записи читает
    # идеи из базы, и всё, чего нет в колонке, для него не существует.
    idea = _idea([_q(phrase="колледж заочно", campaign="111",
                     settings=_settings()),
                  _q(phrase="поступить в колледж", campaign="222",
                     settings=_settings())])
    assert idea["tier"] == tier.TIER_BET
    assert idea["lane"] == lanes.LANE_LAUNCH
    order = idea["action"]["payload"]["order"]
    assert order["idea_id"] == idea["idea_id"]
    assert order["campaign"]["goal_id"] == 360_811_375
    assert {n["campaign_id"] for n in order["donor_negatives"]} == {"111", "222"}


def test_donors_disagreeing_on_the_goal_keep_the_idea_a_proposal():
    # Два смысла конверсии в одной кампании невозможны, и усреднять их
    # нельзя. Находка при этом не теряется: человек видит её и причину.
    idea = _idea([_q(phrase="колледж заочно", campaign="111",
                     settings=_settings()),
                  _q(phrase="поступить в колледж", campaign="222",
                     settings=_settings(goal=111_222_333))])
    assert idea["tier"] == tier.TIER_PROPOSAL
    assert "цел" in idea["detail"]["launch_refusal"]


def test_the_order_of_an_idea_is_the_same_from_run_to_run():
    # Заливка ищет кампанию по имени, а имя несёт order_id. Плавай он от
    # прогона к прогону — каждый такт заводил бы новую кампанию на ту же
    # идею, а прежняя продолжала бы тратить.
    rows = [_q(phrase="колледж заочно", campaign="111", settings=_settings()),
            _q(phrase="поступить в колледж", campaign="222", settings=_settings())]
    first = _idea(rows)["action"]
    second = _idea(rows)["action"]
    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["payload"]["CampaignName"] == second["payload"]["CampaignName"]


def test_a_created_campaign_is_ordered_paused():
    idea = _idea([_q(phrase="колледж заочно", campaign="111",
                     settings=_settings()),
                  _q(phrase="поступить в колледж", campaign="222",
                     settings=_settings())])
    assert idea["action"]["payload"]["state"] == "SUSPENDED"


# ------------------------------------------------- проверка у получателя


def test_idea_is_accepted_by_the_registry():
    row = registry._prepare(_idea())
    assert row["source"] == consolidate.SOURCE
    assert row["detail"]["cannibalization"]["donor_negatives"]


def test_idea_survives_a_real_upsert(store):
    rows = registry.upsert([_idea()])
    assert len(rows) == 1 and rows[0]["status"] == registry.STATUS_NEW
    assert len(store.table) == 1


def test_success_rule_is_machine_checkable():
    rule = _idea()["success_rule"]
    assert rule["metric"] and rule["op"] and rule["value"] > 0


def test_success_rule_beats_the_donors_own_price():
    # Вынос, не улучшивший донорскую цену конверсии, не окупил переезда.
    idea = _idea()
    donor_cpa = (30_000.0 + 30_000.0) / (30.0 + 30.0)
    assert idea["success_rule"]["value"] == pytest.approx(donor_cpa)
    assert idea["success_rule"]["comparison"] == "vs_donors"


# ------------------------------- окупаемость: связка молчит, группа судит


# Пул направления: коэффициенты перехода, которыми лестница пересчитывает
# клики в оплаты. Числители у всех пар выше MIN_RATE_EVENTS — иначе отказ
# пришёл бы из-за слабого пула, а не из-за проверяемого условия.
POOL = ("direction:spo", {"clicks": 100_000, "leads": 5_000, "eff": 2_000,
                          "connected": 1_200, "deals": 400, "paid": 200})
AVG_CHECK = 40_000.0


def _mute(phrase="колледж заочно", campaign="111", clicks=4_000, **over):
    """Связка, которой ЛЕСТНИЦА вердикт не дала: p_pay_sum и romi пусты.

    Так выглядит подавляющее большинство связок в проде: порог лестницы —
    25 событий за окно, а «кампания × фраза» столько кликов в одиночку не
    набирает (замер 28.08: 61 строка из 37 318). Свои события при этом у
    связки есть, и группа обязана считать по ним.
    """
    row = _q(phrase=phrase, campaign=campaign, **over)
    row.update({"p_pay_sum": None, "romi": None,
                "counts": {"clicks": clicks}, "pools": (POOL,),
                "avg_check": AVG_CHECK})
    return row


def _mute_pair():
    return [_mute(phrase="колледж заочно", campaign="111"),
            _mute(phrase="поступить в колледж", campaign="222")]


def test_query_the_ladder_refused_to_judge_is_not_dropped():
    # Прежняя версия отвергала такую связку по «окупаемость не посчитана», и
    # до группировки не доживал ни один донор из семнадцати. Отказ лестницы
    # судить связку — не приговор связке: он про объём события, а не про её
    # качество.
    result = consolidate.scan(_mute_pair(), _ctx())
    assert result["ideas"], result["skipped"]


def test_group_economics_sums_events_of_the_donor_campaign():
    # Клики двух связок ОДНОЙ кампании складываются и один раз проходят
    # лестницу: коэффициенты перехода и средний чек — свойство кампании, и
    # прогонять по ним чужие события нельзя.
    payments, revenue, refusal = consolidate._economics(
        [consolidate._one(row, _ctx())[0]
         for row in (_mute(clicks=1_500), _mute(phrase="колледж после 9",
                                                clicks=2_500))])
    alone = consolidate._economics(
        [consolidate._one(_mute(clicks=4_000), _ctx())[0]])

    assert refusal is None
    assert payments == pytest.approx(alone[0])
    assert revenue == pytest.approx(alone[1])


def test_group_without_events_at_all_is_refused_with_a_named_reason():
    # Ноль событий — это «складывать нечего», а не «мало»: подставить сюда
    # ноль оплат значило бы отправить вынос на проверку объёма с выдумкой.
    rows = [_mute(phrase="колледж заочно", campaign="111", clicks=0),
            _mute(phrase="поступить в колледж", campaign="222", clicks=0)]
    result = consolidate.scan(rows, _ctx())

    assert result["ideas"] == []
    assert any(row["reason"] == consolidate.GROUP_REASON_NO_PAYMENTS
               for row in result["skipped"])


def test_group_margin_is_judged_on_the_campaign_that_gets_created():
    # Порог ×GROWTH_LAMBDA_MARGIN никуда не делся — он применён к выносу
    # целиком, а не к отдельной связке, у которой статистики на вердикт нет.
    ctx = _ctx(**{"lambda": 100.0})
    result = consolidate.scan(_mute_pair(), ctx)

    assert result["ideas"] == []
    assert any(row["reason"] == consolidate.GROUP_REASON_THIN_MARGIN
               for row in result["skipped"])


def test_group_without_an_average_check_names_the_missing_numerator():
    # Средний чек кампании не посчитан → выручку не из чего собрать.
    # Молчаливый отказ здесь неотличим от «связки плохие».
    rows = [dict(row, avg_check=None) for row in _mute_pair()]
    result = consolidate.scan(rows, _ctx())

    assert result["ideas"] == []
    assert result["skipped"], "отказ группы обязан быть назван"


def test_computed_romi_below_the_margin_still_drops_the_query():
    # Трёхзначность окупаемости не отменяет отказа: посчитанная и низкая
    # окупаемость — приговор связке, она не станет лучше в новой кампании.
    rows = [dict(row, romi=LAMBDA * 1.01, p_pay_sum=12.0)
            for row in _mute_pair()]
    result = consolidate.scan(rows, _ctx())

    assert result["ideas"] == []
    assert all(row["reason"] == consolidate.REASON_THIN_MARGIN
               for row in result["skipped"])
