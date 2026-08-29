# -*- coding: utf-8 -*-
"""
Панель настроек ДЕЙСТВУЕТ, а не только объявлена.

Замер 26.08.2026: из десяти параметров панели к рычагам были подключены три,
и все три — в такте расчёта. Семь остальных не встречались нигде, кроме
самого config.py: решали зашитые константы модулей. Хуже прочих молчал
autonomy — «только предлагать» и «не работать» были надписями, и остановить
агента настройкой было нельзя.

Каждый тест проверяет ОДНУ ручку по последствию, а не по наличию проброса:
настройка, которую видно в сигнатуре, но не в поведении, — ровно тот случай,
который эти тесты и ловят.
"""

import pytest

from sync.agent import confidence
from sync.agent.writer import budget as writer_budget
from sync.agent.writer import switch as writer_switch


# --- пороги уверенности --------------------------------------------------

def test_thresholds_default_to_code_constants():
    """Пустая панель = поведение до появления панели."""
    assert confidence.thresholds_from_config(None) == {}
    assert confidence.thresholds_from_config({}) == {}
    for action_class, spec in confidence.ACTION_CLASSES.items():
        assert confidence.min_p_sign(action_class) == pytest.approx(
            spec["min_p_sign"])


def test_panel_threshold_changes_the_verdict():
    """Та же уверенность, разные пороги — разный вердикт.

    Отношение и ошибка подобраны так, чтобы p_sign лёг МЕЖДУ порогом класса
    (0.90) и настроенным (0.85): иначе тест прошёл бы и на неподключённой
    настройке.
    """
    ratio, rel = 1.30, 0.23
    p = confidence.p_sign(ratio, rel)
    assert 0.85 < p < 0.90, f"выборка теста устарела: p_sign={p}"

    strict = confidence.assess(ratio, rel, "budget_shift")
    assert strict["confident"] is False
    assert strict["min_p_sign"] == pytest.approx(0.90)

    loose = confidence.assess(ratio, rel, "budget_shift",
                              confidence.thresholds_from_config(
                                  {"p_sign_budget": 0.85}))
    assert loose["confident"] is True
    # Порог в ответе — ДЕЙСТВУЮЩИЙ: по нему человек читает отказ в отчёте.
    assert loose["min_p_sign"] == pytest.approx(0.85)


def test_every_panel_threshold_key_reaches_some_action_class():
    """Ключ панели, не привязанный ни к одному классу, — снова та же ловушка."""
    from sync.agent import config as agent_config

    panel_keys = {k for k in agent_config.SPEC if k.startswith("p_sign_")}
    assert panel_keys == set(confidence.CONFIG_THRESHOLD_KEYS.values())
    assert set(confidence.CONFIG_THRESHOLD_KEYS) == set(confidence.ACTION_CLASSES)


# --- разведка в такте записи ---------------------------------------------

M = 1_000_000


def _rows(*, target, current, roi, rel, exploration_rub=None):
    rows = [
        {"setting_kind": "budget_target", "setting_key": "target_28d",
         "value": target, "raw_value": current, "support_n": 100,
         "rel_error": rel},
        {"setting_kind": "budget_target", "setting_key": "roi_vs_lambda",
         "value": roi, "raw_value": 1.0, "support_n": 100, "rel_error": rel},
    ]
    if exploration_rub is not None:
        rows.append({"setting_kind": "budget_target",
                     "setting_key": "exploration_rub",
                     "value": exploration_rub, "raw_value": current,
                     "support_n": 100, "rel_error": rel})
    return rows


