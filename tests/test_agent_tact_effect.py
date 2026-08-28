# -*- coding: utf-8 -*-
"""
tests/test_agent_tact_effect.py — эффект ТАКТА ЦЕЛИКОМ против заповедника
(sync/agent/tact_effect.py, задача 25 плана беты).

Зачем нужен замер уровня такта. Индивидуальный DiD сторожа судит одно
действие по одной кампании, и на четырёхстах одновременных правках он
зашумлён общим сдвигом кабинета: у каждой кампании свой контроль слишком мал,
а сезон, аукцион и рынок двигают их все разом. Такт — это одно решение
системы, и спрашивать с него надо тоже одним числом.

Что здесь проверяется:

  * сезон вычтен. Такт, поднявший цену ровно настолько, насколько подорожал
    весь рынок, эффекта не имеет — и обязан показать ноль, а не «стало хуже»;
  * молчание не выдаётся за успех. Пустой заповедник, слишком мелкий
    заповедник, отсутствие применённых действий — вердикт «unknown» с
    названной причиной. Иначе первый же такт без контроля отчитается победой;
  * интервал считается, а не подразумевается. Вердикт «улучшил» законен
    только тогда, когда весь доверительный интервал лежит по одну сторону
    нуля: точечная оценка на шумных данных всегда чем-то да отличается от
    нуля.
"""

import pytest

from sync.agent import experiments, holdout, tact_effect

TACT = "2026-09-01"

TREATED = ("101", "102", "103")
HOLDOUT = ("901", "902")


def _days(start, count):
    """Список дат-строк: count дней подряд, начиная со start (ISO)."""
    from datetime import date, timedelta

    first = date.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


def _facts(ids, *, base_cpa, obs_cpa, leads_per_day,
           horizon=experiments.HORIZON_DAYS, tact=TACT):
    """Дневные факты группы кампаний: своя цена лида до такта и после.

    Форма строки — та же, что отдаёт витрина сторожу
    (db.load_daily_facts: campaign_id, fact_date, cost, eff_leads); своей
    формы у замера нет и быть не должно, иначе он мерил бы не те данные,
    которыми живёт остальной агент.
    """
    from datetime import date, timedelta

    tact_day = date.fromisoformat(tact)
    before = [(tact_day - timedelta(days=horizon - i)).isoformat()
              for i in range(horizon)]
    after = _days(tact, horizon)

    rows = []
    for campaign_id in ids:
        for day in before:
            rows.append({"campaign_id": campaign_id, "fact_date": day,
                         "cost": base_cpa * leads_per_day,
                         "eff_leads": leads_per_day})
        for day in after:
            rows.append({"campaign_id": campaign_id, "fact_date": day,
                         "cost": obs_cpa * leads_per_day,
                         "eff_leads": leads_per_day})
    return rows


def _seasonal_lift():
    """Такт, не изменивший ничего: подорожали одинаково обе группы на 20 %."""
    return (_facts(TREATED, base_cpa=1_000.0, obs_cpa=1_200.0, leads_per_day=10)
            + _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=1_200.0, leads_per_day=10))


def _measure(facts=None, holdout_ids=HOLDOUT, applied=TREATED, **over):
    return tact_effect.measure(TACT, facts if facts is not None else _seasonal_lift(),
                               holdout_ids, applied, **over)


# ------------------------------------------------- шаг 1: сезон вычитается


def test_tact_effect_is_did_against_holdout():
    # Шаг 1 задачи 25. Обе группы подорожали на 20 %: такт не сделал ничего,
    # и «было/стало» показало бы провал на ровном месте.
    out = _measure()
    assert out["did"] == pytest.approx(0.0, abs=0.02), "сезон не вычтен"


def test_before_after_would_have_blamed_the_tact():
    # Тот же вход глазами замера без контроля: обработанные подорожали на
    # 20 %. Именно эту цифру и вычитает DiD — тест держит смысл первого.
    out = _measure()
    assert out["treated_delta"] == pytest.approx(0.20, abs=0.01)
    assert out["holdout_delta"] == pytest.approx(0.20, abs=0.01)


def test_a_real_improvement_survives_the_season():
    # Заповедник подорожал на 20 %, обработанные — только на 8 %: такт
    # отыграл 12 процентных пунктов у рынка, и это его заслуга.
    facts = (_facts(TREATED, base_cpa=1_000.0, obs_cpa=1_080.0, leads_per_day=40)
             + _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=1_200.0, leads_per_day=40))
    out = _measure(facts)

    assert out["did"] < 0
    assert out["verdict"] == "improved"


