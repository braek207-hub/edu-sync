# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_geo.py — рычаг географии показов (задача 24).

География решает, в какие аукционы кампания входит вообще, и один вид
действия несёт тут ДВА разных утверждения:

  * сужение — утверждение о ПРОШЛОМ: регион откручивал деньги и за зрелое
    окно не дал ни одной конверсии при расходе выше трёх её цен. Класс 0,
    риском не платит — та же арифметика, что минус-фраза и запрет площадки;
  * расширение — СТАВКА: истории по новому региону у этой кампании нет по
    определению, и число обещания снято с другого объекта. Класс 2.

Отсюда главная проверка файла: класс считается по СОДЕРЖИМОМУ действия, а не
по слову в названии вида. Назови ход «сужением» — и он всё равно останется
ставкой, пока список регионов не окажется строгим подмножеством прочитанного
из кабинета.

Форма запроса не выдумана: RegionIds — поле ГРУППЫ (читатель кабинета
sync/edu_direct_settings._fetch_adgroups_by_campaign; там же его читают
sync/agent/segments.py и sync/lime_direct.py), а у кампании такого поля нет ни
в чтении, ни в записи. Значит запрос уходит в adgroups.update, а действие
всё равно адресовано кампании — по ней считаются цена риска и кулдаун.
"""

import pytest

from sync.agent.objects import CANDIDATE_WINDOW_DAYS
from sync.agent.writer import (expectation, geo, guardrails, lanes, learning,
                               tier)
from sync.agent.writer.apply import to_api_call
from sync.agent.writer.guardrails import check_action, check_rollback
from sync.agent.writer.rollback import rollback_payload
from sync.agent_e1_watchdog import rollback_guard_form

CAMPAIGN = "111"
MOSCOW = 213
SPB = 2
NOVOSIBIRSK = 65
KAZAN = 43          # регион, которого у кампании нет
GROUPS = (1001, 1002)

CURRENT = (MOSCOW, SPB, NOVOSIBIRSK)


def _state(regions=CURRENT, groups=GROUPS, **over):
    """Прочитанное состояние: тип кампании и её группы с их регионами."""
    state = {
        "campaign_type": "TEXT_CAMPAIGN",
        "adgroups": [{"id": g, "region_ids": list(regions)} for g in groups],
    }
    state.update(over)
    return state


def _desired(regions, **over):
    """Ход рычага: полный желаемый список и числа, которыми ход судится."""
    move = {"region_ids": list(regions),
            # сужение: расход убираемых регионов за окно и их конверсии
            "cut_cost": 12_000.0, "cut_conversions": 0, "baseline_cpa": 2_400.0,
            # расширение: расход нового региона (снят с другого объекта) и
            # цена лида ЭТОЙ кампании
            "added_daily_rub": 400.0, "cpa_rub": 2_400.0}
    move.update(over)
    return {CAMPAIGN: move}


def _diff(regions, state=None, coverage=None, **over):
    return geo.diff_geo(_desired(regions, **over),
                        {CAMPAIGN: state or _state()},
                        coverage_by_region=coverage)


def _narrow(**over):
    """Сужение: Новосибирск убран, остальное на месте."""
    return _diff((MOSCOW, SPB), **over)


def _widen(**over):
    """Расширение: добавлена Казань, где кампания не показывалась ни дня."""
    return _diff(CURRENT + (KAZAN,), **over)


# ------------------------------------------- шаг 1: расширение — ставка


def test_widening_geo_is_a_bet_not_a_measurement():
    # Шаг 1 задачи 24. Обещание у расширения ЕСТЬ — значит по общему правилу
    # вид получил бы класс 1 «измерено здесь» и заплатил бы риском как за
    # измерение. Это занижение цены уверенности: в новом регионе кампания не
    # показывалась ни дня, и число перенесено с другого объекта.
    action = _widen()[0][0]
    assert expectation.of(action, {}) is not None
    assert tier.tier_of(action) == tier.TIER_BET


def test_widening_stays_a_bet_even_with_a_cutting_evidence():
    # Класс считает СОДЕРЖИМОЕ, а не пометку. Подложи расширению основание
    # отсечения — оно всё равно не сужение: список регионов вырос.
    action = _widen()[0][0]
    forged = {**action, "evidence": {"cost_rub": 90_000.0, "conversions": 0,
                                     "baseline_cpa": 2_400.0,
                                     "window_days": 30}}
    assert tier.tier_of(forged) == tier.TIER_BET


# ------------------------------------------- шаг 2: сужение — класс 0


def test_narrowing_with_zero_conversions_over_a_mature_window_is_arithmetic():
    # Шаг 2. 12 000 ₽ при цене лида 2 400 ₽ — это пять её цен, конверсий ноль,
    # окно 30 дней зрелое. Утверждение о прошлом, а не прогноз: класс 0.
    action = _narrow()[0][0]
    assert tier.tier_of(action) == tier.TIER_ARITHMETIC
    assert tier.TIER_ARITHMETIC not in tier.RISK_PAYING_TIERS


def test_narrowing_over_an_immature_window_is_not_arithmetic():
    # Ноль конверсий на коротком окне значит «не приехало», а не «не было»:
    # лиды CRM идут днями целиком с лагом 2–4 дня.
    actions, _ = geo.diff_geo(_desired((MOSCOW, SPB)),
                              {CAMPAIGN: _state()}, window_days=5)
    assert tier.tier_of(actions[0]) > tier.TIER_ARITHMETIC


def test_narrowing_of_converting_traffic_is_not_arithmetic():
    # На убираемом регионе есть конверсии — это уже прогноз «дороже, чем нам
    # надо», а не утверждение о прошлом.
    action = _narrow(cut_conversions=4)[0][0]
    assert tier.tier_of(action) > tier.TIER_ARITHMETIC


def test_narrowing_without_a_baseline_cpa_is_not_arithmetic():
    # Порога нет — показать, что расход превысил три цены конверсии, нечем.
    action = _narrow(baseline_cpa=None)[0][0]
    assert "evidence" not in action
    assert tier.tier_of(action) > tier.TIER_ARITHMETIC


def test_unmeasured_conversions_do_not_buy_the_zero_tier():
    # «Не измеряли» обязано остаться отличимым от «ноль»: ноль даёт право
    # резать без риск-бюджета, неизвестность не даёт.
    action = _narrow(cut_conversions=None)[0][0]
    assert tier.tier_of(action) > tier.TIER_ARITHMETIC


# ------------------------------------------- шаг 3: каннибализация


def test_a_region_another_campaign_already_covers_is_not_added():
    # Шаг 3. Две кампании кабинета в одном аукционе торгуются друг с другом и
    # поднимают цену клика себе же. Отказ несёт и регион, и номер кампании,
    # которая его занимает, — иначе его не с чем сопоставить.
    actions, refused = _widen(coverage={KAZAN: ["222"]})
    assert actions == []
    assert str(KAZAN) in refused[0]["reason"]
    assert "222" in refused[0]["reason"]


def test_the_campaigns_own_regions_are_not_cannibalisation():
    # Своё покрытие — не чужое: кампания уже ведёт Москву, и это ей не мешает.
    actions, refused = _widen(coverage={MOSCOW: [CAMPAIGN], KAZAN: [CAMPAIGN]})
    assert refused == [] and len(actions) == 1


def test_coverage_keys_are_read_as_strings_too():
    # Карта покрытия приходит из JSON-подобных источников, где целые ключи
    # становятся строками. Форма уже, чем у источника, означала бы, что
    # каннибализация не находится вовсе.
    actions, refused = _widen(coverage={str(KAZAN): ["222"]})
    assert actions == [] and refused


def test_narrowing_ignores_the_coverage_map():
    # Убираемый регион занимать нечем: каннибализация — свойство ДОБАВЛЯЕМОГО.
    actions, refused = _narrow(coverage={NOVOSIBIRSK: ["222"]})
    assert refused == [] and len(actions) == 1


# ------------------------------------------- когда рычаг отказывается


def test_a_mixed_move_is_refused():
    # Убрать одно и добавить другое одним действием — два утверждения разных
    # классов в одной строке: замер не сказал бы, какая сторона сдвинула
    # число, а цена посчиталась бы по одной из них.
    actions, refused = _diff((MOSCOW, SPB, KAZAN))
    assert actions == []
    assert "два" in refused[0]["reason"]


def test_an_empty_region_list_is_refused():
    actions, refused = _diff(())
    assert actions == []
    assert "пуст" in refused[0]["reason"]


def test_a_negative_region_is_refused_not_guessed():
    # Отрицательный id — минус-регион («Россия − Москва», см.
    # lime_direct._format_regions_display). Покрытие такого списка по спискам
    # не вычисляется, и сужает ли ход — тоже.
    actions, refused = _diff((MOSCOW, -SPB))
    assert actions == []
    assert "минус-регион" in refused[0]["reason"]


def test_groups_targeted_differently_are_refused():
    # Единый список стёр бы ручное разделение по группам, которое сделал
    # человек: география кампании в этом случае просто не определена.
    split = _state()
    split["adgroups"][1]["region_ids"] = [MOSCOW]
    actions, refused = _diff((MOSCOW, SPB), state=split)
    assert actions == []
    assert "разную географию" in refused[0]["reason"]


def test_a_campaign_without_groups_is_refused():
    actions, refused = _diff((MOSCOW, SPB), state=_state(adgroups=[]))
    assert actions == []
    assert "групп" in refused[0]["reason"]


def test_cutting_more_than_half_of_the_geography_is_refused():
    # Из трёх регионов убираются два: от прежнего объекта осталось меньше,
    # чем убрано. Это не правка географии, а другая кампания.
    actions, refused = _diff((MOSCOW,))
    assert actions == []
    assert "другая кампания" in refused[0]["reason"]


def test_the_same_geography_is_not_an_action():
    actions, refused = _diff(CURRENT)
    assert actions == [] and refused == []


def test_a_non_text_campaign_is_refused():
    actions, refused = _diff((MOSCOW, SPB),
                             state=_state(campaign_type="SMART_CAMPAIGN"))
    assert actions == []
    assert "не текстовая" in refused[0]["reason"]


def test_a_campaign_missing_from_the_cabinet_is_silent():
    actions, refused = geo.diff_geo(_desired((MOSCOW, SPB)), {})
    assert actions == [] and refused == []


# ------------------------------------------- форма запроса к API


def test_the_request_goes_to_the_ad_groups_not_to_the_campaign():
    # RegionIds — поле ГРУППЫ: у кампании его нет ни в чтении, ни в записи
    # (edu_direct_settings: base_fields кампании региона не содержат, а
    # _fetch_adgroups_by_campaign спрашивает RegionIds у adgroups.get).
    service, method, params = to_api_call(_narrow()[0][0])
    assert (service, method) == ("adgroups", "update")
    assert [g["Id"] for g in params["AdGroups"]] == list(GROUPS)


def test_the_list_travels_whole_because_the_api_replaces_it_whole():
    # Дельты в API нет: список регионов заменяется целиком, и полный новый
    # список собран из ПРОЧИТАННОГО состояния.
    _, _, params = to_api_call(_narrow()[0][0])
    for group in params["AdGroups"]:
        assert group["RegionIds"] == sorted([MOSCOW, SPB])


def test_the_previous_list_travels_for_the_rollback():
    action = _narrow()[0][0]
    assert action["previous_state"]["RegionIds"] == sorted(CURRENT)
    assert action["previous_state"]["AdGroupIds"] == list(GROUPS)


def test_the_key_does_not_depend_on_the_order_of_regions():
    one = _diff((MOSCOW, SPB))[0][0]
    two = _diff((SPB, MOSCOW))[0][0]
    assert one["idempotency_key"] == two["idempotency_key"]


# ------------------------------------------- полоса, обучение, экспозиция


def test_the_lever_lives_in_the_allocation_lane():
    # Полоса 3: география меняет, КУДА кампания тратит те же деньги.
    assert lanes.lane_of(_narrow()[0][0]) == lanes.LANE_ALLOCATION


def test_geo_is_neither_declared_safe_nor_declared_resetting():
    """Справка географию перезапускающей не называет — утверждать «сбрасывает»
    нечем. Но под новым списком регионов стратегия торгуется на другом наборе
    аукционов, и записать это в безопасные значило бы выдать незнание за
    знание. Класс «unknown», и кулдаун держит гео как сбрасывающее — тот же
    исход, что у расписания.
    """
    assert geo.GEO_KIND not in learning.RESETS_LEARNING
    assert geo.GEO_KIND not in learning.SAFE_FOR_LEARNING
    assert learning.learning_impact(_narrow()[0][0]) == "unknown"


def test_narrowing_puts_only_the_cut_traffic_at_risk():
    # Под ударом ровно вырезаемый поток — столько же, сколько у запрета
    # площадки: 12 000 ₽ за 30 дней окна.
    action = _narrow()[0][0]
    assert action["exposure"]["daily_rub"] == pytest.approx(
        12_000.0 / CANDIDATE_WINDOW_DAYS, abs=0.01)


def test_widening_puts_the_whole_object_at_risk():
    # Лимит рычаг не двигает: новый регион берёт деньги из ТОГО ЖЕ лимита, а
    # какую долю — неизвестно. Неизвестная доля означает «весь объект».
    assert _widen()[0][0]["exposure"]["share"] == 1.0


# ------------------------------------------- обещание


def test_narrowing_promises_less_spend_and_no_fewer_leads():
    # Зеркало отсечения: 400 ₽/дн уходит с кабинета за 14 дней замера, лидов
    # на вырезаемом трафике не было.
    exp = expectation.of(_narrow()[0][0], {})
    assert exp["rub_delta"] == pytest.approx(-5_600.0, abs=0.01)
    assert exp["leads_delta"] == 0.0
    assert exp["measure_days"] == lanes.MEASURE_DAYS[lanes.LANE_ALLOCATION]


def test_narrowing_of_converting_traffic_admits_the_loss():
    # Режется трафик с конверсиями — обещать «лидов не потеряем» значило бы
    # сделать наблюдение заведомо провальным.
    exp = expectation.of(_narrow(cut_conversions=6)[0][0], {})
    assert exp["leads_delta"] < 0


def test_widening_promises_more_spend_and_more_leads():
    # 400 ₽/дн нового региона × 14 дн = 5 600 ₽; при цене лида 2 400 ₽ это
    # 2.33 лида. Расход региона снят с другого объекта — основание обязано
    # это сказать, иначе замер не знает, откуда число.
    exp = expectation.of(_widen()[0][0], {})
    assert exp["rub_delta"] == pytest.approx(5_600.0, abs=0.01)
    assert exp["leads_delta"] == pytest.approx(2.33, abs=0.01)
    assert "с другого объекта" in exp["basis"]


def _raw(action):
    """То же действие без ЗАЯВЛЕННОГО ожидания: заставляет модель считать.

    expectation.of сначала читает заявленное рычагом (payload), и на готовом
    действии подделка списков ничего бы не показала — вернулось бы число,
    посчитанное при сборке.
    """
    payload = {k: v for k, v in (action.get("payload") or {}).items()
               if k not in (expectation.LEADS_KEY, expectation.RUB_KEY,
                            expectation.BASIS_KEY, expectation.DAYS_KEY)}
    return {**action, "payload": payload}


def test_the_promise_reads_the_lists_not_the_builders_label():
    # AddedRegionIds и RemovedRegionIds посчитал ТОТ ЖЕ код, что и сам ход, —
    # выбирать по ним модель обещания значит проверять построитель его же
    # ответом. Подложи расширению пометку сужения: списки говорят, что регион
    # добавлен, и обещание обязано остаться обещанием РОСТА.
    action = _raw(_widen()[0][0])
    forged = {**action, "payload": {**action["payload"],
                                    "RemovedRegionIds": [NOVOSIBIRSK],
                                    "AddedRegionIds": []}}
    exp = expectation.of(forged, {"added_daily_rub": 400.0, "cpa_rub": 2_400.0})
    assert exp is not None and exp["rub_delta"] > 0


def test_without_the_previous_list_nothing_is_promised():
    # Прошлого списка нет — направление хода неизвестно, а модели у гео две.
    # Выбрать наугад значит пообещать рост там, где идёт отсечение.
    action = _raw(_widen()[0][0])
    blind = {**action, "previous_state": {}}
    assert expectation.of(blind, {"added_daily_rub": 400.0,
                                  "cpa_rub": 2_400.0}) is None


def test_without_the_transferred_spend_nothing_is_promised():
    # Курс «рубли → лиды» не выдумывается: без оценки расхода нового региона
    # обещание было бы прогнозом из воздуха, а петля обучения зачла бы его
    # сбывшимся по любому исходу.
    action = _widen(added_daily_rub=None)[0][0]
    assert (action.get("payload") or {}).get("expected_leads_delta") is None


# ------------------------------------------- рельса


def test_the_kind_is_allowed_to_be_applied_and_rolled_back():
    assert geo.GEO_KIND in guardrails.ALLOWED_ACTION_KINDS
    assert geo.GEO_KIND in guardrails.ROLLBACK_ALLOWED_ACTION_KINDS


@pytest.mark.parametrize("action", [_narrow()[0][0], _widen()[0][0]])
def test_both_directions_pass_the_rail(action):
    ok, reason = check_action(action)
    assert ok, reason


def _forged(**over):
    """Действие, собранное МИМО рычага: рельса обязана считать сама."""
    action = _narrow()[0][0]
    payload = {**action["payload"], **over.pop("payload", {})}
    return {**action, "payload": payload, **over}


def test_the_rail_refuses_an_empty_list_of_its_own():
    ok, reason = check_action(_forged(payload={"RegionIds": []}))
    assert not ok and "пуст" in reason


def test_the_rail_refuses_a_move_without_groups():
    ok, reason = check_action(_forged(payload={"AdGroupIds": []}))
    assert not ok and "групп" in reason


def test_the_rail_refuses_a_negative_region():
    ok, reason = check_action(_forged(payload={"RegionIds": [MOSCOW, -SPB]}))
    assert not ok and "минус-регион" in reason


def test_the_rail_refuses_a_move_without_a_known_past():
    ok, reason = check_action(_forged(previous_state={}))
    assert not ok and "прежняя география" in reason


def test_the_rail_refuses_a_mixed_move_of_its_own():
    ok, reason = check_action(_forged(payload={"RegionIds": [MOSCOW, KAZAN]}))
    assert not ok and "два разных утверждения" in reason


def test_the_rail_caps_the_cut_independently_of_the_builder():
    # Рельса считает долю по СВОИМ полям: построитель мог ошибиться, и
    # проверять его его же формулой — не рельса.
    ok, reason = check_action(_forged(payload={"RegionIds": [MOSCOW]}))
    assert not ok and "другая кампания" in reason


def test_the_rail_refuses_a_move_that_changes_nothing():
    ok, reason = check_action(_forged(payload={"RegionIds": list(CURRENT)}))
    assert not ok and "без содержания" in reason


# ------------------------------------------- путь назад


def test_the_rollback_returns_the_whole_previous_list():
    service, method, params = rollback_payload(_narrow()[0][0])
    assert (service, method) == ("adgroups", "update")
    assert [g["Id"] for g in params["AdGroups"]] == list(GROUPS)
    for group in params["AdGroups"]:
        assert sorted(group["RegionIds"]) == sorted(CURRENT)


def test_the_rollback_is_refused_when_the_past_is_unknown():
    # Вслепую не пишем — то же правило, что у корректировки без Id.
    action = {**_narrow()[0][0], "previous_state": {}}
    assert rollback_payload(action) is None


def test_the_rollback_request_passes_the_return_rail():
    # Рельса возврата судит по СОДЕРЖИМОМУ запроса: вид выводится из того, что
    # реально уезжает в кабинет, а не из поля журнала.
    action = _narrow()[0][0]
    service, method, params = rollback_payload(action)
    ok, reason = check_rollback(rollback_guard_form(action, service, method, params))
    assert ok, reason
