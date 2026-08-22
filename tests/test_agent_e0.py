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

import json as _json
from datetime import date

import sync.agent_e0 as agent_e0
from sync.agent.guard import check_freshness as real_check_freshness
from sync.agent.guard import verdict as real_verdict
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


def _today():
    return date.today().isoformat()


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
    # Сверка сумм гейта ходит в витрину: без подмены тест падал бы на
    # отсутствии DATABASE_URL, а не на своём утверждении.
    monkeypatch.setattr(agent_e0.agent_db, "mart_cost_total", lambda *a, **k: 0.0)
    # Справочник базового CPA — источник порога для кандидатов в минус-слова.
    # Пусто = порога нет, кандидаты не считаются: это штатное состояние
    # кабинета без истории, а не повод падать.
    monkeypatch.setattr(agent_e0.agent_db, "load_baseline_cpa", lambda *a, **k: {})
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
    monkeypatch.setattr(agent_e0, "fetch_search_queries",
                        lambda *a, **k: ([], {"goal_column": "Conversions_111_LSCCD", "conversions": 7, "columns_offered": 1}))
    by_login = _REPORTS_BY_LOGIN if reports is None else reports
    monkeypatch.setattr(
        agent_e0, "fetch_segment_report",
        # Числа отдаёт только срез по устройствам: пол и возраст оставлены
        # пустыми, чтобы разбивка отчёта читалась однозначно.
        lambda login, kind, date_from, date_to, by_campaign=False, goals=():
            ([] if (by_campaign or kind != "device")
             else list(by_login.get(login, [])),
             {"goal_column": "Conversions_111_LSCCD", "conversions": 1,
              "columns_offered": 1}),
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
            (seen_goals.append(list(goals)) or [], {"goal_column": "Conversions_111_LSCCD", "conversions": 7, "columns_offered": 1}))

    assert agent_e0.main() == 0
    capsys.readouterr()

    assert seen_goals, "отчёты не запрашивались вовсе"
    # Ни одного запроса без целей: без них Reports API не отдаёт Conversions.
    assert all(g == ["555", "666"] for g in seen_goals), seen_goals


# --------------- пороги свежести: у источников разная норма отставания

def _fresh_direct(monkeypatch, today):
    """Свежий расход Директа: у него свой порог, и он не должен мешать
    проверять поведение гейта по CRM."""
    monkeypatch.setattr(agent_e0.agent_db, "load_direct_rows",
                        lambda *a, **k: [{"date": today.isoformat(), "campaign_id": "111",
                                          "cost": 100.0, "clicks": 10, "impressions": 100}])
    # Витрина сходится с источником — иначе сверка сумм гейта краснеет, и тест
    # падал бы не на своём утверждении. Это и есть штатное состояние.
    monkeypatch.setattr(agent_e0.agent_db, "mart_cost_total", lambda *a, **k: 100.0)


def test_normal_crm_lag_does_not_kill_the_run(monkeypatch, capsys):
    """Отставание CRM на четыре дня — норма, а не повод не считать.

    Общий порог в 72 часа ронял ВЕСЬ расчёт на штатном лаге (боевой прогон
    32450489955: crm_lead_details 77 часов при лимите 72 → verdict RED, выход 1).
    Защита, срабатывающая на норме, просто останавливает работу — тот же класс
    дефекта, что вечный RED у гейта витрины сторожа.
    """
    import json as _json
    from datetime import date as _date, timedelta as _td

    _patch_e0_run(monkeypatch)
    today = _date.today()
    _fresh_direct(monkeypatch, today)
    monkeypatch.setattr(agent_e0.agent_db, "load_lead_rows",
                        lambda *a, **k: [{"lead_id": "l1", "campaign_id": "111",
                                          "created_date": (today - _td(days=4)).isoformat()}])
    monkeypatch.setattr(agent_e0, "check_freshness", real_check_freshness)
    monkeypatch.setattr(agent_e0, "verdict", real_verdict)

    assert agent_e0.main() == 0

    report = _json.loads(capsys.readouterr().out)
    assert report["verdict"] == "GREEN"
    assert report["crm_lag_days"] == 4


