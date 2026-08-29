# -*- coding: utf-8 -*-
"""
tests/test_agent_ideas_closure.py — замыкание реестра идей.

Выигравшая гипотеза закрывалась вердиктом и на этом заканчивалась, а
генератор детерминирован: назавтра он предлагал ровно ту же проигравшую
гипотезу снова. Отсюда две болезни сразу — доказанное нашими же деньгами
никуда не идёт, а опровергнутое проверяется вечно.

Проверяется здесь ровно то, что ломается молча:

  * исход ставки закрывает СВОЮ идею, а не остаётся в таблице гипотез;
  * выигрыш порождает идею масштабирования с ссылкой на родителя;
  * цепочка масштабирований не уходит в бесконечность;
  * проигрыш становится адресным запретом, а не общим молчанием.

БД не требуется: реестр подменяется двойником (фикстура store в conftest).
"""

import pytest

from sync.agent import experiments
from sync.agent.ideas import abtests, proven, registry
from sync.agent.writer import lanes, tier

ACCOUNT = "edu-vuz"
CAMPAIGN = "555"
EXPERIMENT = "exp-1"


def _won_idea(**over):
    """Строка реестра, закрытая выигранной ставкой."""
    subject = {"campaign_id": CAMPAIGN,
               "segment": {"kind": "device", "key": "MOBILE"}}
    row = {
        "idea_id": registry.idea_id(proven.SOURCE, subject, ACCOUNT),
        "source": proven.SOURCE,
        "account": ACCOUNT,
        "subject": subject,
        "subject_key": registry.subject_key(subject, ACCOUNT),
        "tier": tier.TIER_MEASURED,
        "lane": lanes.LANE_TUNING,
        "expected_rub": 12_000.0,
        "test_cost_rub": 3_000.0,
        "horizon_days": experiments.HORIZON_DAYS,
        "success_rule": {"metric": "eff_cpl", "op": "<=", "value": 1500.0},
        "detail": {"segment_share": 0.31},
        "action": {"kind": "bidmodifier.add"},
        "status": registry.STATUS_DONE,
        "experiment_id": EXPERIMENT,
        "bet_status": experiments.STATUS_WON,
    }
    row.update(over)
    return row


def _running(store, **over):
    """Живая идея, за которой стоит ставка: её и закрывают исходом."""
    row = {**_won_idea(**over), "status": registry.STATUS_RUNNING}
    row.pop("bet_status", None)
    store.table[row["idea_id"]] = row
    return row


# ------------------------------------------------ исход ставки закрывает идею


def test_a_won_bet_closes_its_idea_as_done(store):
    row = _running(store)

    closed = registry.settle_by_experiment(EXPERIMENT, experiments.STATUS_WON,
                                           "горизонт закрыт вердиктом «improved»")

    assert closed["idea_id"] == row["idea_id"]
    assert closed["status"] == registry.STATUS_DONE
    assert store.table[row["idea_id"]]["status"] == registry.STATUS_DONE


def test_a_lost_bet_closes_its_idea_with_a_named_reason(store):
    # Снятая машиной идея без причины на разборе неотличима от отказа
    # человека, а лечатся они противоположным.
    row = _running(store)

    closed = registry.settle_by_experiment(EXPERIMENT, experiments.STATUS_LOST,
                                           "наблюдение закрыто без вердикта")

    assert closed["status"] == registry.STATUS_DROPPED
    assert "наблюдение закрыто без вердикта" in closed["dropped_reason"]
    # Машинное снятие объект НЕ глушит: право сказать «нет» навсегда — у
    # человека, и признак его отказа отдельный (registry.reject).
    assert not closed.get("rejected_by")


def test_a_bet_without_an_idea_is_not_an_error(store):
    # Половина ставок заводится рычагами расчёта, а не идеями. Падать здесь
    # значило бы ронять сторожа на каждом обычном действии.
    assert registry.settle_by_experiment("нет-такой", experiments.STATUS_WON) is None


