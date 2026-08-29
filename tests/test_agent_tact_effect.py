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
    нуля;
  * интервал считается ЧЕСТНО. Пуассоновская ошибка счётчиков лидов занижала
    разброс вдвое (6,7 % против замеренных на проде 13,2 %,
    docs/AGENT-TICK-POWER.md), и на данных БЕЗ эффекта замер выносил уверенный
    вердикт в каждом пятом такте вместо каждого двадцатого. Вердикты такта
    зачитываются в ставки (experiments.WINNING_VERDICTS), то есть агент
    повышал бы ставки по шуму.
"""

import random

import pytest

from sync.agent import experiments, holdout, tact_effect

TACT = "2026-09-01"

TREATED = ("101", "102", "103")
# Заповедник из пяти кампаний, а не из двух: контроль обязан стоять на
# эффективном размере не ниже holdout.MIN_CONTROL_NEFF. Две кампании дают
# Kish n_eff = 2 — по такому «контролю» вычитается не сезон, а история одной
# из них (docs/AGENT-TICK-POWER.md: когорта с n_eff 1,47 при 453 лидах).
HOLDOUT = ("901", "902", "903", "904", "905")


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
    # Заповедник подорожал на 20 %, обработанные подешевели на 40 %: такт
    # отыграл у рынка 60 процентных пунктов, и это его заслуга.
    #
    # Почему эффект такой крупный. Наименьшее, что один такт различает при
    # честном интервале, — 37 % изменения цены лида (docs/AGENT-TICK-POWER.md);
    # прежние 12 п.п. этого теста лежали внутри шума, и «improved» на них
    # означал не заслугу, а заниженный вдвое интервал.
    facts = (_facts(TREATED, base_cpa=1_000.0, obs_cpa=600.0, leads_per_day=40)
             + _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=1_200.0, leads_per_day=40))
    out = _measure(facts)

    assert out["did"] < 0
    assert out["verdict"] == "improved"


def test_a_real_worsening_is_not_hidden_by_the_season():
    # Обратный случай: рынок подешевел, а обработанные подорожали. Замер
    # обязан назвать это ухудшением, а не спрятать за «зато у всех плохо».
    facts = (_facts(TREATED, base_cpa=1_000.0, obs_cpa=1_600.0, leads_per_day=40)
             + _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=900.0, leads_per_day=40))
    out = _measure(facts)

    assert out["did"] > 0
    assert out["verdict"] == "worsened"


def test_a_twelve_point_move_is_no_longer_a_verdict():
    # Ровно тот вход, который до правки читался как «improved»: заповедник
    # +20 %, обработанные +8 %. Разность 12 п.п. меньше разброса самого
    # оценщика на пустом месте (13,2 %), и утверждать по ней нечего.
    facts = (_facts(TREATED, base_cpa=1_000.0, obs_cpa=1_080.0, leads_per_day=40)
             + _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=1_200.0, leads_per_day=40))
    out = _measure(facts)

    assert out["did"] == pytest.approx(-0.12, abs=0.01)
    assert out["verdict"] == "inconclusive"


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


def test_more_leads_make_the_interval_narrower_but_only_to_the_floor():
    # Ширина интервала падает с объёмом ровно до пола: лидов может быть
    # сколько угодно, но цена лида группы за две недели гуляет сама по себе, и
    # никакой счётчик об этом не знает. Прежняя редакция теста требовала
    # только «шире/уже» — и была одинаково верна для интервала, который
    # схлопывается в ноль на больших объёмах.
    thin = (_facts(TREATED, base_cpa=1_000.0, obs_cpa=1_080.0, leads_per_day=1)
            + _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=1_200.0, leads_per_day=1))
    fat = (_facts(TREATED, base_cpa=1_000.0, obs_cpa=1_080.0, leads_per_day=200)
           + _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=1_200.0, leads_per_day=200))

    thin_out, fat_out = _measure(thin), _measure(fat)
    thin_ci, fat_ci = thin_out["ci"], fat_out["ci"]

    assert (fat_ci[1] - fat_ci[0]) < (thin_ci[1] - thin_ci[0])
    assert thin_out["error"]["standard_error"] == thin_out["error"]["poisson"]
    assert (fat_out["error"]["standard_error"]
            == tact_effect.MEASURED_PLACEBO_SIGMA)


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
    facts = (_facts(TREATED, base_cpa=1_000.0, obs_cpa=600.0,
                    leads_per_day=40, horizon=7)
             + _facts(HOLDOUT, base_cpa=1_000.0, obs_cpa=1_200.0,
                      leads_per_day=40, horizon=7))
    out = _measure(facts, horizon_days=7)

    assert out["windows"]["horizon_days"] == 7
    assert out["verdict"] == "improved"


# ------------------------------- честность интервала: пол ошибки из плацебо


def _noisy_history(days=200, leads_per_day=20, sigma=0.30, seed=20260829,
                   start="2026-01-01"):
    """Кабинет, в котором агент не делал НИЧЕГО.

    Цена лида каждой кампании гуляет сама по себе: спрос, аукцион, состав
    трафика. Обе группы живут одним и тем же процессом, поэтому истинный
    эффект любого «такта» здесь равен нулю по построению — ровно так же, как
    в плацебо-замере на прод-данных (docs/AGENT-TICK-POWER.md), где 168 точек
    истории дали разброс 13,2 % при пуассоновской оценке 6,7 %.

    Лидов в день намеренно много: пуассоновская ошибка на таком объёме почти
    нулевая, и всё, что останется в интервале, — честный разброс цены.
    """
    import math
    from datetime import date, timedelta

    rng = random.Random(seed)
    first = date.fromisoformat(start)
    rows = []
    for campaign_id in tuple(TREATED) + tuple(HOLDOUT):
        for i in range(days):
            cpa = 1_000.0 * math.exp(rng.gauss(0.0, sigma))
            rows.append({"campaign_id": campaign_id,
                         "fact_date": (first + timedelta(days=i)).isoformat(),
                         "cost": cpa * leads_per_day,
                         "eff_leads": leads_per_day})
    return rows


def _placebo_tacts(count=40, step=3, offset=60, start="2026-01-01"):
    """Дни такта внутри истории: обе стороны окон в витрине, плацебо позади."""
    from datetime import date, timedelta

    first = date.fromisoformat(start)
    return [(first + timedelta(days=offset + i * step)).isoformat()
            for i in range(count)]


def _naive_verdict(out):
    """Вердикт по ПРЕЖНЕМУ правилу: интервал из одной пуассоновской ошибки.

    Ошибка пересчитывается из счётчиков лидов самого отчёта, а не читается из
    нового поля error: правило воспроизводится буква в букву таким, каким оно
    было до правки, и тест сравнивает два правила, а не правило с самим собой.
    """
    import math

    half = tact_effect.Z_95 * math.sqrt(
        1.0 / max(int(out["treated"]["leads"]), 1)
        + 1.0 / max(int(out["holdout"]["leads"]), 1))
    if out["did"] + half < 0:
        return "improved"
    if out["did"] - half > 0:
        return "worsened"
    return "inconclusive"


def test_noise_alone_does_not_buy_a_verdict():
    # Главная проверка правки. На истории БЕЗ единого действия доля уверенных
    # вердиктов обязана держаться заявленных 5 %: замер, который на пустом
    # месте говорит «improved» в каждом пятом такте, не слаб — он выдаёт шум
    # за результат, а experiments.WINNING_VERDICTS зачитывает такие вердикты
    # в ставки (замер на проде: 20,8 % против заявленных 5 %).
    facts = _noisy_history()
    outs = [tact_effect.measure(tact, facts, HOLDOUT, TREATED)
            for tact in _placebo_tacts()]

    assert all(o["did"] is not None for o in outs), "замер отказал, а не судил"
    confident = [o for o in outs if o["verdict"] in ("improved", "worsened")]
    assert len(confident) / len(outs) <= 0.05

    # И доказательство, что вход был бы обманчив для старого правила: на тех
    # же числах пуассоновский интервал раздаёт вердикты пачками. Без этой
    # половины теста «уверенных нет» означало бы лишь «данные скучные».
    naive = [o for o in outs
             if _naive_verdict(o) in ("improved", "worsened")]
    assert len(naive) / len(outs) > 0.15


def test_the_floor_is_measured_on_the_history_at_hand():
    # Пол считается прогоном по истории (mining.placebo_sigma, поднятый на
    # уровень групп), а не берётся константой: кабинет шумнее замеренного —
    # интервал обязан расшириться вслед за ним, а не остаться на числе,
    # снятом 29.08.2026.
    facts = _noisy_history(sigma=0.9, seed=20260830)
    out = tact_effect.measure(_placebo_tacts(count=1)[0], facts, HOLDOUT, TREATED)

    assert out["error"]["placebo"] > tact_effect.MEASURED_PLACEBO_SIGMA
    assert out["error"]["standard_error"] == out["error"]["placebo"]


def test_without_history_the_floor_is_the_one_measured_on_production():
    # Прогон сторожа держит в памяти ровно два окна такта (load_facts:
    # 2 × 14 дней), и плацебо-точек в них нет. «Не смогли посчитать разброс» —
    # не повод вернуться к вдвое заниженному интервалу.
    out = _measure()

    assert out["error"]["placebo"] is None
    assert out["error"]["standard_error"] == tact_effect.MEASURED_PLACEBO_SIGMA
    assert out["error"]["poisson"] < tact_effect.MEASURED_PLACEBO_SIGMA


def test_the_floor_can_be_computed_once_for_many_tacts():
    # Разбор считает пол один раз на десятки тактов — проход по истории
    # дорогой, а разброс кабинета у них общий. Переданный пол обязан
    # применяться как свой.
    from datetime import date

    facts = _noisy_history()
    floor = tact_effect.placebo_sigma(facts, TREATED, HOLDOUT,
                                      before=date(2026, 7, 1))
    out = tact_effect.measure(_placebo_tacts(count=1)[0], facts, HOLDOUT,
                              TREATED, error_floor=0.5)

    assert floor is not None
    assert out["error"]["standard_error"] == 0.5


# ------------------------------ годность контроля: эффективный размер группы


def test_a_control_carried_by_a_single_campaign_is_unknown():
    # Сумма лидов о годности контроля не говорит: 616 лидов, из них 560 у
    # одной кампании, — это контроль из ОДНОЙ кампании (Kish n_eff 1,2), и
    # вычитается по нему не сезон, а её собственная история. Ровно это
    # случилось с когортой заповедника за два месяца: 453 лида при n_eff 1,47
    # (docs/AGENT-TICK-POWER.md).
    lopsided = (_facts(TREATED, base_cpa=1_000.0, obs_cpa=600.0, leads_per_day=40)
                + _facts(HOLDOUT[:1], base_cpa=1_000.0, obs_cpa=1_200.0,
                         leads_per_day=40)
                + _facts(HOLDOUT[1:], base_cpa=1_000.0, obs_cpa=1_200.0,
                         leads_per_day=1))
    out = _measure(lopsided)

    assert out["holdout"]["leads"] >= holdout.MIN_CONTROL_LEADS
    assert out["verdict"] == "unknown"
    assert "эффективный размер" in out["reason"]


def test_an_evenly_spread_control_passes():
    # Обратная сторона порога: пять кампаний с равным весом дают n_eff 5 и
    # контролем работают. Порог обязан отсекать перекос, а не размер группы.
    out = _measure()

    assert out["holdout"]["n_eff"] == pytest.approx(len(HOLDOUT), abs=0.01)
    assert out["verdict"] != "unknown"


# ------------------------------------------------- шаг 8: врезка в сторожа


def _action(object_id, applied_on):
    return {"object_id": object_id, "applied_at": applied_on,
            "action_kind": "bidmodifier.set"}


def _watchdog_inputs(tact=TACT, horizon=experiments.HORIZON_DAYS):
    """Вход сторожа: факты обработанных по кампаниям и факты заповедника."""
    from datetime import date, timedelta

    treated_rows = _facts(TREATED, base_cpa=1_000.0, obs_cpa=600.0,
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
