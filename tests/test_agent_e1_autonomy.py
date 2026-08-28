# -*- coding: utf-8 -*-
"""Лестница автономии В БОЮ: ступень полосы решает, что уедет в кабинет.

Лестница была посчитана и покрыта тестами (test_agent_autonomy), но прогон её
не спрашивал: ступени приходили ключом панели, которого в панели не было, — то
есть все полосы стояли на одной константе. Здесь проверяется врезка: ступень
берётся из послужного списка полосы, а полоса на ступени 0 не молчит, а пишет
намерение в журнал.
"""

from datetime import date, timedelta

import sync.agent_e1 as agent_e1
import sync.agent_e1_watchdog as watchdog
from sync.agent import autonomy, learning_loop
from sync.agent.writer import db as writer_db
from sync.agent.writer import expectation
from sync.agent.writer import lanes

from tests.test_agent_e1 import _patch_run, _reports, _setting


# --------------------------------------------- ступень из послужного списка


def _closed(kind, verdict, count, money=None):
    """Закрытые наблюдения одного вида действий."""
    return [{"action_kind": kind, "closing_verdict": verdict,
             "money_verdict": money} for _ in range(count)]


def test_the_step_is_read_from_the_track_record_not_from_the_config():
    # Полоса тонкой настройки заработала верхнюю ступень: 48 закрытых, доля
    # улучшений выше 65 %, деньги подтвердили. Ключа в панели нет вовсе —
    # ступень обязана прийти из журнала, а не из константы.
    record = learning_loop.track_record(
        _closed("bidmodifier.set", "improved", 45, money="improved")
        + _closed("bidmodifier.set", "worsened", 3))

    steps = lanes.steps_by_lane(record, config={})

    assert steps[lanes.LANE_TUNING]["step"] == 3
    assert steps[lanes.LANE_TUNING]["source"] == lanes.STEP_EARNED


def test_a_lane_without_history_starts_on_its_floor_not_in_the_shadow():
    # Без пола лестница заперла бы агента навсегда: ступень 1 требует 12
    # закрытых наблюдений, а закрытых наблюдений не появится, пока полоса не
    # применяет. Пол — вход в лестницу, а не подарок.
    steps = lanes.steps_by_lane({}, config={})

    assert steps[lanes.LANE_ALLOCATION]["step"] == lanes.DEFAULT_STEP
    assert steps[lanes.LANE_ALLOCATION]["source"] == lanes.STEP_FLOOR


def test_the_human_word_overrides_the_ladder_in_both_directions():
    # Из тени выпускает человек, и он же сажает обратно, не дожидаясь, пока
    # накопленная история переварит свежий провал.
    record = learning_loop.track_record(
        _closed("bidmodifier.set", "improved", 45, money="improved"))

    steps = lanes.steps_by_lane(record, config={"lane_steps": {"tuning": 0}})

    assert steps[lanes.LANE_TUNING] == {"step": 0, "source": lanes.STEP_HUMAN}

    released = lanes.steps_by_lane(record, config={"lane_steps": {"launch": 2}})

    assert released[lanes.LANE_LAUNCH]["step"] == 2


def test_the_shadow_list_beats_a_brilliant_record():
    record = learning_loop.track_record(
        _closed("campaign.suspend", "improved", 45, money="improved"))

    steps = lanes.steps_by_lane(record, config={"shadow_lanes": ["suspend"]})

    assert steps[lanes.LANE_SUSPEND] == {"step": 0, "source": lanes.STEP_SHADOW}


def test_kinds_of_one_lane_are_summed_not_judged_apart():
    # Ступень получает ПОЛОСА, а не вид действия: у сдвига бюджета и целевой
    # цены общая физика ошибки, общий срок замера и общий карман. Раздельный
    # счёт держал бы обоих в тени вдвое дольше при том же объёме доказательств.
    record = learning_loop.track_record(
        _closed("budget.set", "improved", 8) + _closed("tcpa.set", "improved", 8))

    lane = lanes.lane_records(record)[lanes.LANE_ALLOCATION]

    assert lane["closed"] == 16
    assert lane[learning_loop.SUCCESS] == 16


def test_a_kind_of_a_removed_lever_does_not_break_the_ladder():
    # В истории лежат виды снятых рычагов. Уронить прогон из-за строки
    # полугодовой давности — цена, несопоставимая с пользой от строгости
    # (в отличие от lane_of, который на незнакомом виде обязан падать).
    assert lanes.lane_records({"нет.такого.вида": {"closed": 5}}) == {}


# --------------------------------------------------- падение в том же такте