def test_an_unknown_bet_outcome_is_refused(store):
    _running(store)

    with pytest.raises(registry.InvalidIdea):
        registry.settle_by_experiment(EXPERIMENT, "победа-наверное")


def test_a_closed_idea_keeps_its_first_outcome(store):
    # Из закрытого статуса пути нет: второй исход означал бы, что ставку
    # посчитали дважды.
    _running(store)
    registry.settle_by_experiment(EXPERIMENT, experiments.STATUS_WON)

    with pytest.raises(registry.InvalidIdea):
        registry.settle_by_experiment(EXPERIMENT, experiments.STATUS_LOST)


# ------------------------------------------- выигрыш порождает масштабирование


def _spawned(settled, ctx):
    """Только идеи: у замыкания форма вызова одна — scan_closed с причинами.

    Отдельной обёртки candidates_from_closed нет намеренно: боевой путь
    печатает отбракованные строки в отчёт, а функция без боевого вызова — это
    мёртвый код, который гейт tests/test_no_orphan_code.py не пропускает.
    """
    return proven.scan_closed(settled, ctx)["ideas"]


def _ctx(**over):
    ctx = {"account": ACCOUNT}
    ctx.update(over)
    return ctx


def test_a_won_idea_spawns_a_scaling_idea():
    parent = _won_idea()

    spawned = _spawned([parent], _ctx())

    assert spawned
    assert spawned[0]["source"] == proven.SOURCE
    assert spawned[0]["subject"]["parent_idea_id"] == parent["idea_id"]


def test_a_lost_idea_spawns_nothing():
    # Масштабировать нечего: деньги потрачены, утверждение не подтвердилось.
    lost = _won_idea(status=registry.STATUS_DROPPED,
                     bet_status=experiments.STATUS_LOST)

    assert _spawned([lost], _ctx()) == []


def test_the_spawn_keeps_the_address_of_its_parent():
    # Масштабируется ДОКАЗАННОЕ место, а не абстрактная идея роста: потеряй
    # адрес — и предложение станет лозунгом.
    spawned = _spawned([_won_idea()], _ctx())[0]

    assert spawned["subject"]["campaign_id"] == CAMPAIGN
    assert spawned["subject"]["segment"] == {"kind": "device", "key": "MOBILE"}


def test_the_spawn_is_a_proposal_and_carries_no_payload():
    # Нагрузку рычага расчётный такт собрать не может: bidmodifiers.add
    # поверх УЖЕ поставленной корректировки Директ отвергает, а перезапись
    # требует прочитанного состояния кабинета. Класс 3 — честный ответ.
    spawned = _spawned([_won_idea()], _ctx())[0]

    assert spawned["tier"] == tier.TIER_PROPOSAL
    assert spawned["lane"] == lanes.LANE_PROPOSAL
    assert not spawned.get("action")


def test_the_spawn_is_accepted_by_the_registry(store):
    # Проверка у ПОЛУЧАТЕЛЯ: идея, которую реестр отвергнет, — это молчаливая
    # потеря всей ветки замыкания, и увидеть её в отчёте будет нечем.
    written = registry.upsert(_spawned([_won_idea()], _ctx()))

    assert len(written) == 1
    assert written[0]["status"] == registry.STATUS_NEW


def test_the_same_parent_spawns_the_same_idea_twice():
    # Иначе каждый прогон заводит новую строку на то же масштабирование —
    # ровно та болезнь, ради которой заведён реестр.
    first = _spawned([_won_idea()], _ctx())[0]
    second = _spawned([_won_idea()], _ctx())[0]

    assert (registry.idea_id(first["source"], first["subject"], ACCOUNT)
            == registry.idea_id(second["source"], second["subject"], ACCOUNT))


def test_the_chain_does_not_go_on_forever():
    # Масштабирование масштабирования масштабирования съедает кабинет:
    # каждое звено обосновано предыдущим, а не фактом.
    deep = _won_idea(detail={"chain_depth": proven.MAX_CHAIN_DEPTH})

    found = proven.scan_closed([deep], _ctx())

    assert found["ideas"] == []
    assert found["skipped"][0]["reason"] == proven.REASON_CHAIN_DEPTH