def test_a_real_worsening_is_not_hidden_by_the_season():
    # Обратный случай: рынок подешевел, а обработанные подорожали. Замер
    # обязан назвать это ухудшением, а не спрятать за «зато у всех плохо».
    facts = (_facts(TREATED, base_cpa=1_000.0, obs_cpa=1_400.0, leads_per_day=40)
             + _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=900.0, leads_per_day=40))
    out = _measure(facts)

    assert out["did"] > 0
    assert out["verdict"] == "worsened"


# ---------------------------------------- шаг 2: пустой заповедник — unknown


def test_empty_holdout_is_unknown_not_success():
    # Шаг 2. Без контроля вычитать нечего, и «сезон равен нулю» — не
    # умолчание, а утверждение, которого никто не проверял.
    out = _measure(holdout_ids=())

    assert out["verdict"] == "unknown"
    assert out["did"] is None
    assert "заповедник" in out["reason"]


def test_a_holdout_too_small_to_be_a_control_is_unknown():
    # Порог тот же, что у контроля одного действия (holdout.MIN_CONTROL_LEADS):
    # на десятке лидов цена — шум, и вычтенный по такому контролю «сезон»
    # добавил бы к оценке случайное число вместо поправки.
    thin = (_facts(TREATED, base_cpa=1_000.0, obs_cpa=1_080.0, leads_per_day=40)
            + _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=1_200.0, leads_per_day=0))
    out = _measure(thin)

    assert out["verdict"] == "unknown"
    assert str(holdout.MIN_CONTROL_LEADS) in out["reason"]


def test_the_control_threshold_is_the_watchdog_one():
    # Второй копии порога быть не должно: сторож и замер такта обязаны
    # называть контролем одно и то же.
    from sync import agent_e1_watchdog

    assert agent_e1_watchdog.MIN_CONTROL_LEADS is holdout.MIN_CONTROL_LEADS


# ------------------------------------- шаг 3: такт без действий не заявляет


def test_a_tact_without_applied_actions_claims_nothing():
    # Шаг 3. Кабинет двигается сам по себе каждый день. Такт, не применивший
    # ничего, обязан молчать — иначе агент припишет себе чужое движение, и
    # припишет тем охотнее, чем сильнее оно в его пользу.
    out = _measure(applied=())

    assert out["verdict"] == "unknown"
    assert out["did"] is None
    assert "не применил" in out["reason"]


def test_holdout_campaigns_are_never_treated():
    # Кампания заповедника, попавшая в список применённых, — это дефект
    # отбора (agent_e1 отсекает такие действия check_holdout). Замер обязан
    # исключить её из обработанных, а не мерить заповедник против себя.
    out = _measure(applied=tuple(TREATED) + tuple(HOLDOUT))

    assert set(out["treated"]["campaigns"]) == set(TREATED)
    assert out["did"] == pytest.approx(0.0, abs=0.02)


def test_treated_campaigns_without_facts_are_not_invented():
    # Применённые действия есть, а фактов по ним нет: витрина не доехала.
    # Ноль лидов — не «эффекта нет», а «мерить нечем».
    only_holdout = _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=1_200.0,
                          leads_per_day=40)
    out = _measure(only_holdout)

    assert out["verdict"] == "unknown"
    assert out["did"] is None


# ---------------------------------------------- шаг 4: интервал считается


def test_confidence_interval_is_computed_not_implied():
    # Шаг 4. Интервал — не украшение отчёта: именно он отличает «такт
    # улучшил» от «числа разошлись на шуме».
    out = _measure()
    low, high = out["ci"]

    assert low < out["did"] < high
    assert low == pytest.approx(out["did"] - (high - out["did"]), abs=1e-9)


def test_more_leads_make_the_interval_narrower():
    # Ширина интервала обязана падать с объёмом: иначе это константа, и
    # вердикт по ней ничего не значит.
    thin = (_facts(TREATED, base_cpa=1_000.0, obs_cpa=1_080.0, leads_per_day=2)
            + _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=1_200.0, leads_per_day=2))
    fat = (_facts(TREATED, base_cpa=1_000.0, obs_cpa=1_080.0, leads_per_day=200)
           + _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=1_200.0, leads_per_day=200))

    thin_ci = _measure(thin)["ci"]
    fat_ci = _measure(fat)["ci"]

    assert (fat_ci[1] - fat_ci[0]) < (thin_ci[1] - thin_ci[0])


def test_a_difference_inside_the_noise_is_inconclusive():
    # Оценка отличается от нуля, но интервал накрывает ноль: ответ не куплен.
    # Записать такое победой значит наполнить контур решений шумом.
    noisy = (_facts(TREATED, base_cpa=1_000.0, obs_cpa=1_100.0, leads_per_day=2)
             + _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=1_200.0, leads_per_day=2))
    out = _measure(noisy)

    assert out["did"] != 0.0
    assert out["ci"][0] < 0 < out["ci"][1]
    assert out["verdict"] == "inconclusive"