def test_dead_crm_still_stops_the_run(monkeypatch, capsys):
    # Неделя тишины — уже поломка: 02.08.2026 таблица встала на четверо суток,
    # и никто не замечал четыре дня. Порог обязан остаться, просто выше нормы.
    import json as _json
    from datetime import date as _date, timedelta as _td

    _patch_e0_run(monkeypatch)
    today = _date.today()
    _fresh_direct(monkeypatch, today)
    monkeypatch.setattr(agent_e0.agent_db, "load_lead_rows",
                        lambda *a, **k: [{"lead_id": "l1", "campaign_id": "111",
                                          "created_date": (today - _td(days=9)).isoformat()}])
    monkeypatch.setattr(agent_e0, "check_freshness", real_check_freshness)
    monkeypatch.setattr(agent_e0, "verdict", real_verdict)

    assert agent_e0.main() == 1
    assert _json.loads(capsys.readouterr().out)["verdict"] == "RED"


def test_direct_keeps_the_strict_threshold(monkeypatch, capsys):
    # Расход Директа приезжает своим синком и почти не отстаёт: послабление
    # для CRM не должно распространяться на него, иначе вставший синк расхода
    # пройдёт незамеченным целую неделю.
    assert agent_e0.DIRECT_MAX_AGE_HOURS == 72
    assert agent_e0.CRM_MAX_AGE_HOURS > agent_e0.DIRECT_MAX_AGE_HOURS


# --------------- почасовой профиль: счётчики складываются, а не склеиваются

def test_counter_is_bound_to_the_account_that_owns_its_campaigns():
    """Профиль накладывается на TimeTargeting КАМПАНИИ, а кампания ведёт на
    ОДИН сайт. Значит счётчик обязан быть привязан к своему кабинету.

    Проба 32579931952: связь однозначная — 98627983 узнаётся по 93 % имён
    кампаний одного кабинета, 96526110 по 90 % другого, 95348914 по 75 %
    третьего.
    """
    from sync.agent.metrika import resolve_counter_account

    login_by_name = {"к1": "acc-1", "к2": "acc-1", "к3": "acc-2"}

    assert resolve_counter_account({"к1", "к2"}, login_by_name) == "acc-1"
    assert resolve_counter_account({"к3"}, login_by_name) == "acc-2"


def test_one_stray_campaign_name_does_not_hand_over_the_schedule():
    """Порог доли обязателен: кабинет, поймавший одно случайное совпадение
    имени, получил бы расписание чужого сайта — а это ровно тот отказ, который
    надо видеть, а не сглаживать.

    В пробе такие совпадения и были: 1/42 и 1/30 у постороннего кабинета.
    """
    from sync.agent.metrika import resolve_counter_account

    names = {"чужая-%d" % i for i in range(41)} | {"к1"}

    assert resolve_counter_account(names, {"к1": "acc-1"}) is None


def test_counter_without_any_known_campaign_has_no_account():
    from sync.agent.metrika import resolve_counter_account

    assert resolve_counter_account(set(), {"к1": "acc-1"}) is None
    assert resolve_counter_account({"неизвестная"}, {"к1": "acc-1"}) is None