def test_exploration_shift_passes_where_an_ordinary_one_is_refused():
    """Одни и те же числа: обычный сдвиг отсекается, разведочный проходит.

    Разница ровно в строке exploration_rub. Уверенности у разведки нет по
    построению — карман делится пропорционально незнанию, — и требовать её
    значит не иметь разведки вовсе.
    """
    shared = dict(target=130_000.0, current=100_000.0, roi=1.30, rel=0.23)

    plain = writer_budget.plan_budget_moves({"111": _rows(**shared)})
    assert plain["desired"] == {}
    assert len(plain["low_confidence"]) == 1
    assert plain["exploration"] == []

    explored = writer_budget.plan_budget_moves(
        {"111": _rows(**shared, exploration_rub=12_000.0)})
    assert "111" in explored["desired"]
    assert explored["desired"]["111"]["exploration"] is True
    assert explored["desired"]["111"]["exploration_rub"] == pytest.approx(12_000.0)
    # Отказ по уверенности заменён пометкой, а не спрятан: разведочные
    # применения обязаны быть отличимы от обычных в журнале.
    assert explored["low_confidence"] == []
    assert len(explored["exploration"]) == 1
    assert "разведка" in explored["exploration"][0]["reason"]


def test_exploration_without_roi_row_is_still_planned():
    """У разведки нет и предмета отказа «уверенность неизвестна».

    Обычный сдвиг без строки отношения к λ судить нечем — отказ. Основание
    разведочного лежит не в отношении к λ, а в незнании, и отсутствие
    экономической оценки его не отменяет.
    """
    rows = [r for r in _rows(target=130_000.0, current=100_000.0, roi=1.3,
                             rel=0.23, exploration_rub=12_000.0)
            if r["setting_key"] != "roi_vs_lambda"]
    plan = writer_budget.plan_budget_moves({"111": rows})
    assert "111" in plan["desired"]
    assert plan["confidence_unknown"] == 0
    assert plan["desired"]["111"]["p_sign"] is None


def test_exploration_still_obeys_the_write_step_cap():
    """Разведка — право не знать, а не право не отвечать.

    Кап шага записи применяется к разведочному сдвигу ровно так же: снят
    ОДИН гейт (уверенность), остальные рельсы на месте.
    """
    desired = {"111": {"target_28d": 400_000.0, "cost_28d": 100_000.0,
                       "ratio": 4.0, "roi_vs_lambda": 1.3, "p_sign": None,
                       "exploration": True, "exploration_rub": 50_000.0}}
    state = {"111": {
        "campaign_id": "111", "campaign_type": "TEXT_CAMPAIGN",
        "strategy": {
            "Search": {"BiddingStrategyType": "AVERAGE_CPA",
                       "AverageCpa": {"AverageCpa": 3000 * M, "GoalId": 42,
                                      "WeeklySpendLimit": 20_000 * M}},
            "Network": {"BiddingStrategyType": "SERVING_OFF"}},
    }}
    actions, _refused = writer_budget.diff_budget(
        desired, state, {"111": 20_000.0}, max_write_step=0.20)
    assert len(actions) == 1
    limit = actions[0]["payload"]["BiddingStrategy"]["Search"]["AverageCpa"][
        "WeeklySpendLimit"]
    # ×4 от расхода запрошено, +20 % разрешено: в кабинет уходит потолок шага.
    assert limit == 24_000 * M


def test_exploration_counts_as_a_growth_address():
    """Разведочные рубли — назначенные деньги, а не свободные.

    Гейт баланса такта снимает сокращения, когда освобождённому не нашлось
    адресата. Разведочная доливка адресатом ЯВЛЯЕТСЯ: иначе агент, который
    режет и одновременно разведывает, блокировал бы сам себя.
    """
    from sync.agent.balance import balance_inputs, tact_balance

    actions = [
        {"action_kind": writer_budget.BUDGET_KIND, "object_id": "111",
         "idempotency_key": "k1", "payload": {}},
        {"action_kind": "campaign.suspend", "object_id": "222",
         "idempotency_key": "k2", "payload": {}},
    ]
    moves = {"111": {"cost_28d": 100_000.0, "target_28d": 150_000.0,
                     "exploration": True, "exploration_rub": 50_000.0}}
    balance = tact_balance(**balance_inputs(
        actions, moves, {"222": 50_000.0}, {}))
    assert balance["added_rub"] == pytest.approx(50_000.0)
    assert balance["freed_rub"] == pytest.approx(50_000.0)
    assert balance["assigned_share"] == pytest.approx(1.0)
    assert balance["shrinking"] is False


