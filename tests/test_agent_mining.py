import math

import sync.agent.mining as mining
from sync.agent.mining import (
    detect_change_points,
    did_effect,
    did_rel_error,
    mine_quasi_experiments,
)


def test_detects_step_change():
    series = [{"date": f"2026-06-{d:02d}", "value": 1000.0} for d in range(1, 15)]
    series += [{"date": f"2026-06-{d:02d}", "value": 2000.0} for d in range(15, 29)]
    points = detect_change_points(series, min_jump=0.3)
    assert len(points) == 1
    assert points[0]["date"] == "2026-06-15"


def test_ignores_noise_below_threshold():
    series = [{"date": f"2026-06-{d:02d}", "value": 1000.0 + (d % 3) * 10} for d in range(1, 29)]
    assert detect_change_points(series, min_jump=0.3) == []


def test_ignores_change_without_enough_history():
    series = [{"date": "2026-06-01", "value": 100.0}, {"date": "2026-06-02", "value": 900.0}]
    assert detect_change_points(series, min_jump=0.3) == []


def test_did_subtracts_control_movement():
    # Обработанная улучшилась на 20%, контроль — на 5%. Заслуга — разность
    # ЛОГАРИФМОВ (шкала эластичности): ln(0.8) − ln(0.95) = −0.1719.
    out = did_effect(treated_before=100.0, treated_after=80.0,
                     control_before=100.0, control_after=95.0, rel_error=0.1)
    assert abs(out["effect"] - (math.log(0.8) - math.log(0.95))) < 1e-4


def test_did_returns_none_on_zero_base():
    out = did_effect(0.0, 80.0, 100.0, 95.0, rel_error=0.1)
    assert out["effect"] is None


def test_did_interval_comes_from_lead_counts_not_from_effect_size():
    # Прежний интервал был долей от эффекта: нулевой эффект получал почти
    # нулевой интервал независимо от объёма данных. Теперь ширина задаётся
    # счётчиками лидов и одинакова для большого и нулевого эффекта.
    rel = did_rel_error(25, 25, 1000, 1000)
    wide = did_effect(100.0, 100.0, 100.0, 100.0, rel)
    assert abs((wide["effect_hi"] - wide["effect_lo"]) / 2 - rel) < 1e-4


def test_rel_error_is_driven_by_smallest_window():
    # Контроль с тысячами лидов почти не добавляет ошибки — её задают окна
    # обработанной кампании.
    assert did_rel_error(25, 25, 10000, 10000) < did_rel_error(25, 25, 30, 30)
    assert did_rel_error(400, 400, 10000, 10000) < did_rel_error(25, 25, 10000, 10000)


def test_zero_lead_window_counts_as_one_event_upper_bound():
    assert did_rel_error(0, 10, 10, 10) == did_rel_error(1, 10, 10, 10)


def _facts_row(day, campaign_id, cost, eff_leads):
    return {"fact_date": f"2026-06-{day:02d}", "campaign_id": campaign_id,
            "cost": cost, "eff_leads": eff_leads}


def test_mine_emits_class_b_rows_in_eff_cpl_currency():
    facts = []
    for d in range(1, 29):
        facts.append(_facts_row(d, "111", 1000.0 if d < 15 else 2000.0, 5))
        facts.append(_facts_row(d, "222", 1000.0, 5))
    rows = mine_quasi_experiments(facts, window=7)
    assert rows, "изменение бюджета кампании 111 должно быть найдено"
    assert all(r["reliability_class"] == "B" for r in rows)
    assert all(r["source"] == "quasi" for r in rows)
    assert all(r["mechanism"] == "did" for r in rows)
    # Валюта — цена эффективного лида, не p_pay: Э2.0 закрыл sum_p_pay
    # для агрегатных решений.
    assert all(r["metric"] == "eff_cpl" for r in rows)
    assert all(r["params"]["rel_error"] > 0 for r in rows)
    assert all(set(r["params"]["leads"]) == {
        "treated_before", "treated_after", "control_before", "control_after",
    } for r in rows)


def test_mine_skips_change_with_leadless_window():
    # После скачка лидов нет вовсе — конечной цены лида не существует,
    # эксперимент не эмитится (а не эмитится с нулём в знаменателе).
    facts = []
    for d in range(1, 29):
        facts.append(_facts_row(d, "111", 1000.0 if d < 15 else 2000.0,
                                5 if d < 15 else 0))
        facts.append(_facts_row(d, "222", 1000.0, 5))
    assert mine_quasi_experiments(facts, window=7) == []


