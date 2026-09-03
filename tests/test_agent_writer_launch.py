# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_launch.py — полоса запуска: билдер как рычаг агента.

Запуск отличается от всех прочих рычагов тем, что создаёт объект, которого не
было. Отсюда три особенности, которые здесь и проверяются.

**Тело кампании собирает ДРУГОЙ репозиторий.** Агент не может отправить
`campaigns.add` сам: групп, ключей и объявлений у него нет. Поэтому
`campaign.create` не стоит в allow-листе записи, и его отказ — не «пока не
дошли руки», а описание устройства: наряд едет билдеру, а не в API.

**Кросс-минусовка едет одним тактом с созданием — и только с ним.** Донор,
заминусованный под кампанию, которой нет, теряет рабочий трафик молча: в
кабинете всё выглядит исправным. Поэтому минусовка связана с созданием
явным полем, и без него не едет.

**Из тени выпускает человек.** Лестница автономии полосу запуска не поднимает
никогда: 12 закрытых наблюдений на горизонте 30 дней — это три месяца, а
цена ошибки здесь не отматывается откатом (удаление кампаний запрещено
инвариантом, откат — пауза плюс возврат доноров).
"""

import pytest

from sync.agent import autonomy, build_order
from sync.agent.writer import (apply, exposure, guardrails, lanes, negatives,
                               risk)
from sync.agent.writer import launch


ACCOUNT = "account1-506453-ln8s"


def _order(**over):
    """Наряд на вынос двух связок у двух разных доноров."""
    order = {
        "order_id": "consolidate-vpo",
        "idea_id": "d96b5cf53b8073c1c6d122e5",
        "kind": build_order.KIND_CONSOLIDATE,
        "account": ACCOUNT,
        "level_slug": "vpo_consolidate",
        "campaign_name": "vpo / consolidate / consolidate-vpo",
        "direction": "vpo",
        "queries": [
            {"phrase": "колледж заочно москва", "donor_campaign_id": "111",
             "cost_rub": 18_400.0, "conversions": 12},
            {"phrase": "заочный колледж после 9", "donor_campaign_id": "222",
             "cost_rub": 9_100.0, "conversions": 6},
        ],
        "donor_negatives": [
            {"campaign_id": "111", "phrases": ["колледж заочно москва"]},
            {"campaign_id": "222", "phrases": ["заочный колледж после 9"]},
        ],
        "campaign": {"weekly_budget": 60_000, "target_cpa": 1_600,
                     "counter_id": 98_627_983, "goal_id": 360_811_375},
        "window_days": 30,
        "horizon_days": 30,
        "success_rule": {"metric": "cpa_rub", "op": "<=", "threshold": 1_600.0,
                         "comparison": "vs_donors"},
    }
    order.update(over)
    return order


def _actual(*campaign_ids):
    """Прочитанные списки минус-фраз доноров: у 111 фраза человека."""
    state = {}
    for campaign_id in campaign_ids or ("111", "222"):
        state[campaign_id] = {
            "campaign_type": "TEXT_CAMPAIGN",
            "negative_keywords": ["бесплатно"] if campaign_id == "111" else [],
        }
    return state


# ------------------------------------------------- созданная кампания на паузе


def test_created_campaign_is_always_paused():
    # Запуск и создание — два разных действия, и между ними стоит человек.
    action = launch.build(_order())
    assert action["payload"]["state"] == launch.STATE_SUSPENDED


def test_the_order_travels_inside_the_action():
    # Такт записи читает идеи ИЗ БАЗЫ: всё, чего нет в колонке action, для
    # него не существует. Наряд обязан ехать целиком, а не ссылкой на расчёт.
    action = launch.build(_order())
    assert action["payload"]["order"]["queries"][0]["phrase"] == "колледж заочно москва"
    assert action["payload"]["order"]["campaign"]["goal_id"] == 360_811_375


def test_an_invalid_order_never_becomes_an_action():
    # Наряд без кросс-минусовки не должен превращаться в действие даже в
    # отчёте: попав в реестр, он ждал бы применения, которого нельзя дать.
    with pytest.raises(ValueError, match="кросс-минусовк"):
        launch.build(_order(donor_negatives=[]))


# ------------------------------------------------------------ цена запуска


def test_launch_risk_is_budget_times_horizon():
    action = launch.build(_order())
    assert action["exposure"]["kind"] == exposure.LAUNCH_KIND
    assert risk.action_risk(action, {}) == pytest.approx(60_000 / 7 * 30, rel=0.01)


def test_a_launch_is_priced_without_a_single_known_campaign():
    # Справочник расходов пуст — у прочих рычагов это +inf («оценить не от
    # чего»). У запуска оценивать и не надо: под ударом ровно тот бюджет,
    # который кампании открывают, и он записан в наряде.
    assert risk.action_risk(launch.build(_order()), {}) != float("inf")


def test_a_launch_is_not_capped_by_a_stranger_daily_cost():
    # Потолок объекта — расход САМОЙ кампании за горизонт. У новой его нет, и
    # медиана чужих кампаний к ней отношения не имеет: возьми отбор её за
    # потолок, и запуск с недельным лимитом 60 000 ₽ списал бы с полосы цену
    # случайной соседки.
    action = launch.build(_order())
    assert risk.object_cap(action, {"999": 10.0}) == pytest.approx(
        risk.action_risk(action, {}), rel=0.01)


def test_the_horizon_of_the_launch_comes_from_the_order():
    # Срок замера у прочих полос — свойство полосы (lanes.MEASURE_DAYS). У
    # запуска он назначен наряду: связке, которой нужно накопить объём на
    # вердикт, ставится свой горизонт, и цена обязана считать по нему.
    action = launch.build(_order(horizon_days=60))
    assert risk.action_risk(action, {}) == pytest.approx(60_000 / 7 * 60, rel=0.01)


# ------------------------------------------ кросс-минусовка одним тактом


def test_donor_negatives_ship_in_the_same_tact():
    actions = launch.build_all(launch.build(_order()), _actual())
    kinds = [a["action_kind"] for a in actions]
    assert kinds.count(negatives.NEGATIVE_KIND) == 2
    assert launch.LAUNCH_KIND in kinds


def test_the_donor_keeps_the_phrases_a_human_put_there():
    # Минусовка донора собирается поверх ПРОЧИТАННОГО списка, а не вместо
    # него: фраза человека, вытесненная нашей, — это правка чужой настройки.
    actions = launch.build_all(launch.build(_order()), _actual())
    donor = next(a for a in actions
                 if a.get("payload", {}).get("CampaignId") == 111)
    assert "бесплатно" in donor["payload"]["NegativeKeywords"]["Items"]


def test_cross_negatives_do_not_ride_without_their_launch():
    # Донор, заминусованный под кампанию, которой нет, теряет трафик молча.
    bundle = launch.build_all(launch.build(_order()), _actual())
    kept, dropped = launch.drop_unlaunched([a for a in bundle
                                            if a["action_kind"] != launch.LAUNCH_KIND])
    assert kept == []
    assert len(dropped) == 2
    assert all("запуск" in d["blocked_reason"] for d in dropped)


def test_cross_negatives_ride_when_their_launch_rides():
    bundle = launch.build_all(launch.build(_order()), _actual())
    kept, dropped = launch.drop_unlaunched(bundle)
    assert dropped == []
    assert len(kept) == 3


def test_a_stranger_negative_is_not_touched_by_the_bundle_guard():
    # Обычная гигиена не несёт признака связки и проверке не подлежит:
    # сторож связки, начав отбирать чужие действия, стал бы вторым отбором.
    hygiene = {"action_kind": negatives.NEGATIVE_KIND,
               "idempotency_key": "k1", "payload": {"CampaignId": 333}}
    kept, dropped = launch.drop_unlaunched([hygiene])
    assert kept == [hygiene] and dropped == []


# ------------------------------------------------------------ идемпотентность


def test_a_repeated_order_does_not_breed_a_second_campaign():
    # Заливка ищет кампанию ПО ИМЕНИ (direct/upload.py билдера). Плавай в
    # имени дата, и каждый прогон генератора заводил бы новую кампанию на ту
    # же идею — а старая продолжала бы тратить.
    first = launch.build(_order())
    second = launch.build(_order())
    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["payload"]["CampaignName"] == second["payload"]["CampaignName"]


def test_the_order_id_of_an_idea_does_not_depend_on_the_day():
    idea = {"idea_id": "i-1", "account": ACCOUNT,
            "subject": {"kind": "consolidate", "direction": "vpo"},
            "horizon_days": 30, "detail": {"queries": _order()["queries"],
                                           "window_days": 30},
            "success_rule": {"metric": "cpa_rub", "op": "<=", "value": 1_600.0,
                             "comparison": "vs_donors"}}
    campaign = _order()["campaign"]
    assert (build_order.from_idea(idea, campaign=campaign)["order_id"]
            == build_order.from_idea(idea, campaign=campaign)["order_id"])


# ------------------------------------------------------------- тень полосы


def test_a_launch_reaches_no_cabinet():
    # Тело кампании собирает другой репозиторий: отправить его агент не может
    # физически. Рельса обязана сказать это словами, а не общим «вне
    # allow-листа», иначе отказ читается как недоделка.
    ok, reason = guardrails.check_action(launch.build(_order()))
    assert ok is False
    assert "билдер" in reason


def test_no_api_request_can_be_built_for_a_launch():
    with pytest.raises(ValueError):
        apply.to_api_call(launch.build(_order()))


def test_the_launch_lane_stands_in_shadow_by_default():
    # DEFAULT_STEP = 1 («приёмка нового рычага не заперта») для запуска
    # неверен: рычага записи у него на стороне агента нет вовсе.
    assert lanes.default_step_of(lanes.LANE_LAUNCH) == 0
    assert lanes.default_step_of(lanes.LANE_HYGIENE) == lanes.DEFAULT_STEP


def test_in_shadow_the_launch_is_refused_and_its_negatives_with_it():
    taken, refused = lanes.select(launch.build_all(launch.build(_order()),
                                                   _actual()),
                                  weekly_spend_rub=5_000_000.0,
                                  daily_cost_by_campaign={"111": 20_000.0,
                                                          "222": 15_000.0})
    kept, dropped = launch.drop_unlaunched(taken)
    assert launch.LAUNCH_KIND not in [a["action_kind"] for a in taken]
    assert kept == []
    assert len(refused) + len(dropped) == 3


# ----------------------------------------------------------------- откат


def _applied(state="ON"):
    """Применённая связка запуска: кампания создана, доноры заминусованы."""
    bundle = launch.build_all(launch.build(_order()), _actual())
    return {
        "create": bundle[0],
        "negatives": [a for a in bundle if a["action_kind"] == negatives.NEGATIVE_KIND],
        "campaign_id": "555",
        "state": state,
        "actual_by_campaign": {
            "111": {"campaign_type": "TEXT_CAMPAIGN",
                    "negative_keywords": ["бесплатно", "колледж заочно москва",
                                          "отзывы"]},
            "222": {"campaign_type": "TEXT_CAMPAIGN",
                    "negative_keywords": ["заочный колледж после 9"]},
        },
    }


def test_rolling_back_a_launch_pauses_and_restores_donors():
    back = launch.rollback(_applied())
    kinds = [a["action_kind"] for a in back]
    assert kinds.count("campaign.suspend") == 1
    assert kinds.count(negatives.REMOVE_KIND) == 2


def test_a_rollback_restores_donors_even_if_the_campaign_is_already_paused():
    # Пауза уже стоит — выключать нечего, а доноры всё равно минусованы по
    # фразам, которые больше некому обслуживать.
    back = launch.rollback(_applied(state="SUSPENDED"))
    kinds = [a["action_kind"] for a in back]
    assert "campaign.suspend" not in kinds
    assert kinds.count(negatives.REMOVE_KIND) == 2


def test_a_rollback_keeps_the_phrases_added_between_the_tacts():
    back = launch.rollback(_applied())
    donor = next(a for a in back
                 if a.get("payload", {}).get("CampaignId") == 111)
    items = donor["payload"]["NegativeKeywords"]["Items"]
    assert "колледж заочно москва" not in items
    assert items == ["бесплатно", "отзывы"]


def test_a_rollback_without_a_campaign_id_still_restores_donors():
    # Наряд уехал билдеру, кампанию завести не вышло, минусовка применилась.
    applied = _applied()
    applied["campaign_id"] = None
    kinds = [a["action_kind"] for a in launch.rollback(applied)]
    assert kinds == [negatives.REMOVE_KIND, negatives.REMOVE_KIND]


# --------------------------------------------- снятие своей же минус-фразы


def test_removing_added_negatives_keeps_manual_ones():
    assert negatives.remove_added(current={"а", "б", "в"},
                                  added={"б"}) == {"а", "в"}


def test_removing_added_negatives_ignores_what_is_not_there():
    # Фразу мог снять человек. «Её нет» — не ошибка и не повод трогать список.
    assert negatives.remove_added(current={"а"}, added={"б"}) == {"а"}


def test_the_removal_action_names_what_it_takes_out():
    action = negatives.remove_added_action(
        "111", current=["бесплатно", "колледж заочно москва"],
        added=["колледж заочно москва"])
    assert action["payload"]["RemovedPhrases"] == ["колледж заочно москва"]
    assert action["payload"]["NegativeKeywords"]["Items"] == ["бесплатно"]
    assert action["previous_state"]["NegativeKeywords"]["Items"] == [
        "бесплатно", "колледж заочно москва"]


def test_a_removal_that_takes_nothing_is_not_an_action():
    assert negatives.remove_added_action(
        "111", current=["бесплатно"], added=["колледж заочно москва"]) is None


def test_the_rail_lets_through_a_removal_of_our_own_phrase():
    ok, reason = guardrails.check_action(negatives.remove_added_action(
        "111", current=["бесплатно", "колледж заочно москва"],
        added=["колледж заочно москва"]))
    assert ok is True, reason


def test_the_rail_refuses_removing_a_stranger_phrase():
    # Сторож удаления объектов на снятие своей же фразы не распространяется,
    # но освобождением он быть не должен: снять чужую минус-фразу значит
    # вернуть трафик, который человек выключил сознательно.
    action = negatives.remove_added_action(
        "111", current=["бесплатно", "колледж заочно москва"],
        added=["колледж заочно москва"])
    action["payload"]["RemovedPhrases"] = ["бесплатно"]
    ok, reason = guardrails.check_action(action)
    assert ok is False
    assert "не добавлял" in reason


def test_the_rail_refuses_a_removal_that_does_not_match_the_previous_list():
    action = negatives.remove_added_action(
        "111", current=["бесплатно", "колледж заочно москва"],
        added=["колледж заочно москва"])
    action["payload"]["NegativeKeywords"]["Items"] = []
    ok, reason = guardrails.check_action(action)
    assert ok is False
    assert "прежн" in reason


def test_a_removal_is_a_campaigns_update():
    service, method, params = apply.to_api_call(negatives.remove_added_action(
        "111", current=["бесплатно", "колледж заочно москва"],
        added=["колледж заочно москва"]))
    assert (service, method) == ("campaigns", "update")
    assert params["Campaigns"][0]["NegativeKeywords"]["Items"] == ["бесплатно"]


# ------------------------------------------------- лестница не поднимает


def test_launch_lane_never_auto_promotes():
    record = {"closed": 100, "improved": 90, "money_confirmed": 80}
    assert autonomy.step_of(lanes.LANE_LAUNCH, record) == 0


def test_another_lane_with_the_same_record_climbs():
    # Ноль у запуска — правило про полосу, а не про пустую лестницу.
    record = {"closed": 100, "improved": 90, "money_confirmed": 80}
    assert autonomy.step_of(lanes.LANE_HYGIENE, record) > 0


def test_the_manual_lane_is_named_by_the_same_string_as_the_lane():
    # Имя полосы живёт в lanes, правило — в autonomy, а импортировать lanes
    # оттуда нельзя (кольцо). Сторож держит две строки вместе.
    assert lanes.LANE_LAUNCH in autonomy.MANUAL_RELEASE_LANES


# -------------------------------------------- настройки кампании от доноров


def _donor_settings():
    # Формат — витрина edu_campaign_settings (edu_direct_settings.py), а не
    # сырой campaigns.get: именно её подаёт бой (agent_e0 →
    # db.load_campaign_settings_raw → bundles → consolidate). Прежняя
    # фикстура в сыром формате держала тесты зелёными, пока в проде КАЖДЫЙ
    # вынос отказывал с «не прочитан счётчик».
    return {
        "meta": {"counterIds": [98_627_983]},
        "strategy": {"search": {"goalIds": [360_811_375],
                                "biddingStrategyType": "AVERAGE_CPA"}},
        "targeting": {"regions": [1, 10_716]},
    }


def _donors():
    return [
        {"donor_campaign_id": "111", "cost_rub": 18_400.0, "conversions": 12,
         "settings": _donor_settings()},
        {"donor_campaign_id": "222", "cost_rub": 9_100.0, "conversions": 6,
         "settings": _donor_settings()},
    ]


def test_campaign_settings_come_from_the_donors():
    # Счётчик и цель не выдумываются и не берутся из панели: новая кампания
    # обязана мерить тем же, чем меряют доноры, иначе её результат несравним
    # с тем, ради чего вынос затевался.
    campaign, reason = launch.campaign_from_donors(
        _donors(), donor_cpa=1_600.0, window_days=30)
    assert reason is None
    assert campaign["counter_id"] == 98_627_983
    assert campaign["goal_id"] == 360_811_375
    assert campaign["target_cpa"] == 1_600


def test_the_raw_campaigns_get_format_is_still_readable():
    # Запасной путь: наряд может собираться и из свежего чтения кабинета, где
    # настройки в сыром формате campaigns.get.
    raw = {"TextCampaign": {
        "CounterIds": {"Items": [98_627_983]},
        "BiddingStrategy": {"Search": {"AverageCpa": {
            "GoalId": 360_811_375, "AverageCpa": 1_600_000_000}}}}}
    donors = _donors()
    for donor in donors:
        donor["settings"] = raw
    campaign, reason = launch.campaign_from_donors(
        donors, donor_cpa=1_600.0, window_days=30)
    assert reason is None
    assert campaign["goal_id"] == 360_811_375


def test_the_weekly_budget_is_what_the_donors_already_spend():
    campaign, _ = launch.campaign_from_donors(
        _donors(), donor_cpa=1_600.0, window_days=30)
    assert campaign["weekly_budget"] == round((18_400.0 + 9_100.0) / 30 * 7)


def test_donors_disagreeing_on_the_goal_refuse_the_launch():
    donors = _donors()
    donors[1]["settings"]["strategy"]["search"]["goalIds"] = [111_222_333]
    campaign, reason = launch.campaign_from_donors(
        donors, donor_cpa=1_600.0, window_days=30)
    assert campaign is None
    assert "цел" in reason


def test_a_donor_without_a_goal_refuses_the_launch():
    # Стратегия AVERAGE_CPA без цели невозможна, и кампания встала бы на
    # ручных ставках — тихо, уже после заливки.
    donors = _donors()
    donors[0]["settings"] = {}
    campaign, reason = launch.campaign_from_donors(
        donors, donor_cpa=1_600.0, window_days=30)
    assert campaign is None
    assert reason


# ------------------------------------------- критерий генератора и наряд


def test_the_criterion_of_the_consolidate_generator_is_a_valid_baseline():
    # Генератор выноса судит новую кампанию донорской ценой конверсии. Не
    # окажись этой базы среди допустимых, наряд из его же идеи не собрался
    # бы — а тесты обеих сторон остались бы зелёными на выдуманном входе.
    assert "vs_donors" in build_order.COMPARISONS


# ------------------------------------------------- включение через апрув


def _built_row(**over):
    row = {"status": "built", "campaign_id": "987654",
           "started_on": None, "account": "acc-edu",
           "campaign_name": "EDU_CONS_MSK", "order_id": "ord-1"}
    row.update(over)
    return row


def test_resume_action_from_a_built_order():
    action = launch.resume_action(_built_row())
    assert action["action_kind"] == launch.RESUME_KIND
    assert action["object_id"] == "987654"
    assert action["account"] == "acc-edu"
    assert action["payload"]["CampaignId"] == 987654
    assert action["payload"]["order_id"] == "ord-1"
    assert action["previous_state"] == {"State": launch.STATE_SUSPENDED}
    # Риском не платит и красной линии не несёт: деньги — переезд донорских,
    # наблюдение — vs_holdout наряда, не рельса.
    assert action["risk_rub"] == 0.0
    assert action["red_line"] == {}


def test_resume_action_key_depends_only_on_campaign():
    a = launch.resume_action(_built_row())
    b = launch.resume_action(_built_row(campaign_name="другое имя",
                                        order_id="ord-2"))
    assert a["idempotency_key"] == b["idempotency_key"]
    assert a["idempotency_key"] != launch._rollback_key("suspend", "987654")


def test_resume_action_refuses_orders_that_are_not_ready():
    # Не built, без адреса, уже включена — во всех трёх включать нечего.
    assert launch.resume_action(_built_row(status="queued")) is None
    assert launch.resume_action(_built_row(campaign_id="")) is None
    assert launch.resume_action(_built_row(started_on="2026-09-03")) is None


def test_resume_action_travels_to_the_api_as_campaigns_resume():
    action = launch.resume_action(_built_row())
    service, method, params = apply.to_api_call(action)
    assert (service, method) == ("campaigns", "resume")
    assert params == {"SelectionCriteria": {"Ids": [987654]}}
    # Ответ resume разбираем по элементам — метод обязан быть известен
    # разборщику, иначе исход уехал бы в stale.
    assert apply._RESULT_COLLECTION.get("resume") == "ResumeResults"


def test_resume_kind_is_not_writable_from_the_general_plan():
    # Включение едет ТОЛЬКО через апрув-контур: общий план его отклоняет.
    ok, reason = guardrails.check_action(launch.resume_action(_built_row()))
    assert ok is False