def test_each_counter_profile_goes_to_its_own_account_only(monkeypatch, capsys):
    """Сквозная половина: разделение обязано СЛУЧИТЬСЯ в прогоне.

    Раньше профили трёх счётчиков складывались в один и писались под каждым
    логином. Счётчики о сутках не согласны — проба 32579085232 дала у 98627983
    часы 02-05 на уровне 130, у 96526110 те же часы на уровне 90, — поэтому
    слитый профиль решал спор объёмом, и кампаниям меньшего счётчика
    доставалось расписание чужого сайта.
    """
    seen = []
    calls = _patch_e0_run(monkeypatch)
    monkeypatch.setenv("YM_TOKEN", "test-token")
    monkeypatch.setattr(agent_e0, "EDU_COUNTERS", [111, 222])
    # Счётчик 111 видит кампанию acc-1, счётчик 222 — кампанию acc-2.
    monkeypatch.setattr(agent_e0, "fetch_campaign_ids",
                        lambda login: [111] if login == "acc-1" else [222])
    monkeypatch.setattr(agent_e0, "assemble_facts", lambda *a, **k: [
        dict(_fact(_today()), campaign_id="111", campaign_name="к1"),
        dict(_fact(_today()), campaign_id="222", campaign_name="к2"),
    ])
    monkeypatch.setattr(
        agent_e0, "fetch_campaign_behavior",
        lambda counter, *a, **k: [{"campaign_name": "к1" if counter == 111 else "к2",
                                   "visits": 10, "bounces": 1, "pageviews": 20,
                                   "visit_seconds": 100}])

    def fake_hourly(counter, *_args, **_kwargs):
        conv = 0.40 if counter == 111 else 0.20
        rows = [{"segment_kind": "hour", "segment_key": str(h), "clicks": 1000,
                 "leads": int(1000 * conv), "sum_p_pay": 1000 * conv * (0.9 + 0.2 * (h % 2))}
                for h in range(24)]
        return rows, {"goal_id": 1, "reaches": int(24 * 1000 * conv), "goals_offered": 2}

    monkeypatch.setattr(agent_e0, "fetch_counter_goal_ids", lambda counter: [1, 2])
    monkeypatch.setattr(agent_e0, "fetch_hourly_profile", fake_hourly)
    monkeypatch.setattr(
        agent_e0, "compute_schedule",
        lambda rows: (seen.append(list(rows)) or
                      [{"setting_kind": "schedule:hour", "setting_key": str(h),
                        "value": 0.0, "support_n": 1000, "raw_value": 1.0}
                       for h in range(24)], None))

    assert agent_e0.main() == 0
    capsys.readouterr()

    # Каждый счётчик посчитан ОТДЕЛЬНО: два вызова по 24 строки, а не один.
    assert [len(rows) for rows in seen] == [24, 24]
    assert seen[0][0]["clicks"] == 1000, "счётчики снова сложены в один профиль"

    schedule_owners = [c["object_id"] for c in calls
                       if any(r["setting_kind"] == "schedule:hour" for r in c["rows"])]
    assert sorted(schedule_owners) == ["acc-1", "acc-2"]


def test_counter_without_an_account_is_reported_not_broadcast(monkeypatch, capsys):
    """Привязки нет — применять расписание некуда, и это видно в отчёте.

    Записать его «всем» значило бы вернуть исходный дефект под другим именем:
    в пробе 32579931952 четвёртый кабинет (5 кампаний) не принадлежит ни
    одному счётчику EDU.
    """
    calls = _patch_e0_run(monkeypatch)
    monkeypatch.setenv("YM_TOKEN", "test-token")
    monkeypatch.setattr(agent_e0, "EDU_COUNTERS", [111])
    monkeypatch.setattr(agent_e0, "fetch_campaign_ids", lambda login: [])
    monkeypatch.setattr(agent_e0, "fetch_campaign_behavior",
                        lambda *a, **k: [{"campaign_name": "ничья", "visits": 10,
                                          "bounces": 1, "pageviews": 20,
                                          "visit_seconds": 100}])
    monkeypatch.setattr(agent_e0, "fetch_counter_goal_ids", lambda counter: [1, 2])
    monkeypatch.setattr(
        agent_e0, "fetch_hourly_profile",
        lambda *a, **k: ([{"segment_kind": "hour", "segment_key": str(h), "clicks": 1000,
                           "leads": 40, "sum_p_pay": 40.0} for h in range(24)],
                         {"goal_id": 1, "reaches": 960, "goals_offered": 2}))
    monkeypatch.setattr(agent_e0, "compute_schedule",
                        lambda rows: (_ for _ in ()).throw(
                            AssertionError("расчёт без кабинета-владельца")))

    assert agent_e0.main() == 0
    report = _json.loads(capsys.readouterr().out)

    assert {"counter": 111, "reason": "счётчик не привязан к кабинету"} in \
        report["metrika_hourly_skipped"]
    assert not [c for c in calls
                if any(r["setting_kind"] == "schedule:hour" for r in c["rows"])]


