# -*- coding: utf-8 -*-
"""
tests/test_agent_e0.py — тесты расчётной стороны Э0.

Чистые функции, без сети и без БД.

Дефект 1: расчёт вычисленных настроек идёт в цикле по кабинетам, но результат
складывался в один общий список и записывался с захардкоженным идентификатором
объекта. Первичный ключ таблицы включает этот идентификатор, запись построчная —
дубликаты не падали, а тихо перетирали друг друга: в базе выживали числа того
кабинета, который дописался последним. Кабинет обязан ехать вместе с числами.

Дефект 2: конверсионность сегмента считалась по ожидаемым оплатам, размазанным
по ДОЛЕ КЛИКОВ (_attach_expected_payments) — из-за чего у всех сегментов среза
она получалась одинаковой и «корректировка по сегменту» сегменты не различала.
Теперь она считается по Conversions самого отчёта Директа, а вырожденный срез
корректировок не даёт вовсе и называет причину в отчёте прогона.
"""

import sync.agent_e0 as agent_e0
from sync.agent.computed import DEGENERATE_REASON, NO_CONVERSIONS_REASON


def _segment_rows(*specs):
    """(ключ, клики, конверсии) → строки сегментного отчёта Директа."""
    return [{"segment_kind": "device", "segment_key": key,
             "clicks": clicks, "conversions": conversions}
            for key, clicks, conversions in specs]


def test_computed_rows_carry_their_own_account():
    job_a = {"purpose": "computed", "login": "acc-1", "kind": "device",
             "rows": _segment_rows(("MOBILE", 20000, 1200), ("DESKTOP", 20000, 200))}
    job_b = {"purpose": "computed", "login": "acc-2", "kind": "device",
             "rows": _segment_rows(("MOBILE", 20000, 200), ("DESKTOP", 20000, 1200))}

    login_a, rows_a, reason_a = agent_e0.computed_rows_for_job(job_a)
    login_b, rows_b, reason_b = agent_e0.computed_rows_for_job(job_b)

    assert (login_a, login_b) == ("acc-1", "acc-2")
    assert reason_a is None and reason_b is None
    # Аудитория кабинетов разная — и числа обязаны получиться разные: именно
    # поэтому их нельзя писать под общим ключом, один набор перетирал бы другой.
    value_a = {r["setting_key"]: r["value"] for r in rows_a}
    value_b = {r["setting_key"]: r["value"] for r in rows_b}
    assert value_a["MOBILE"] != value_b["MOBILE"]


def test_computed_rows_distinguish_segments():
    """Сегменты одного среза расходятся по конверсиям, а не по объёму кликов.

    На старом коде оплаты раздавались по доле кликов: при равных кликах оба
    сегмента получали одну конверсионность и одно и то же значение.
    """
    job = {"purpose": "computed", "login": "acc-1", "kind": "device",
           "rows": _segment_rows(("MOBILE", 20000, 1200), ("DESKTOP", 20000, 200))}

    _, rows, reason = agent_e0.computed_rows_for_job(job)

    assert reason is None
    value = {r["setting_key"]: r["value"] for r in rows}
    assert value["MOBILE"] > 0 > value["DESKTOP"]


def test_computed_rows_refuse_slice_without_conversions():
    job = {"purpose": "computed", "login": "acc-1", "kind": "device",
           "rows": _segment_rows(("MOBILE", 20000, 0), ("DESKTOP", 20000, 0))}

    login, rows, reason = agent_e0.computed_rows_for_job(job)

    assert login == "acc-1"
    assert rows == []
    assert reason == NO_CONVERSIONS_REASON


def test_computed_rows_are_empty_when_segment_below_support():
    job = {"purpose": "computed", "login": "acc-1", "kind": "device",
           "rows": _segment_rows(("MOBILE", 5, 2))}

    login, rows, reason = agent_e0.computed_rows_for_job(job)

    assert login == "acc-1"
    assert rows == []
    assert reason is not None