def test_a_fresh_failure_drops_the_step_in_the_same_tact():
    # Накопленная доля падает медленно: у рычага с сотней наблюдений две
    # плохие недели не сдвинут её и на процент. Лестница обязана уронить
    # ступень В ТОМ ЖЕ ТАКТЕ, что и провал, — иначе полоса продолжает тратить
    # верхний потолок ровно тогда, когда перестала попадать.
    good = _closed("bidmodifier.set", "improved", 45, money="improved")
    fresh_failure = _closed("bidmodifier.set", "worsened",
                            autonomy.RECENT_WINDOW)

    record = learning_loop.track_record(good + fresh_failure)

    assert record["bidmodifier.set"]["recent_closed"] == autonomy.RECENT_WINDOW
    assert record["bidmodifier.set"]["recent_improved"] == 0
    # Накопленная доля всё ещё выше порога верхней ступени…
    assert record["bidmodifier.set"]["hit_rate"] > 0.65
    # …а ступень уже упала.
    assert lanes.steps_by_lane(record, {})[lanes.LANE_TUNING]["step"] == 2


def test_the_fresh_window_is_the_tail_of_the_journal_order():
    # «Последние» — по порядку строк выборки (ORDER BY applied_at). Возьми
    # окно с начала, и свежий провал выглядел бы как древняя история.
    record = learning_loop.track_record(
        _closed("bidmodifier.set", "worsened", 20)
        + _closed("bidmodifier.set", "improved", autonomy.RECENT_WINDOW))

    assert record["bidmodifier.set"]["recent_improved"] == autonomy.RECENT_WINDOW


def test_an_incomplete_fresh_window_is_not_a_failure():
    # На семи наблюдениях доля улучшений скачет на 14 пунктов от одного
    # вердикта. Ронять ступень по такому окну — ронять её каждый такт.
    record = learning_loop.track_record(
        _closed("bidmodifier.set", "improved", 45, money="improved")
        + _closed("bidmodifier.set", "worsened", 7))

    assert lanes.steps_by_lane(record, {})[lanes.LANE_TUNING]["step"] == 3


# ------------------------------------------------- ступень 0 пишет намерение


def _shadow_run(monkeypatch, **over):
    """Прогон, где полоса тонкой настройки посажена в тень решением человека."""
    kwargs = {
        "computed_by_login": {"acc-1": [_setting("bid_modifier:device",
                                                 "DESKTOP", 30)]},
        "campaigns_by_login": {"acc-1": [111]},
        "daily_cost": {"111": 1000.0},
        "prod_apply": True,
    }
    kwargs.update(over)
    rows = _patch_run(monkeypatch, **kwargs)
    # Строка портфеля: без неё цена лида неизвестна, ожидание не заявляется, и
    # намерение уехало бы в журнал без обещания — сверять его было бы нечем.
    monkeypatch.setattr(
        agent_e1.agent_db, "load_latest_campaign_computed",
        lambda ids: {"111": [{"setting_kind": "budget_target",
                              "setting_key": "target_28d",
                              "value": 0.0, "raw_value": 28_000.0,
                              "support_n": 28.0,
                              "calc_date": date.today().isoformat()}]})
    monkeypatch.setattr(agent_e1.agent_db, "load_agent_config",
                        lambda: {"preset": None,
                                 "overrides": {"lane_steps": {"tuning": 0}}})
    return rows


def test_shadow_step_records_intent_without_applying(monkeypatch, capsys):
    rows = _shadow_run(monkeypatch)

    assert agent_e1.main() == 0
    report = [r for r in _reports(capsys) if "account" in r][0]

    sent = [c.sent for c in agent_e1.WriteClient.instances]
    assert sent == [[]], "полоса в тени не имеет права ехать в кабинет"
    assert [r["status"] for r in rows] == [writer_db.SHADOW_STATUS]
    assert report["shadow"]["intents"] == 1
    assert report["shadow"]["written"] == 1
    assert report["shadow"]["by_lane"] == {lanes.LANE_TUNING: 1}


def test_the_intent_carries_the_promise_it_would_have_made(monkeypatch, capsys):
    # Намерение без обещания сверять не с чем: строка в журнале была бы
    # памятником, а не материалом для решения о выпуске.
    rows = _shadow_run(monkeypatch)

    assert agent_e1.main() == 0
    capsys.readouterr()

    payload = rows[0]["payload"]
    assert payload[expectation.BASIS_KEY]
    assert payload[expectation.DAYS_KEY] == lanes.MEASURE_DAYS[lanes.LANE_TUNING]


def test_the_intent_does_not_charge_the_risk_budget(monkeypatch, capsys):
    # Цена действия — цена ЭКСПОЗИЦИИ, а её не было. Ненулевое число здесь
    # читалось бы риск-бюджетом недели как занятые деньги: намерение стоило бы
    # места настоящим изменениям и попало бы в худший недельный исход.
    rows = _shadow_run(monkeypatch)

    assert agent_e1.main() == 0
    capsys.readouterr()

    assert rows[0]["risk_rub"] == 0.0