# --------------- кандидаты в минус-слова: расчёт обязан СЛУЧИТЬСЯ

def _queries(*rows):
    return [{"query": q, "cost": cost, "conversions": conv, "clicks": 10,
             "campaign_id": "111", "account": "acc-1"} for q, cost, conv in rows]


def test_minus_word_candidates_land_in_the_report(monkeypatch, capsys):
    """Данные для минус-слов собирались каждый прогон, а расчёт не вызывался.

    Тест держит именно вызов: порог берётся из справочника базового CPA, и в
    отчёт попадают запросы, сжёгшие втрое больше без единой конверсии.
    """
    _patch_e0_run(monkeypatch)
    monkeypatch.setattr(agent_e0.agent_db, "load_baseline_cpa",
                        lambda *a, **k: {"111": 1000.0, "222": 2000.0, "333": 9000.0})
    monkeypatch.setattr(agent_e0, "fetch_search_queries", lambda login, *a, **k:
                        (_queries(("дорогой мусор", 7000.0, 0),
                                  ("дешёвый мусор", 500.0, 0),
                                  ("дорогой рабочий", 7000.0, 3)) if login == "acc-1"
                         else [], {"goal_column": "Conversions_111_LSCCD", "conversions": 7, "columns_offered": 1}))

    assert agent_e0.main() == 0
    report = _json.loads(capsys.readouterr().out)["minus_word_candidates"]

    # Порог — МЕДИАНА справочника (2000), а не среднее (4000): среднее тянет
    # вверх единичная дорогая кампания, и «втрое дороже» перестаёт значить.
    assert report["cpa_limit"] == 2000.0
    assert report["sample"] == ["дорогой мусор"]
    assert report["count"] == 1
    assert report["cost_burned"] == 7000.0


def test_query_report_asks_for_the_same_goals_as_the_slices(monkeypatch, capsys):
    """Цели решаются ОДИН РАЗ на кабинет и достаются обоим отчётам.

    Отчёт запросов читал сырое поле секрета client["goal_ids"], а срезы —
    resolve_goal_ids, который при молчащем секрете спрашивает сам кабинет.
    У кабинетов EDU поле пустое, поэтому запросы уходили в Директ БЕЗ Goals:
    в ответе нет ни одной колонки Conversions, у каждого запроса ноль, и
    правило «дорого и без конверсий» объявляло мусором рабочее ядро.
    """
    seen_goals = []
    _patch_e0_run(monkeypatch)
    monkeypatch.setenv("DIRECT_CLIENTS_JSON", _json.dumps([{"login": "acc-1"}]))
    monkeypatch.setattr(agent_e0, "fetch_account_goal_ids", lambda login: [555, 666])
    monkeypatch.setattr(
        agent_e0, "fetch_search_queries",
        lambda login, *a, goals=(), **k: (
            seen_goals.append(list(goals)) or [],
            {"goal_column": "Conversions_555_LSCCD", "conversions": 1,
             "columns_offered": 2}))

    assert agent_e0.main() == 0
    capsys.readouterr()

    assert seen_goals == [["555", "666"]]