def _quasi_facts(n_control):
    """Кампания 111 удвоила бюджет 15-го; n_control ровных контрольных кампаний.

    Цена лида контроля меняется ото дня ко дню (лидов = номер дня), поэтому
    «последние window СТРОК» и «последние window СУТОК» дают разные числа —
    на этом расхождении и ловится подмена окна.

    У обработанной кампании ранняя история (1–7) намеренно другого качества,
    чем окно 8–14: иначе окно можно было бы расширить на всю историю до
    перелома, и ни один тест этого не заметил бы.
    """
    facts = []
    for d in range(1, 29):
        facts.append(_facts_row(d, "111", 1000.0 if d < 15 else 2000.0,
                                40 if d < 8 else 10))
        for i in range(n_control):
            facts.append(_facts_row(d, f"{200 + i}", 100.0, d))
    return facts


def test_control_window_is_measured_in_days_not_in_rows():
    """Контроль обязан покрывать ТЕ ЖЕ сутки, что и обработанная кампания.

    Срез «последние window строк» плоского списка всех кампаний давал контролю
    примерно window/N суток: при 20 кампаниях — треть дня, при 84 — шестую часть.
    """
    rows = mine_quasi_experiments(_quasi_facts(20), window=7)
    assert len(rows) == 1
    # Обработанная: CPL 7000/70 → 14000/70, +100 %. Контроль за те же сутки:
    # дни 08–14 CPL = 14000/(77·20)·20 = 700/77, дни 15–21 = 700/126,
    # то есть −38.89 %. DiD = 1.0 − (−0.3889) = 1.3889.
    assert abs(rows[0]["effect"] - 1.1856) < 1e-4   # шкала логарифмическая


def test_control_window_does_not_shrink_as_the_cabinet_grows():
    """Оценка эффекта не может зависеть от числа кампаний в кабинете."""
    few = mine_quasi_experiments(_quasi_facts(5), window=7)
    many = mine_quasi_experiments(_quasi_facts(40), window=7)
    assert few and many
    assert few[0]["effect"] == many[0]["effect"]


def test_mine_returns_empty_on_flat_history():
    facts = [_facts_row(d, "111", 1000.0, 5) for d in range(1, 29)]
    assert mine_quasi_experiments(facts, window=7) == []


# ------------------------- DiD в логарифмической шкале


def test_did_effect_is_measured_in_logs():
    # Эффект уходит в эластичность делением на ЛОГАРИФМ скачка бюджета
    # (history.elasticity, saturation), поэтому и сам обязан быть логарифмом
    # отношения — иначе на крупных скачках делится арифметическая величина на
    # логарифмическую и |eps| раздут. На малых изменениях обе шкалы совпадают.
    out = did_effect(treated_before=100.0, treated_after=50.0,
                     control_before=100.0, control_after=100.0, rel_error=0.1)
    assert abs(out["effect"] - math.log(0.5)) < 1e-4

    small = did_effect(100.0, 99.0, 100.0, 100.0, rel_error=0.1)
    assert abs(small["effect"] - (-0.01)) < 1e-3   # ≈ арифметическая шкала


def test_did_returns_none_when_a_window_has_zero_level():
    # Логарифм нуля не существует: нулевая цена лида в любом из четырёх окон
    # делает оценку невозможной, а не «эффект −100 %».
    assert did_effect(100.0, 0.0, 100.0, 95.0, rel_error=0.1)["effect"] is None
    assert did_effect(100.0, 80.0, 100.0, 0.0, rel_error=0.1)["effect"] is None


# ------------------------- C5: возврат к среднему (RTM) в предыстории