# --- доля разведки в солвере ---------------------------------------------

def _curve(cost=100_000.0, leads=100, beta=0.8, marginal=1250.0,
           direction="spo", rel=0.05):
    return {"direction": direction, "cost_28d": cost, "leads_28d": leads,
            "beta": beta, "marginal_cpl": marginal, "marginal_rel_error": rel}


def _ladder(revenue=1_000_000.0, eff=100, rel=0.1):
    return {"expected_revenue": revenue, "events_by_step": {"eff": eff},
            "rel_error": rel}


def test_explore_share_is_a_parameter_not_a_constant():
    """Две доли — два разных кармана. До 25.08 солвер считал по константе.

    Настройка была видна в отчёте и в сигнатуре, но карман считался от зашитой
    EXPLORATION_SHARE: панель показывала одно, механизм делал другое.
    """
    from sync.agent.portfolio import portfolio_targets

    saturation = {"1": _curve(), "2": _curve()}
    ladder = {"1": _ladder(revenue=800_000.0), "2": _ladder(revenue=300_000.0)}
    logins = {"1": "acc", "2": "acc"}

    small = portfolio_targets(saturation, ladder, logins, explore_share=0.02)
    big = portfolio_targets(saturation, ladder, logins, explore_share=0.10)
    small_rub = small["accounts"]["acc"]["exploration"]["rub"]
    big_rub = big["accounts"]["acc"]["exploration"]["rub"]
    assert big_rub > small_rub > 0, (small_rub, big_rub)
    # Инвариант портфеля не сломан ни при одной доле: карман берётся ИЗ
    # бюджета кабинета, а не сверх него.
    for section in (small, big):
        assert abs(section["accounts"]["acc"]["sum_residual"]) < 1.0


def test_report_tells_the_setting_apart_from_what_was_actually_given():
    """Заявленная доля и выданная — двумя числами.

    Они расходятся законно: карман берётся из запаса до пола капа шага, а
    раздаётся до адресного потолка кампании. Одно число не отличило бы «в
    панели 10 %» от «раздать удалось 4 %», и молчание рычага читалось бы как
    выполненная настройка.
    """
    from sync.agent.portfolio import portfolio_targets

    saturation = {"1": _curve(), "2": _curve()}
    ladder = {"1": _ladder(revenue=800_000.0), "2": _ladder(revenue=300_000.0)}
    section = portfolio_targets(saturation, ladder, {"1": "acc", "2": "acc"},
                                explore_share=0.10)["accounts"]["acc"]
    assert section["exploration"]["share"] == pytest.approx(0.10)
    assert 0 < section["exploration"]["share_effective"] <= 0.10


def test_exploration_reaches_the_writer_as_its_own_row():
    """Карман доезжает до такта записи отдельной строкой, а не внутри цели.

    Без неё writer не отличает разведочную кампанию от обычной и гасит её тем
    же порогом уверенности, ради обхода которого карман и существует.
    """
    from sync.agent.portfolio import computed_rows, portfolio_targets

    saturation = {"1": _curve(), "2": _curve()}
    ladder = {"1": _ladder(revenue=800_000.0), "2": _ladder(revenue=300_000.0)}
    section = portfolio_targets(saturation, ladder, {"1": "acc", "2": "acc"},
                                explore_share=0.10)
    rows = computed_rows(section)
    explore = [r for rows_of_campaign in rows.values()
               for r in rows_of_campaign
               if r["setting_key"] == "exploration_rub"]
    assert explore, "разведочные рубли не доехали до строк Э0"
    assert all(float(r["value"]) > 0 for r in explore)


# --- потолок выключений --------------------------------------------------

def test_max_suspends_per_run_comes_from_the_panel():
    actions = [{"action_kind": "campaign.suspend", "object_id": str(i)}
               for i in range(5)]
    kept, deferred = writer_switch.cap_suspends(actions, max_per_run=3)
    assert len(kept) == 3 and len(deferred) == 2
    kept, deferred = writer_switch.cap_suspends(actions, max_per_run=0)
    assert kept == [] and len(deferred) == 5