# --------------- дефект 1, сквозная половина: расчёт ПЕРЕДАЁТ кабинет при записи
# Проверка чистой функции computed_rows_for_job выше не ловит возврат исходного
# дефекта: логин можно честно вернуть из неё и всё равно записать все кабинеты
# под общим ключом. Ревьюер вернул старое поведение прямо в теле main(), и весь
# набор остался зелёным. Поэтому здесь — прогон main() с двумя кабинетами и
# перехват аргументов, с которыми вызывается сохранение настроек.

_DB_NOOPS = (
    "ensure_agent_tables", "insert_guard_checks", "upsert_facts", "upsert_holdout",
    "upsert_experiments", "upsert_sliced_facts", "upsert_objects",
    "upsert_search_queries", "upsert_settings_snapshot", "upsert_profile",
    "upsert_behavior", "clear_holdout", "clear_bulk_tables",
)

_DB_EMPTY_LOADERS = (
    "load_direct_rows", "load_lead_rows", "load_score_rows",
    "load_campaign_features", "table_sizes",
)

# Отчёты Директа по кабинетам: у каждого свои сегменты и своя конверсионность —
# именно поэтому их числа нельзя писать под общим ключом.
_REPORTS_BY_LOGIN = {
    "acc-1": _segment_rows(("MOBILE", 20000, 1200), ("DESKTOP", 20000, 200)),
    "acc-2": _segment_rows(("TABLET", 20000, 1200), ("DESKTOP", 20000, 200)),
}

# Цели не настроены / отчёт отдал нули — считать конверсионность нечем.
_REPORTS_NO_CONVERSIONS = {
    "acc-1": _segment_rows(("MOBILE", 20000, 0), ("DESKTOP", 20000, 0)),
    "acc-2": _segment_rows(("TABLET", 20000, 0), ("DESKTOP", 20000, 0)),
}

# Конверсионность сегментов совпала (0.02 у обоих) — сигнатура починенного
# дефекта: данные не различают сегменты, корректировок быть не должно.
_REPORTS_DEGENERATE = {
    "acc-1": _segment_rows(("MOBILE", 50000, 1000), ("DESKTOP", 10000, 200)),
    "acc-2": _segment_rows(("TABLET", 50000, 1000), ("DESKTOP", 10000, 200)),
}


def _fact(today):
    return {
        "fact_date": today, "campaign_id": "111", "campaign_name": "к1",
        "direction": "vuz", "cost": 1000.0, "clicks": 1000, "leads": 10,
        "eff_leads": 5, "sum_p_pay": 500.0,
    }


def _patch_e0_run(monkeypatch, reports=None):
    """Расчёт Э0 без сети и без БД. Возвращает список вызовов записи настроек."""
    import json as _json
    from datetime import date as _date

    calls = []
    monkeypatch.setattr(agent_e0.sys, "argv", ["agent_e0"])
    monkeypatch.delenv("YM_TOKEN", raising=False)
    monkeypatch.setenv("DIRECT_CLIENTS_JSON", _json.dumps(
        [{"login": "acc-1", "goal_ids": ["1"]}, {"login": "acc-2", "goal_ids": ["2"]}]))

    for name in _DB_NOOPS:
        monkeypatch.setattr(agent_e0.agent_db, name, lambda *a, **k: 0)
    for name in _DB_EMPTY_LOADERS:
        monkeypatch.setattr(agent_e0.agent_db, name, lambda *a, **k: [])
    # Снимок настроек читается словарём кампания → сырые настройки, не списком.
    monkeypatch.setattr(agent_e0.agent_db, "load_campaign_settings_raw", lambda *a, **k: {})
    monkeypatch.setattr(
        agent_e0.agent_db, "upsert_computed_settings",
        # Значения по умолчанию намеренно: на коде ДО правки вызов идёт без
        # object_id, и тест обязан упасть на утверждении о ключе, а не на TypeError.
        lambda rows, calc_date=None, object_id=None, object_level="account":
            calls.append({"rows": list(rows), "object_id": object_id}) or len(rows),
    )

    today = _date.today().isoformat()
    monkeypatch.setattr(agent_e0, "check_freshness", lambda *a, **k: [])
    monkeypatch.setattr(agent_e0, "check_continuity",
                        lambda *a, **k: {"check_name": "continuity", "status": "OK"})
    monkeypatch.setattr(agent_e0, "verdict", lambda checks: "GREEN")
    monkeypatch.setattr(agent_e0, "assemble_facts", lambda *a, **k: [_fact(today)])
    monkeypatch.setattr(agent_e0, "select_holdout", lambda *a, **k: [])
    monkeypatch.setattr(agent_e0, "mine_quasi_experiments", lambda *a, **k: [])
    monkeypatch.setattr(agent_e0, "build_profile", lambda *a, **k: {})
    monkeypatch.setattr(agent_e0, "power_report", lambda *a, **k: {})
    monkeypatch.setattr(agent_e0, "fetch_campaign_ids", lambda *a, **k: [])
    monkeypatch.setattr(agent_e0, "fetch_objects", lambda *a, **k: [])
    monkeypatch.setattr(agent_e0, "fetch_search_queries", lambda *a, **k: [])
    by_login = _REPORTS_BY_LOGIN if reports is None else reports
    monkeypatch.setattr(
        agent_e0, "fetch_segment_report",
        # Числа отдаёт только срез по устройствам: пол и возраст оставлены
        # пустыми, чтобы разбивка отчёта читалась однозначно.
        lambda login, kind, date_from, date_to, by_campaign=False, goals=():
            [] if (by_campaign or kind != "device")
            else list(by_login.get(login, [])),
    )
    return calls