def test_the_shadow_refusal_carries_its_own_reason(monkeypatch, capsys):
    # «Полосу не выпустил человек» не лечится ни рублём лимита, ни порядком
    # ценности. Слей его с lane_limit — и на разборе рычаг на приёмке выглядел
    # бы как полоса, которой мало денег.
    _shadow_run(monkeypatch)

    assert agent_e1.main() == 0
    report = [r for r in _reports(capsys) if "account" in r][0]

    assert report["lanes"]["refused"].get("shadow") == 1
    assert "lane_limit" not in report["lanes"]["refused"]


def test_a_rehearsal_writes_no_intent(monkeypatch, capsys):
    # Намерение, записанное репетицией, ждало бы сверки с фактом, которого не
    # будет: репетиция журнал не трогает по общему правилу движка.
    rows = _shadow_run(monkeypatch, prod_apply=False)

    assert agent_e1.main() == 0
    report = [r for r in _reports(capsys) if "account" in r][0]

    assert rows == []
    assert report["shadow"]["intents"] == 1
    assert report["shadow"]["written"] == 0


def test_the_steps_are_printed_every_tact(monkeypatch, capsys):
    # Ступень, видимая только в коде, не является решением, о котором можно
    # спорить: «полоса взяла пять действий» без ступени не отвечает, мало ей
    # потолка или мало кандидатов.
    _shadow_run(monkeypatch)

    assert agent_e1.main() == 0
    report = [r for r in _reports(capsys) if "account" in r][0]

    steps = report["autonomy"]["steps"]
    assert set(steps) == set(lanes.ALL_LANES)
    assert steps[lanes.LANE_TUNING] == {"step": 0, "source": lanes.STEP_HUMAN}
    # Происхождение печатается рядом с числом: ступень 1 у полосы без единого
    # закрытого наблюдения и заслуженная проба — разные состояния.
    assert steps[lanes.LANE_HYGIENE]["source"] == lanes.STEP_FLOOR


def test_an_unreadable_journal_does_not_stop_the_run(monkeypatch, capsys):
    # Лестница — отчётный слой поверх решения, а не само решение. Без журнала
    # она возвращает пол полос, и это читается в безопасную сторону: поднять
    # ступень без закрытых наблюдений она не может по построению.
    _patch_run(monkeypatch,
               computed_by_login={"acc-1": [_setting("bid_modifier:device",
                                                     "DESKTOP", 30)]},
               campaigns_by_login={"acc-1": [111]},
               daily_cost={"111": 1000.0})
    monkeypatch.setattr(agent_e1.writer_db, "closed_actions",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("журнал недоступен")))

    assert agent_e1.main() == 0
    report = [r for r in _reports(capsys) if "account" in r][0]

    steps = report["autonomy"]["steps"]
    assert steps[lanes.LANE_TUNING]["step"] == lanes.DEFAULT_STEP
    assert "журнал недоступен" in steps[lanes.LANE_TUNING]["journal_unavailable"]


# ------------------------------------------ намерение сверяется с фактом


TODAY = date(2026, 9, 30)


def _intent(promised=5.0, days=7, base_rate=1.0, created=None):
    """Строка журнала со статусом shadow: «сделал бы X, жду Y за D дней»."""
    return {
        "action_id": "act-1", "account": "acc-1", "object_id": "111",
        "action_kind": "bidmodifier.set", "object_level": "campaign",
        "created_at": created or (TODAY - timedelta(days=days + 2)),
        "red_line": {"baseline_cpa": 1000.0, "baseline_leads_per_day": base_rate},
        "payload": {expectation.BASIS_KEY: "доля сегмента",
                    expectation.LEADS_KEY: promised,
                    expectation.RUB_KEY: -1000.0,
                    expectation.DAYS_KEY: days},
    }


def _facts(leads_per_day, days=9, start=None):
    start = start or (TODAY - timedelta(days=days))
    return [{"campaign_id": "111", "fact_date": start + timedelta(days=i),
             "cost": 1000.0, "eff_leads": leads_per_day} for i in range(days)]


def test_an_intent_whose_promise_came_true_by_itself_is_a_hit():
    # Объект сам ушёл туда, куда обещал рычаг: это довод ПРОТИВ выпуска —
    # рычаг предсказывает дрейф, который случился бы и без него.
    action = _intent(promised=5.0, days=7, base_rate=1.0)

    result = watchdog.shadow_check(action, _facts(3), TODAY, TODAY)

    assert result["verdict"] == watchdog.SHADOW_HIT
    assert result["final"] is True