def test_query_report_without_conversion_columns_yields_no_minus_words(
        monkeypatch, capsys):
    """Ноль конверсий у КАЖДОГО запроса — это «конверсии не спрошены».

    На таких данных правило «дорого и без конверсий» выносит приговор всему
    кабинету: на прогоне 32580972099 в кандидаты попали «колледжи москвы»,
    «мти», «мед колледж» — 31 запрос на 271 975 ₽. Слепой кабинет обязан
    выпадать из расчёта С НАЗВАННОЙ ПРИЧИНОЙ: молчаливый пропуск неотличим
    от «кандидатов не нашлось».
    """
    _patch_e0_run(monkeypatch)
    monkeypatch.setattr(agent_e0.agent_db, "load_baseline_cpa",
                        lambda *a, **k: {"111": 1000.0, "222": 2000.0})
    monkeypatch.setattr(
        agent_e0, "fetch_search_queries",
        lambda login, *a, **k: (
            _queries(("колледжи москвы", 90000.0, 0)) if login == "acc-1" else [],
            {"goal_column": None, "conversions": 0, "columns_offered": 0}
            if login == "acc-1"
            else {"goal_column": "Conversions_2_LSCCD", "conversions": 5,
                  "columns_offered": 1}))

    assert agent_e0.main() == 0
    report = _json.loads(capsys.readouterr().out)["minus_word_candidates"]

    assert report["blind_accounts"] == ["acc-1"]
    assert report["count"] == 0
    assert report["sample"] == []


def test_run_report_names_the_goal_behind_every_slice(monkeypatch, capsys):
    """Выбор цели обязан быть ВИДЕН, а не только правильно сделан.

    Конверсионность каждого среза, а значит и каждая корректировка ставки,
    считается по ОДНОЙ колонке цели. Колонку выбирает код — самую массовую из
    переданных, — а имён целей Директ в отчёте не отдаёт, только
    идентификаторы. Молчаливый выбор значит, что «корректировки посчитаны по
    прокрутке страницы» и «по заявке» выглядят в прогоне одинаково.
    """
    _patch_e0_run(monkeypatch)

    assert agent_e0.main() == 0
    report = _json.loads(capsys.readouterr().out)

    chosen = report["segment_goal_columns"]
    assert chosen, "выбранная цель не видна в отчёте прогона"
    assert {g["account"] for g in chosen} == {"acc-1", "acc-2"}
    assert {g["purpose"] for g in chosen} == {"computed", "sliced"}
    assert all(g["goal_column"] == "Conversions_111_LSCCD" for g in chosen)
    assert all(g["columns_offered"] == 1 for g in chosen)

    # По этой же цели отбираются кандидаты в минус-слова — она тоже названа.
    assert [g["account"] for g in report["query_goal_columns"]] == ["acc-1", "acc-2"]


def test_no_baseline_means_no_threshold_not_a_zero_one(monkeypatch, capsys):
    """Пустой справочник — «считать не от чего», а не порог 0.

    Порог 0 сделал бы кандидатом ЛЮБОЙ запрос без конверсий, включая
    копеечные, — и отчёт превратился бы в шум на весь кабинет.
    """
    _patch_e0_run(monkeypatch)
    monkeypatch.setattr(agent_e0.agent_db, "load_baseline_cpa", lambda *a, **k: {})
    monkeypatch.setattr(agent_e0, "fetch_search_queries", lambda login, *a, **k:
                        (_queries(("копеечный мусор", 3.0, 0)) if login == "acc-1"
                         else [], {"goal_column": "Conversions_111_LSCCD", "conversions": 7, "columns_offered": 1}))

    assert agent_e0.main() == 0
    report = _json.loads(capsys.readouterr().out)["minus_word_candidates"]

    assert report["count"] == 0


# --------------- почасовой профиль считается по ЛИДАМ, а не по «любой цели»