def test_main_saves_each_account_settings_under_its_own_key(monkeypatch, capsys):
    calls = _patch_e0_run(monkeypatch)

    assert agent_e0.main() == 0
    capsys.readouterr()

    by_account = {}
    for call in calls:
        by_account.setdefault(call["object_id"], []).extend(call["rows"])

    # Ключ записи — логин кабинета, а не общий идентификатор на всех.
    assert set(by_account) == {"acc-1", "acc-2"}
    keys_1 = {r["setting_key"] for r in by_account["acc-1"]}
    keys_2 = {r["setting_key"] for r in by_account["acc-2"]}
    # Числа каждого кабинета — под своим ключом и только своим: сегменты
    # кабинетов разные, и перепутать их нельзя ни в одну сторону.
    assert keys_1 == {"MOBILE", "DESKTOP"}
    assert keys_2 == {"TABLET", "DESKTOP"}


# --------------- дефект 2, сквозная половина: до записи доезжают РАЗНЫЕ числа
# Чистая функция может считать честно, а размазывание вернуться прямо в тело
# main() — ровно так дефект 1 пережил свой набор тестов. Поэтому проверка идёт
# по аргументам записи, а не по возврату computed_rows_for_job.

def test_main_writes_different_modifiers_for_different_segments(monkeypatch, capsys):
    calls = _patch_e0_run(monkeypatch)

    assert agent_e0.main() == 0
    capsys.readouterr()

    written = [r for call in calls if call["object_id"] == "acc-1" for r in call["rows"]]
    value = {r["setting_key"]: r["value"] for r in written}
    # Клики у сегментов равные — разъехаться они могут только по конверсиям.
    assert value["MOBILE"] > 0 > value["DESKTOP"]


def test_main_reports_computed_settings_per_account(monkeypatch, capsys):
    # Тот же инвариант в отчёте прогона: разбивка по кабинетам обязана быть
    # видна глазами, иначе схлопывание в один ключ снова пройдёт незамеченным.
    import json as _json

    _patch_e0_run(monkeypatch)

    assert agent_e0.main() == 0

    report = _json.loads(capsys.readouterr().out)
    assert set(report["computed_settings_by_account"]) == {"acc-1", "acc-2"}
    assert report["computed_settings"] == 4


def test_main_reports_slice_without_conversions(monkeypatch, capsys):
    """Нули вместо конверсий не дают нулевых корректировок: срез отбрасывается,
    а причина попадает в отчёт прогона. Молчаливые нули недопустимы."""
    import json as _json

    calls = _patch_e0_run(monkeypatch, reports=_REPORTS_NO_CONVERSIONS)

    assert agent_e0.main() == 0
    report = _json.loads(capsys.readouterr().out)

    assert calls == []
    assert report["computed_settings"] == 0
    skipped = {(s["account"], s["slice"]): s["reason"]
               for s in report["computed_settings_skipped"]}
    assert skipped[("acc-1", "device")] == NO_CONVERSIONS_REASON
    assert skipped[("acc-2", "device")] == NO_CONVERSIONS_REASON