# --- режим автономии -----------------------------------------------------

class _ReachedTheRun(Exception):
    """Метка «прогон пошёл дальше гейта данных»."""


def _stub_autonomy(monkeypatch, value, gate_status="RED"):
    """Панель с заданной автономией + метка на первом шаге после гейта."""
    from sync import agent_e1

    monkeypatch.setattr(agent_e1.agent_db, "load_agent_config",
                        lambda: {"preset": None, "overrides": {"autonomy": value}})
    monkeypatch.setattr(agent_e1, "data_gate",
                        lambda today: {"status": gate_status, "reason": "тест",
                                       "checks": [], "latest_fact_date": None})

    def _reached():
        raise _ReachedTheRun()

    monkeypatch.setattr(agent_e1.agent_db, "load_holdout_ids", _reached)
    return agent_e1


def test_autonomy_off_stops_before_touching_anything(monkeypatch, capsys):
    """«Не работает» — значит ни одного запроса, а не «посчитал и не применил».

    Прогон, который сначала выгружает полкабинета и только потом вспоминает,
    что ему запрещено, тратит квоту API на решение, уже принятое человеком.
    """
    from sync import agent_e1

    monkeypatch.setattr(agent_e1.agent_db, "load_agent_config",
                        lambda: {"preset": None, "overrides": {"autonomy": "off"}})

    def _must_not_be_called(*_a, **_k):  # pragma: no cover — защита теста
        raise AssertionError("прогон обязан остановиться ДО гейта данных")

    monkeypatch.setattr(agent_e1, "data_gate", _must_not_be_called)
    assert agent_e1._run_all([{"login": "acc"}], sandbox=False, dry_run=False,
                             today="2026-08-26") == 0
    assert "AUTONOMY_OFF" in capsys.readouterr().out


def test_suggest_only_downgrades_a_live_run_to_a_rehearsal(monkeypatch, capsys):
    """Панель УЖЕСТОЧАЕТ аргументы запуска.

    Красный гейт данных запрещает запись, но не репетицию. Раз боевой прогон
    после красного гейта не вернул 1, а пошёл дальше — он уже понижен.
    """
    agent_e1 = _stub_autonomy(monkeypatch, "suggest_only")
    with pytest.raises(_ReachedTheRun):
        agent_e1._run_all([{"login": "acc"}], sandbox=False, dry_run=False,
                          today="2026-08-26")
    out = capsys.readouterr().out
    assert "AUTONOMY_SUGGEST_ONLY" in out
    assert "DATA_GATE_RED" not in out


def test_full_autonomy_still_refuses_to_write_on_a_red_gate(monkeypatch, capsys):
    """Панель не отменяет гейт данных: «full» — не «можно по плохим данным»."""
    agent_e1 = _stub_autonomy(monkeypatch, "full")
    assert agent_e1._run_all([{"login": "acc"}], sandbox=False, dry_run=False,
                             today="2026-08-26") == 1
    out = capsys.readouterr().out
    assert "DATA_GATE_RED" in out
    assert "AUTONOMY_SUGGEST_ONLY" not in out


def test_panel_cannot_raise_a_rehearsal_to_a_live_write(monkeypatch, capsys):
    """Обратное невозможно: строка в таблице не решает за галочки запуска."""
    agent_e1 = _stub_autonomy(monkeypatch, "full")
    with pytest.raises(_ReachedTheRun):
        agent_e1._run_all([{"login": "acc"}], sandbox=False, dry_run=True,
                          today="2026-08-26")
    out = capsys.readouterr().out
    # Репетиция при красном гейте продолжается и печатает DATA_GATE:
    # DATA_GATE_RED — исход боевого прогона, и появиться он тут не может.
    assert "DATA_GATE_RED" not in out
    assert '"verdict": "DATA_GATE"' in out