def test_the_chain_depth_grows_by_one():
    spawned = _spawned([_won_idea()], _ctx())[0]

    assert spawned["detail"]["chain_depth"] == 1


def test_an_idea_of_another_cabinet_is_not_scaled():
    # Кабинет входит в идентичность идеи: доказательство одного кабинета не
    # является доказательством для другого.
    stranger = _won_idea(account="edu-spo")

    assert _spawned([stranger], _ctx()) == []


# ------------------------------------------- проигрыш становится запретом


def _campaign(**over):
    row = {
        "campaign_id": CAMPAIGN,
        "direction": "vpo",
        "eff_leads": 400.0 * 2,
        "cost_rub": 560_000.0,
        "window_days": 28,
        "in_holdout": False,
        "limit_binds": True,
        "days_since_learning_reset": 60,
    }
    row.update(over)
    return row


def _lesson(campaign_id=CAMPAIGN, test_kind="budget_down"):
    """Урок из проигранной ставки: адрес теста, и ничего кроме адреса."""
    return {"account": ACCOUNT, "source": abtests.SOURCE,
            "subject": {"kind": abtests.SOURCE, "campaign_id": campaign_id,
                        "test_kind": test_kind}}


def _kinds(ctx):
    return {i["subject"]["test_kind"]
            for i in abtests.candidates([_campaign()], ctx)}


def test_a_lost_test_is_not_offered_again():
    # Генератор детерминирован: без урока он предложит опровергнутую гипотезу
    # тем же тактом, и агент будет вечно проверять одно и то же.
    offered = _kinds({"account": ACCOUNT, "holdout_ids": {"999"}})
    assert "budget_down" in offered

    after = _kinds({"account": ACCOUNT, "holdout_ids": {"999"}, "lost_tests": [_lesson()]})
    assert "budget_down" not in after


def test_the_ban_is_addressed_not_global():
    # Проигрыш одного типа теста не запрещает остальные, а проигрыш на одной
    # кампании — тот же тест на другой. Иначе первая же неудача выключала бы
    # генератор целиком.
    after = _kinds({"account": ACCOUNT, "holdout_ids": {"999"}, "lost_tests": [_lesson()]})

    assert after, "остальные типы теста обязаны остаться"
    other = abtests.candidates(
        [_campaign(campaign_id="777")],
        {"account": ACCOUNT, "holdout_ids": {"999"}, "lost_tests": [_lesson()]})
    assert "budget_down" in {i["subject"]["test_kind"] for i in other}


def test_the_ban_is_visible_as_a_named_refusal():
    # Молчащий отказ неотличим от «повода не нашлось».
    skipped = abtests.scan([_campaign()],
                           {"account": ACCOUNT, "holdout_ids": {"999"}, "lost_tests": [_lesson()]})["skipped"]

    banned = [row for row in skipped if row["reason"] == abtests.REASON_LOST_BEFORE]
    assert [row["test_kind"] for row in banned] == ["budget_down"]


def test_scaling_proposal_inherits_neither_the_value_nor_the_price(store):
    # Ценность родителя уже получена, а его смета уже потрачена: переносить
    # ни то, ни другое нельзя. Своей сметы у предложения нет вовсе —
    # риск-бюджет полосы за него не платит никто (полоса proposal,
    # writer/lanes.RISK_PAYING_LANES), — а чужая смета рядом с пустым
    # ожиданием реестру запрещена (ideas/limits.unpaired_reason): порция
    # масштабирования падала бы целиком, и выигранная ставка не давала бы
    # продолжения вовсе.
    idea = proven.scan_closed([_won_idea()], {"account": ACCOUNT})["ideas"][0]

    assert idea["expected_rub"] is None
    assert idea["test_cost_rub"] is None
    registry.upsert([idea])
