from sync.agent.mining import detect_change_points, did_effect, mine_quasi_experiments


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
    # Обработанная улучшилась на 20%, контроль — на 5%. Заслуга = 15 п.п.
    out = did_effect(treated_before=100.0, treated_after=80.0,
                     control_before=100.0, control_after=95.0)
    assert abs(out["effect"] - (-0.15)) < 1e-9


def test_did_returns_none_on_zero_base():
    out = did_effect(0.0, 80.0, 100.0, 95.0)
    assert out["effect"] is None


def test_did_confidence_interval_is_wide_for_quasi():
    out = did_effect(100.0, 80.0, 100.0, 95.0)
    assert out["effect_lo"] < out["effect"] < out["effect_hi"]


def test_mine_emits_class_b_rows():
    facts = []
    for d in range(1, 29):
        for cid, cost in (("111", 1000.0 if d < 15 else 2000.0), ("222", 1000.0)):
            facts.append({
                "fact_date": f"2026-06-{d:02d}",
                "campaign_id": cid,
                "cost": cost,
                "sum_p_pay": 10.0,
            })
    rows = mine_quasi_experiments(facts, window=7)
    assert rows, "изменение бюджета кампании 111 должно быть найдено"
    assert all(r["reliability_class"] == "B" for r in rows)
    assert all(r["source"] == "quasi" for r in rows)
    assert all(r["mechanism"] == "did" for r in rows)
    assert all(r["experiment_id"] for r in rows)


def _quasi_facts(n_control):
    """Кампания 111 удвоила бюджет 15-го; n_control ровных контрольных кампаний.

    Качество контроля меняется ото дня ко дню (sum_p_pay = номер дня), поэтому
    «последние window СТРОК» и «последние window СУТОК» дают разные числа —
    на этом расхождении и ловится подмена окна.

    У обработанной кампании ранняя история (1–7) намеренно другого качества, чем
    окно 8–14: иначе окно можно было бы расширить на всю историю до перелома, и
    ни один тест этого не заметил бы.
    """
    facts = []
    for d in range(1, 29):
        facts.append({"fact_date": f"2026-06-{d:02d}", "campaign_id": "111",
                      "cost": 1000.0 if d < 15 else 2000.0,
                      "sum_p_pay": 40.0 if d < 8 else 10.0})
        for i in range(n_control):
            facts.append({"fact_date": f"2026-06-{d:02d}", "campaign_id": f"{200 + i}",
                          "cost": 100.0, "sum_p_pay": float(d)})
    return facts


def test_control_window_is_measured_in_days_not_in_rows():
    """Контроль обязан покрывать ТЕ ЖЕ сутки, что и обработанная кампания.

    Срез «последние window строк» плоского списка всех кампаний давал контролю
    примерно window/N суток: при 20 кампаниях — треть дня, при 84 — шестую часть.
    """
    rows = mine_quasi_experiments(_quasi_facts(20), window=7)
    assert len(rows) == 1
    # Контроль за те же семь суток: 08–14 → 14000/1540, 15–21 → 14000/2520,
    # то есть −38.89 %. У обработанной +100 %. DiD = 1.3889.
    # Обрезка по строкам брала только 14-е и 15-е числа и давала 1.0667.
    assert abs(rows[0]["effect"] - 1.3889) < 1e-4


def test_control_window_does_not_shrink_as_the_cabinet_grows():
    """Оценка эффекта не может зависеть от числа кампаний в кабинете."""
    few = mine_quasi_experiments(_quasi_facts(5), window=7)
    many = mine_quasi_experiments(_quasi_facts(40), window=7)
    assert few and many
    assert few[0]["effect"] == many[0]["effect"]


def test_mine_returns_empty_on_flat_history():
    facts = [{"fact_date": f"2026-06-{d:02d}", "campaign_id": "111", "cost": 1000.0, "sum_p_pay": 10.0}
             for d in range(1, 29)]
    assert mine_quasi_experiments(facts, window=7) == []