def test_hourly_profile_asks_only_for_this_counters_lead_goals(monkeypatch, capsys):
    """Цель принадлежит ОДНОМУ счётчику.

    Отправить чужой идентификатор нельзя: Метрика отвергает запрос целиком, и
    профиль всего счётчика обнулился бы — молча, потому что отказ ловится в
    guard_checks, а расписание просто считается по оставшимся строкам.
    """
    asked = []
    _patch_e0_run(monkeypatch)
    monkeypatch.setenv("YM_TOKEN", "test-token")
    monkeypatch.setattr(agent_e0, "EDU_COUNTERS", [111, 222])
    monkeypatch.setattr(agent_e0, "fetch_campaign_behavior", lambda *a, **k: [])
    # acc-1 оптимизируется на цель 1, acc-2 — на цель 2 (см. _patch_e0_run).
    monkeypatch.setattr(agent_e0, "fetch_counter_goal_ids",
                        lambda counter: [1, 999] if counter == 111 else [2])
    monkeypatch.setattr(
        agent_e0, "fetch_hourly_profile",
        lambda counter, *a, **k: (asked.append((counter, a[-1])) or [], {"goal_id": None}))
    monkeypatch.setattr(agent_e0, "compute_schedule", lambda rows: ([], None))

    assert agent_e0.main() == 0
    capsys.readouterr()

    # 999 — цель счётчика, но НЕ цель Директа: в запрос не идёт.
    assert asked == [(111, [1]), (222, [2])]


def test_report_names_the_goal_the_schedule_was_counted_on(monkeypatch, capsys):
    """Чем считали расписание — обязано быть видно в отчёте прогона.

    Числитель выбирается как самая массовая цель Директа (metrika.pick_lead_goal),
    и без имени рядом этот выбор неотличим от подмены: ровно так сюда однажды
    попала микроцель прокрутки вместо заявки.
    """
    _patch_e0_run(monkeypatch)
    monkeypatch.setenv("YM_TOKEN", "test-token")
    monkeypatch.setattr(agent_e0, "EDU_COUNTERS", [111])
    monkeypatch.setattr(agent_e0, "fetch_campaign_behavior", lambda *a, **k: [])
    monkeypatch.setattr(agent_e0, "fetch_counter_goal_ids", lambda counter: [1, 2])
    monkeypatch.setattr(
        agent_e0, "fetch_hourly_profile",
        lambda *a, **k: ([], {"goal_id": 2, "reaches": 4572, "goals_offered": 2}))
    monkeypatch.setattr(agent_e0, "compute_schedule", lambda rows: ([], None))

    assert agent_e0.main() == 0
    report = _json.loads(capsys.readouterr().out)

    assert report["metrika_hourly_numerator"] == [
        {"counter": 111, "goal_id": 2, "reaches": 4572, "goals_offered": 2}]


def test_counter_without_direct_goals_is_reported_not_guessed(monkeypatch, capsys):
    """Нет пересечения — считать нечего, и это видно в отчёте.

    Молчаливый пропуск здесь неотличим от «профиль посчитан»: ровно так
    расписание и оказалось построено не по тем целям.
    """
    _patch_e0_run(monkeypatch)
    monkeypatch.setenv("YM_TOKEN", "test-token")
    monkeypatch.setattr(agent_e0, "EDU_COUNTERS", [333])
    monkeypatch.setattr(agent_e0, "fetch_campaign_behavior", lambda *a, **k: [])
    monkeypatch.setattr(agent_e0, "fetch_counter_goal_ids", lambda counter: [77])
    monkeypatch.setattr(agent_e0, "fetch_hourly_profile",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("запрос без целей Директа")))

    assert agent_e0.main() == 0
    report = _json.loads(capsys.readouterr().out)

    assert report["metrika_hourly"] == 0
    assert report["metrika_hourly_skipped"] == [
        {"counter": 333, "reason": "целей Директа нет на счётчике"}]
    # Цели кабинетов в отчёте — иначе «почему профиль пуст» не разобрать.
    assert report["metrika_hourly_goals"] == [1, 2]


# ---------------------------------------------------------- лестница воронки