def test_config_unavailable_is_visible_not_silent(monkeypatch, capsys):
    """Репетиция при недоступности панели продолжается на кодовых дефолтах,
    отказ базы виден в выводе; боевая запись запрещена (см. тест
    test_config_unavailable_blocks_live_write).

    Иначе молчащая панель читается как применённая, а пользователь не видит
    различия между ошибкой конфига и успешной загрузкой.
    """
    from sync import agent_e1

    def _boom():
        raise RuntimeError("пулер лёг")

    monkeypatch.setattr(agent_e1.agent_db, "load_agent_config", _boom)
    monkeypatch.setattr(agent_e1, "data_gate",
                        lambda today: {"status": "RED", "reason": "тест",
                                       "checks": [], "latest_fact_date": None})

    class _ReachedDataGate(Exception):
        """Метка: репетиция прошла дальше конфиг-шага и достигла data_gate."""

    def _reached():
        raise _ReachedDataGate()

    monkeypatch.setattr(agent_e1.agent_db, "load_holdout_ids", _reached)

    # Репетиция: CONFIG_UNAVAILABLE печатается, run продолжается к data_gate
    try:
        agent_e1._run_all([{"login": "acc"}], sandbox=False, dry_run=True,
                          today="2026-08-26")
        assert False, "репетиция должна была достичь load_holdout_ids"
    except _ReachedDataGate:
        pass  # Ожидаемо: репетиция достигла data_gate и продолжила дальше

    out = capsys.readouterr().out
    assert "CONFIG_UNAVAILABLE" in out
    assert "пулер лёг" in out
    # Репетиция прошла дальше конфиг-шага и достигла data_gate (красный)
    assert "DATA_GATE_RED" not in out  # репетиция не печатает DATA_GATE_RED
    assert '"verdict": "DATA_GATE"' in out  # репетиция печатает DATA_GATE (без RED)


def test_config_unavailable_blocks_live_write(monkeypatch, capsys):
    """Выключатель autonomy живёт в панели. Панель не прочиталась —
    значит слово человека неизвестно, и писать в кабинет нельзя.

    Боевой прогон должен остановиться сразу после CONFIG_UNAVAILABLE,
    без попытки писать в кабинет. Репетиция продолжится на дефолтах.
    """
    from sync import agent_e1

    monkeypatch.setattr(agent_e1.agent_db, "load_agent_config",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    # Гейт не должен вызваться, но подставляем на случай будущих изменений
    monkeypatch.setattr(agent_e1, "data_gate",
                        lambda today: {"status": "GREEN", "reason": "тест",
                                       "checks": [], "latest_fact_date": None})
    rc = agent_e1._run_all(clients=[], sandbox=False, dry_run=False, today="2026-08-30")
    out = capsys.readouterr().out
    assert rc == 1
    assert "CONFIG_UNAVAILABLE" in out


# --- отчёт прогона -------------------------------------------------------

def test_run_report_counts_exploration_separately():
    """Разведочные сдвиги — своей строкой, а не в общем счётчике.

    Они прошли планирование по другому основанию (незнание, а не доказанная
    окупаемость). Слитые с обычными, они читались бы как уверенные решения
    агента — и первый же недельный разбор беты приписал бы их качеству
    модели.
    """
    from sync import agent_e1

    plan = {"small_shift": 0, "low_confidence": [], "confidence_unknown": 0,
            "exploration": [{"campaign_id": "1", "exploration_rub": 12_000.0},
                            {"campaign_id": "2", "exploration_rub": 8_000.0}]}
    report = agent_e1._budget_report(plan, {"1": {}, "2": {}}, planned_count=2)
    assert report["exploration"]["count"] == 2
    assert report["exploration"]["rub"] == pytest.approx(20_000.0)

    quiet = agent_e1._budget_report(
        {"small_shift": 0, "low_confidence": [], "confidence_unknown": 0,
         "exploration": []}, {}, planned_count=0)
    # Нулевая разведка не печатается: строка «0» в отчёте — шум без
    # содержания, тем же правилом, что и уборка репетиционных строк.
    assert "exploration" not in quiet