def test_verdicts_are_the_watchdog_vocabulary():
    # Словарь исходов один на весь агент: реестр гипотез считает победой
    # только «improved» (experiments.WINNING_VERDICTS), и второе слово для
    # того же исхода означало бы, что часть тактов не зачтётся никогда.
    assert tact_effect.VERDICTS == ("improved", "worsened", "inconclusive",
                                    "unknown")
    assert experiments.WINNING_VERDICTS <= set(tact_effect.VERDICTS)


# --------------------------------------------------------- окна и границы


def test_windows_are_symmetric_around_the_tact():
    # База и наблюдение равной длины: разные окна сравнивали бы разное
    # количество дней недели, а у образовательного спроса будни и выходные
    # различаются кратно.
    out = _measure()
    windows = out["windows"]

    assert windows["baseline"][1] < TACT <= windows["observation"][0]
    assert windows["horizon_days"] == experiments.HORIZON_DAYS


def test_horizon_comes_from_the_bet_registry():
    # Горизонт назначает ставка (experiments.HORIZON_DAYS), и второй
    # константы быть не должно: такт судится тем же сроком, что и его
    # действия.
    assert tact_effect.DEFAULT_HORIZON_DAYS is experiments.HORIZON_DAYS


def test_a_shorter_horizon_moves_both_windows():
    # Горизонт — параметр, а не константа замера: разбор может посмотреть
    # такт короче, но обе стороны обязаны поехать вместе.
    facts = (_facts(TREATED, base_cpa=1_000.0, obs_cpa=1_080.0,
                    leads_per_day=40, horizon=7)
             + _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=1_200.0,
                      leads_per_day=40, horizon=7))
    out = _measure(facts, horizon_days=7)

    assert out["windows"]["horizon_days"] == 7
    assert out["verdict"] == "improved"


# ------------------------------------------------- шаг 8: врезка в сторожа


def _action(object_id, applied_on):
    return {"object_id": object_id, "applied_at": applied_on,
            "action_kind": "bidmodifier.set"}


def _watchdog_inputs(tact=TACT, horizon=experiments.HORIZON_DAYS):
    """Вход сторожа: факты обработанных по кампаниям и факты заповедника."""
    from datetime import date, timedelta

    treated_rows = _facts(TREATED, base_cpa=1_000.0, obs_cpa=1_080.0,
                          leads_per_day=40, horizon=horizon, tact=tact)
    by_campaign = {}
    for row in treated_rows:
        by_campaign.setdefault(row["campaign_id"], []).append(row)
    holdout_rows = _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=1_200.0,
                          leads_per_day=40, horizon=horizon, tact=tact)
    today = date.fromisoformat(tact) + timedelta(days=horizon)
    return by_campaign, holdout_rows, today


def test_the_watchdog_measures_the_tact_whose_horizon_closed():
    # Врезка шага 8. Сторож считает эффект такта, применённого ровно горизонт
    # назад: это единственный такт, у которого окно наблюдения закрылось
    # целиком, и мерить его раньше значило бы судить по неполному окну.
    from datetime import timedelta

    from sync import agent_e1_watchdog as watchdog

    by_campaign, holdout_rows, today = _watchdog_inputs()
    tact_day = (today - timedelta(
        days=watchdog.OBSERVATION_HORIZON_DAYS)).isoformat()
    actions = [_action(cid, tact_day) for cid in TREATED]

    out = watchdog.tact_effect_report(actions, by_campaign, holdout_rows,
                                      set(HOLDOUT), today)

    assert out["tact_date"] == tact_day
    assert out["verdict"] == "improved"


def test_actions_of_another_day_are_not_this_tact():
    # Такт определяется днём ПРИМЕНЕНИЯ. Действие, уехавшее в кабинет вчера,
    # работает во вчерашнем такте, и приписывать его сегодняшнему замеру
    # значило бы мерить смесь двух решений одним числом.
    from datetime import timedelta

    from sync import agent_e1_watchdog as watchdog

    by_campaign, holdout_rows, today = _watchdog_inputs()
    other_day = (today - timedelta(
        days=watchdog.OBSERVATION_HORIZON_DAYS - 1)).isoformat()
    actions = [_action(cid, other_day) for cid in TREATED]

    out = watchdog.tact_effect_report(actions, by_campaign, holdout_rows,
                                      set(HOLDOUT), today)

    assert out["verdict"] == "unknown"
    assert "не применил" in out["reason"]