def test_an_intent_whose_promise_did_not_come_true_is_a_miss():
    action = _intent(promised=5.0, days=7, base_rate=1.0)

    result = watchdog.shadow_check(action, _facts(1), TODAY, TODAY)

    assert result["verdict"] == watchdog.SHADOW_MISS


def test_a_drift_the_other_way_is_never_a_hit():
    # Знак отдельно от модуля: объект, ушедший в ПРОТИВОПОЛОЖНУЮ сторону на
    # величину больше обещанной, обещания не исполнил.
    action = _intent(promised=5.0, days=7, base_rate=3.0)

    result = watchdog.shadow_check(action, _facts(0), TODAY, TODAY)

    assert result["verdict"] == watchdog.SHADOW_MISS


def test_an_open_horizon_is_not_judged_and_not_written():
    # Вердикт выносится один раз и только по закрытому горизонту: окно с
    # каждым днём шире, и повторный проход превращал бы «не сбылось» в
    # «сбылось» накопленным дрейфом.
    action = _intent(promised=5.0, days=30,
                     created=TODAY - timedelta(days=2))

    result = watchdog.shadow_check(action, _facts(3), TODAY, TODAY)

    assert result["verdict"] == watchdog.SHADOW_UNKNOWN
    assert result["final"] is False


def test_a_promise_smaller_than_one_lead_is_refused_by_name():
    # Счётчик заявок такую разницу не различает. Отказ назван, а не выдан за
    # «не сбылось»: «не знаем» и «не сбылось» лечатся по-разному.
    action = _intent(promised=0.4)

    result = watchdog.shadow_check(action, _facts(3), TODAY, TODAY)

    assert result["verdict"] == watchdog.SHADOW_UNKNOWN
    assert result["reason"] == watchdog.SHADOW_SMALL_PROMISE_REASON
    assert result["final"] is True


def test_an_intent_without_a_declared_promise_is_refused_by_name():
    action = _intent()
    action["payload"] = {}

    result = watchdog.shadow_check(action, _facts(3), TODAY, TODAY)

    assert result["reason"] == watchdog.SHADOW_NO_PROMISE_REASON


def test_without_a_base_rate_nothing_is_judged():
    # Голая разность сумм показала бы обвал там, где темп вырос: окно базы 28
    # дней, окно сверки 7. Нет темпа базы — нет и вердикта.
    action = _intent(base_rate=0.0)

    result = watchdog.shadow_check(action, _facts(3), TODAY, TODAY)

    assert result["reason"] == watchdog.SHADOW_NO_BASE_REASON


def test_the_promise_is_measured_over_its_own_horizon():
    # «Жду Y за три дня» и «жду Y за тридцать» — разные утверждения. Мерить их
    # общим горизонтом наблюдения значит спрашивать с трёхдневного обещания
    # месячный дрейф объекта.
    short = _intent(promised=5.0, days=3)

    result = watchdog.shadow_check(short, _facts(3, days=30), TODAY, TODAY)

    assert result["horizon_days"] == 3
    window = result["window"]
    assert (date.fromisoformat(window["to"])
            - date.fromisoformat(window["from"])).days == 2


class _ShadowJournal:
    def __init__(self, rows):
        self.rows = rows
        self.marked = []

    def shadow_actions(self, *a, **k):
        return list(self.rows)

    def mark_shadow_outcome(self, action_id, verdict, detail=None):
        self.marked.append((action_id, verdict))
        return True


def test_only_final_verdicts_reach_the_journal():
    journal = _ShadowJournal([_intent(promised=5.0, days=7),
                              _intent(promised=5.0, days=30,
                                      created=TODAY - timedelta(days=2))])

    out = watchdog.shadow_report(journal, {"111": _facts(3)}, TODAY, TODAY)

    assert out["waiting"] == 2
    assert [v for _, v in journal.marked] == [watchdog.SHADOW_HIT]
    assert out["marked"] == 1


def test_a_rehearsal_judges_but_writes_nothing():
    journal = _ShadowJournal([_intent(promised=5.0, days=7)])

    out = watchdog.shadow_report(journal, {"111": _facts(3)}, TODAY, TODAY,
                                 journal_ok=False)

    assert journal.marked == []
    assert out["verdicts"] == {watchdog.SHADOW_HIT: 1}


def test_an_unreadable_journal_does_not_stop_the_watchdog():
    class _Broken:
        def shadow_actions(self, *a, **k):
            raise RuntimeError("журнал недоступен")

    out = watchdog.shadow_report(_Broken(), {}, TODAY, TODAY)

    assert "журнал недоступен" in out["unavailable"]