def test_main_reports_degenerate_slice(monkeypatch, capsys):
    """Совпавшая у всех сегментов конверсионность — отказ с причиной в отчёте,
    а не набор одинаковых корректировок."""
    import json as _json

    calls = _patch_e0_run(monkeypatch, reports=_REPORTS_DEGENERATE)

    assert agent_e0.main() == 0
    report = _json.loads(capsys.readouterr().out)

    assert calls == []
    skipped = {(s["account"], s["slice"]): s["reason"]
               for s in report["computed_settings_skipped"]}
    assert skipped[("acc-1", "device")] == DEGENERATE_REASON
    assert skipped[("acc-2", "device")] == DEGENERATE_REASON


# --------------- цели кабинета выводятся из кабинета, а не только из секрета

def test_goals_come_from_the_account_when_the_secret_is_silent(monkeypatch):
    # Прогон 32406152097: секрет без goal_ids → Conversions не запрашивалась →
    # все двенадцать срезов отказали, применять было нечего. Молчание секрета
    # обязано означать «спроси кабинет», а не «работай вслепую».
    monkeypatch.setattr(agent_e0, "fetch_account_goal_ids", lambda login: [111, 222])

    assert agent_e0.resolve_goal_ids({"login": "cab", "goal_ids": []}) == ["111", "222"]


def test_explicit_goals_win_over_the_account(monkeypatch):
    # Значение в секрете — явное решение оператора сузить набор целей.
    # Подменять его выводом из кабинета нельзя: это тихая отмена его решения.
    def boom(login):
        raise AssertionError("кабинет спрашивать не нужно — цели заданы явно")

    monkeypatch.setattr(agent_e0, "fetch_account_goal_ids", boom)

    assert agent_e0.resolve_goal_ids({"login": "cab", "goal_ids": [7]}) == ["7"]


def test_failure_to_read_goals_does_not_kill_the_run(monkeypatch, capsys):
    # Факты, объекты и майнинг от целей не зависят: отказ одного кабинета не
    # повод потерять весь прогон Э0.
    def boom(login):
        raise RuntimeError("API недоступен")

    monkeypatch.setattr(agent_e0, "fetch_account_goal_ids", boom)

    assert agent_e0.resolve_goal_ids({"login": "cab", "goal_ids": []}) == []
    assert "не получены" in capsys.readouterr().out


def test_account_without_goals_is_named_in_the_log(monkeypatch, capsys):
    # Пустой ответ и сбой запроса — разные диагнозы, и различать их должен лог:
    # в первом случае чинят кабинет, во втором — доступ.
    monkeypatch.setattr(agent_e0, "fetch_account_goal_ids", lambda login: [])

    assert agent_e0.resolve_goal_ids({"login": "cab", "goal_ids": []}) == []
    assert "не задано ни одной цели" in capsys.readouterr().out


def test_main_sends_account_goals_into_the_report_request(monkeypatch, capsys):
    """Сквозная половина: цели кабинета обязаны доехать до запроса отчёта.

    Проверять resolve_goal_ids отдельно недостаточно — ровно так уже выживал
    дефект: чистая функция считала верно, а тело main() продолжало брать
    старое значение. Здесь фиксируются аргументы самого fetch_segment_report.
    """
    import json as _json

    seen_goals = []
    _patch_e0_run(monkeypatch)
    # Секрет молчит про цели — кабинет обязан быть спрошен.
    monkeypatch.setenv("DIRECT_CLIENTS_JSON", _json.dumps([{"login": "acc-1"}]))
    monkeypatch.setattr(agent_e0, "fetch_account_goal_ids", lambda login: [555, 666])
    monkeypatch.setattr(
        agent_e0, "fetch_segment_report",
        lambda login, kind, date_from, date_to, by_campaign=False, goals=():
            seen_goals.append(list(goals)) or [])

    assert agent_e0.main() == 0
    capsys.readouterr()

    assert seen_goals, "отчёты не запрашивались вовсе"
    # Ни одного запроса без целей: без них Reports API не отдаёт Conversions.
    assert all(g == ["555", "666"] for g in seen_goals), seen_goals