def _ladder_fact(fact_date, campaign_id, direction, **counts):
    base = {"fact_date": fact_date, "campaign_id": campaign_id,
            "direction": direction, "payments_fact": 0, "deals": 0,
            "connected_leads": 0, "eff_leads": 0, "leads": 0, "clicks": 0}
    base.update(counts)
    return base


def _ladder_lead(created, direction, paid_on=None, revenue=0.0):
    return {"created_date": created, "payment_date": paid_on,
            "is_paid": paid_on is not None, "revenue": revenue,
            "direction": direction}


def test_ladder_section_shifts_window_by_maturity_from_data():
    """Окно решений сдвигается на p90 лага оплаты, выведенный из лидов прогона."""
    today = date(2026, 8, 22)
    # Десять оплат: девять с лагом 0 дней, одна с лагом 20 → p90 = 20.
    leads = [_ladder_lead("2026-05-01", "spo", paid_on="2026-05-01")] * 9
    leads += [_ladder_lead("2026-05-01", "spo", paid_on="2026-05-21")]
    section = agent_e0.funnel_ladder_section([], leads, today=today)
    assert section["maturity_days"] == 20
    assert section["window_to"] == "2026-08-02"      # 22.08 − 20 дней
    assert section["window_from"] == "2026-05-04"    # ещё −90 дней


def test_ladder_section_excludes_immature_facts():
    """Свежие дни (незрелые оплаты) в счётчики лестницы не входят."""
    today = date(2026, 8, 22)
    leads = [_ladder_lead("2026-05-01", "spo", paid_on="2026-05-31")]  # лаг 30
    facts = [
        _ladder_fact("2026-07-01", "c1", "spo", eff_leads=30),   # зрелый день
        _ladder_fact("2026-08-20", "c1", "spo", eff_leads=500),  # ещё не дозрел
    ]
    section = agent_e0.funnel_ladder_section(facts, leads, today=today)
    assert section["by_object"]["c1"]["events_by_step"]["eff"] == 30


def test_ladder_section_pools_direction_then_account():
    """Коэффициенты кампания занимает у своего направления, не у всего кабинета."""
    today = date(2026, 8, 22)
    leads = [_ladder_lead("2026-05-01", "spo", paid_on="2026-05-01")]
    facts = [
        # Направление spo: сделки → оплаты = 30/100.
        _ladder_fact("2026-07-01", "c1", "spo", eff_leads=300, connected_leads=200,
                     deals=100, payments_fact=30),
        # Направление dist: конверсия хуже на порядок — не должна подмешаться.
        _ladder_fact("2026-07-01", "c2", "dist", eff_leads=3000, connected_leads=2000,
                     deals=1000, payments_fact=30),
    ]
    section = agent_e0.funnel_ladder_section(facts, leads, today=today)
    c1 = section["by_object"]["c1"]
    assert c1["step"] == "paid"  # своих оплат хватает
    assert all(r["source"].startswith("direction:") for r in c1["rates"])


def test_ladder_section_avg_check_comes_from_direction():
    today = date(2026, 8, 22)
    leads = [
        _ladder_lead("2026-05-01", "spo", paid_on="2026-05-01", revenue=100000),
        _ladder_lead("2026-05-02", "spo", paid_on="2026-05-02", revenue=200000),
    ]
    facts = [_ladder_fact("2026-07-01", "c1", "spo", payments_fact=25, deals=50,
                          connected_leads=100, eff_leads=200)]
    section = agent_e0.funnel_ladder_section(facts, leads, today=today)
    # Чек spo = (100000 + 200000) / 2; ожидаемая выручка = 25 оплат × чек.
    assert section["by_object"]["c1"]["expected_revenue"] == 25 * 150000


def test_ladder_section_distribution_names_campaigns_without_step():
    today = date(2026, 8, 22)
    facts = [_ladder_fact("2026-07-01", "thin", "spo", clicks=10)]
    section = agent_e0.funnel_ladder_section(facts, [], today=today)
    assert section["distribution"] == {"нет_ступени": 1}
    assert section["without_step"] == ["thin"]