def test_change_after_cpl_spike_is_declassed_as_rtm_suspect():
    # Бюджет режут ПОСЛЕ всплеска цены лида, а всплеск и сам откатывается к
    # среднему. DiD припишет это возвращение заслуге правки: «срезали бюджет
    # → CPL упал → кампания насыщалась». Такой скачок не даёт права двигать
    # деньги — класс надёжности понижается, и в эластичность он не идёт.
    facts = []
    for d in range(1, 29):
        if d < 8:
            cost, leads = 1000.0, 20        # спокойная база: CPL 50
        elif d < 15:
            cost, leads = 1000.0, 5         # всплеск CPL 200 — на него и реагируют
        else:
            cost, leads = 500.0, 10         # бюджет срезали, CPL вернулся к 50
        facts.append(_facts_row(d, "111", cost, leads))
        facts.append(_facts_row(d, "222", 1000.0, 20))
    rows = mine_quasi_experiments(facts, window=7)
    assert rows, "скачок обязан быть найден — деклассируется, а не прячется"
    assert all(r["reliability_class"] == "C" for r in rows)
    assert all(r["params"]["pre_trend"]["rtm_suspect"] is True for r in rows)


def test_change_on_a_calm_history_keeps_class_b():
    facts = []
    for d in range(1, 29):
        facts.append(_facts_row(d, "111", 1000.0 if d < 15 else 2000.0, 5))
        facts.append(_facts_row(d, "222", 1000.0, 5))
    rows = mine_quasi_experiments(facts, window=7)
    assert all(r["reliability_class"] == "B" for r in rows)
    assert all(r["params"]["pre_trend"]["rtm_suspect"] is False for r in rows)


def test_rtm_suspect_experiment_is_not_used_for_elasticity():
    from sync.agent.history import elasticity
    suspect = {"effect": -0.5, "reliability_class": "C",
               "params": {"before": 1000.0, "after": 500.0, "rel_error": 0.2}}
    trusted = {**suspect, "reliability_class": "B"}
    assert elasticity(suspect) is None
    assert elasticity(trusted) is not None


# ------------------- placebo-DiD: эмпирический пол ошибки


def _noisy_facts(seed_shift=0):
    """Кампании без единого изменения — только естественные колебания."""
    facts = []
    wobble = [0, 2, -1, 3, -2, 1, 4, -3, 2, 0, 1, -1, 3, -2,
              2, -1, 0, 4, -3, 1, 2, -2, 3, 0, 1, -1, 2, -2]
    for i, day in enumerate(range(1, 29)):
        facts.append(_facts_row(day, "111", 1000.0,
                                20 + wobble[(i + seed_shift) % len(wobble)]))
        facts.append(_facts_row(day, "222", 1000.0,
                                20 - wobble[(i + seed_shift) % len(wobble)]))
    return facts


def test_placebo_sigma_measures_noise_where_nothing_happened():
    # DiD в точках БЕЗ изменений обязан давать ноль. Разброс этих «эффектов
    # на пустом месте» и есть честный пол ошибки: пуассоновский счёт лидов
    # его недооценивает, потому что не знает ни о сезоне, ни о конкурентах.
    sigma = mining.placebo_sigma(_noisy_facts(), window=7)
    assert sigma is not None and sigma > 0


def test_placebo_sigma_is_none_without_enough_points():
    assert mining.placebo_sigma([], window=7) is None


def test_experiment_error_is_never_below_the_placebo_floor():
    facts = []
    for d in range(1, 29):
        facts.append(_facts_row(d, "111", 1000.0 if d < 15 else 2000.0,
                                5 if d % 2 else 9))
        facts.append(_facts_row(d, "222", 1000.0, 7 if d % 3 else 12))
    rows = mine_quasi_experiments(facts, window=7)
    assert rows
    for r in rows:
        floor = r["params"].get("placebo_sigma")
        assert floor is not None
        assert r["params"]["rel_error"] >= floor - 1e-9


def test_precomputed_placebo_floor_is_not_recomputed(monkeypatch):
    # Плацебо-проход дорогой (DiD по всем кампаниям и точкам). Прогон Э0
    # считает его один раз и передаёт обоим потребителям — квазиэкспериментам
    # и парам недель.
    monkeypatch.setattr(mining, "placebo_sigma", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("пол ошибки уже посчитан — пересчитывать нельзя")))
    facts = []
    for d in range(1, 29):
        facts.append(_facts_row(d, "111", 1000.0 if d < 15 else 2000.0, 5))
        facts.append(_facts_row(d, "222", 1000.0, 5))
    rows = mine_quasi_experiments(facts, window=7, error_floor=0.33)
    assert rows and all(r["params"]["placebo_sigma"] == 0.33 for r in rows)
