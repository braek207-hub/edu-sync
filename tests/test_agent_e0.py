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
from datetime import date, timedelta

import sync.agent_e0 as agent_e0
from sync.agent.guard import check_freshness as real_check_freshness
from sync.agent.guard import verdict as real_verdict
from sync.agent.guard import check_sum_reconciliation
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
    "load_campaign_features", "load_device_bridge", "table_sizes",
    # Спрос Wordstat: без подмены прогон ушёл бы в реальную базу и напечатал
    # ретраи коннекта в тот же stdout, который тест парсит как JSON.
    "load_wordstat_demand",
    # Лиды со скором — сырьё тормоза роста (quality.py), та же причина.
    "load_quality_facts",
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
    # Чёрный ящик ходит в живую базу — в тестах он молчит. Своё поведение
    # он проверяет сам (tests/test_agent_blackbox.py).
    monkeypatch.setattr(agent_e0.blackbox, "save_run",
                        lambda *a, **k: {"saved": False, "error": "тест"})
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
    # Панель настроек агента: без подмены прогон пошёл бы в БД за пресетом и
    # напечатал бы ретраи коннекта в тот же stdout, который тест парсит как JSON.
    monkeypatch.setattr(agent_e0.agent_db, "load_agent_config",
                        lambda *a, **k: {"preset": None, "overrides": {}})
    # Отчёт по площадкам — отдельный поход в Директ; без подмены тесты
    # расчёта падали бы на сети, а не на своих утверждениях.
    monkeypatch.setattr(agent_e0, "fetch_placements", lambda *a, **k: ([], {}))
    # Сверка сумм гейта ходит в витрину: без подмены тест падал бы на
    # отсутствии DATABASE_URL, а не на своём утверждении.
    monkeypatch.setattr(agent_e0.agent_db, "mart_cost_total", lambda *a, **k: 0.0)
    # Граница витрины: сверка сумм берёт по ней общий интервал с источником.
    # None = витрина пуста, обе стороны нули — штатное состояние тестового прогона.
    monkeypatch.setattr(agent_e0.agent_db, "mart_last_fact_date", lambda *a, **k: None)
    # Справочник базового CPA — источник порога для кандидатов в минус-слова.
    # Пусто = порога нет, кандидаты не считаются: это штатное состояние
    # кабинета без истории, а не повод падать.
    monkeypatch.setattr(agent_e0.agent_db, "load_baseline_cpa", lambda *a, **k: {})
    # Факт месяца по кампаниям — вход пейсинга. Без подмены прогон уходит в
    # реальную базу и печатает ретраи коннекта в тот же stdout, который тест
    # парсит как JSON. Пусто = месяц ещё ничего не выбрал: план целиком впереди.
    monkeypatch.setattr(agent_e0.agent_db, "load_cost_by_campaign", lambda *a, **k: {})
    # Реестр идей: секция ideas печатается каждым прогоном, и без подмены
    # чтение уходит в реальную базу и печатает ретраи коннекта в тот же
    # stdout, который тест парсит как JSON. Пусто = реестр без открытых идей,
    # штатное состояние кабинета до Ф13.
    monkeypatch.setattr(agent_e0.ideas_registry, "open_ideas", lambda *a, **k: [])
    # Журнал применённых действий — вход петли обучения. Без подмены прогон
    # уходит в БД и печатает ретраи коннекта в тот же stdout, который тест
    # парсит как JSON (та же причина, что у панели настроек выше).
    monkeypatch.setattr(agent_e0.writer_db, "closed_actions", lambda *a, **k: [])
    # Журнал сбросов обучения — вход генератора A/B-тестов (задача 16а). Та же
    # причина подмены: без неё прогон уходит в реальную базу и печатает ретраи
    # коннекта в тот же stdout, который тест парсит как JSON.
    monkeypatch.setattr(agent_e0.writer_db, "last_learning_reset", lambda *a, **k: {})
    # Запись находок генераторов. Пусто = порция принята; настоящая запись
    # проверяется отдельно (tests/test_agent_e0_ideas.py).
    monkeypatch.setattr(agent_e0.ideas_registry, "upsert", lambda rows: list(rows))
    monkeypatch.setattr(
        agent_e0.agent_db, "upsert_computed_settings",
        # Значения по умолчанию намеренно: на коде ДО правки вызов идёт без
        # object_id, и тест обязан упасть на утверждении о ключе, а не на TypeError.
        lambda rows, calc_date=None, object_id=None, object_level="account":
            calls.append({"rows": list(rows), "object_id": object_id,
                          "object_level": object_level}) or len(rows),
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


def _bridge_leads(device, n, eff=1.0, conn=1.0, deal=1.0, paid=0.0):
    n_eff, n_conn = int(n * eff), int(n * eff * conn)
    n_deal = int(n * eff * conn * deal)
    n_paid = int(n * eff * conn * deal * paid)
    return [{"device": device, "is_eff": i < n_eff, "is_connected": i < n_conn,
             "is_deal": i < n_deal, "is_paid": i < n_paid,
             "amount": 100000.0 if i < n_paid else None} for i in range(n)]


def test_main_multiplies_device_modifiers_by_lead_quality(monkeypatch, capsys):
    """Э2.2b сквозняком: сегмент с лучшей конверсией в лид, но плохим качеством
    лида (соединение/деньги из моста) не получает плюс по одной конверсии."""
    import json as _json

    calls = _patch_e0_run(monkeypatch)
    # ПК доводит лиды до денег, смартфоны — вдвое хуже по соединению и оплате.
    bridge = (_bridge_leads("ПК", 700, eff=0.9, conn=0.5, deal=0.5, paid=0.5)
              + _bridge_leads("Смартфоны", 800, eff=0.9, conn=0.2, deal=0.5, paid=0.2))
    monkeypatch.setattr(agent_e0.agent_db, "load_device_bridge",
                        lambda *a, **k: bridge)

    assert agent_e0.main() == 0
    report = _json.loads(capsys.readouterr().out)

    written = [r for call in calls if call["object_id"] == "acc-1" for r in call["rows"]]
    mobile = next(r for r in written if r["setting_key"] == "MOBILE")
    # По конверсии в лид MOBILE упирался в потолок +50; качество лида ~0.6
    # обязано срезать корректировку, и обе компоненты остаются видимыми.
    assert mobile["quality_ratio"] < 1.0
    assert mobile["conv_ratio"] > 1.0
    assert mobile["value"] < 50.0

    # Планшетов в мосте нет — TABLET (acc-2) остаётся на чистой конверсии,
    # отсутствие данных о качестве не обнуляет корректировку молча.
    tablet = next(r for call in calls if call["object_id"] == "acc-2"
                  for r in call["rows"] if r["setting_key"] == "TABLET")
    assert "quality_ratio" not in tablet

    quality = report["device_quality"]
    assert quality["reason"] is None
    assert set(quality["ratios"]) == {"DESKTOP", "MOBILE"}
    assert quality["modifiers_adjusted"] >= 2


def test_main_without_bridge_keeps_pure_conversion_and_names_reason(monkeypatch, capsys):
    import json as _json

    calls = _patch_e0_run(monkeypatch)  # мост пуст по умолчанию

    assert agent_e0.main() == 0
    report = _json.loads(capsys.readouterr().out)

    written = [r for call in calls for r in call["rows"]]
    assert written and all("quality_ratio" not in r for r in written)
    assert report["device_quality"]["modifiers_adjusted"] == 0
    assert report["device_quality"]["reason"]


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
    # Кликов достаточно, чтобы отсутствие конверсий что-то значило: правило
    # трёх против базовой конверсии набора (agent/objects.py). На десяти
    # кликах «ноль конверсий» — отсутствие наблюдений, а не приговор.
    return [{"query": q, "cost": cost, "conversions": conv, "clicks": 300,
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
                                  ("дорогой рабочий", 7000.0, 30)) if login == "acc-1"
                         else [], {"goal_column": "Conversions_111_LSCCD", "conversions": 7, "columns_offered": 1}))

    assert agent_e0.main() == 0
    report = _json.loads(capsys.readouterr().out)["minus_word_candidates"]

    # Порог — МЕДИАНА справочника (2000), а не среднее (4000): среднее тянет
    # вверх единичная дорогая кампания, и «втрое дороже» перестаёт значить.
    assert report["cpa_limit"] == 2000.0
    assert report["sample"] == ["дорогой мусор"]
    assert report["count"] == 1
    assert report["cost_burned"] == 7000.0
    assert report["by_reason"] == {"zero_conversions": 1}


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


def test_main_writes_campaign_level_device_modifiers(monkeypatch, capsys):
    """Э2.2: покампанийные device-строки едут под object_level='campaign'."""
    calls = _patch_e0_run(monkeypatch)
    week = "2026-08-17"
    campaign_rows = [
        {"date": week, "campaign_id": "111", "slice_key": "MOBILE",
         "clicks": 20000, "conversions": 2000, "cost": 1.0, "impressions": 1},
        {"date": week, "campaign_id": "111", "slice_key": "DESKTOP",
         "clicks": 20000, "conversions": 500, "cost": 1.0, "impressions": 1},
    ]
    monkeypatch.setattr(
        agent_e0, "fetch_segment_report",
        lambda login, kind, date_from, date_to, by_campaign=False, goals=():
            ((list(campaign_rows) if (by_campaign and kind == "device"
                                      and login == "acc-1") else [])
             if by_campaign else
             (list(_REPORTS_BY_LOGIN.get(login, [])) if kind == "device" else []),
             {"goal_column": "Conversions_111_LSCCD", "conversions": 1,
              "columns_offered": 1}),
    )

    assert agent_e0.main() == 0
    report = _json.loads(capsys.readouterr().out)

    campaign_calls = [c for c in calls if c["object_level"] == "campaign"]
    assert campaign_calls, "покампанийная запись не случилась"
    assert campaign_calls[0]["object_id"] == "111"
    kinds = {r["setting_kind"] for c in campaign_calls for r in c["rows"]}
    assert kinds == {"bid_modifier:device"}

    summary = report["campaign_modifiers"]["acc-1"]
    assert summary["campaigns"] == 1
    # Сверка с кабинетным уровнем — в отчёте: без неё цену Э2.2 не увидеть.
    assert summary["top_deltas_vs_account"]
    # Кабинет без покампанийных данных честно назван с причиной, а не молчит.
    assert any(sk["account"] == "acc-2"
               for sk in report["campaign_modifiers_skipped"])


# ------------------------- p90 лага: правая цензура свежих когорт


def test_lag_percentile_ignores_censored_young_cohorts():
    # Свежая когорта физически не может показать длинный лаг: её длинные
    # оплаты ещё не случились. Считать её лаги наравне со зрелыми — правая
    # цензура: p90 занижается, «зрелое» окно оказывается незрелым, value
    # направлений с длинным лагом занижена. Лаг меряется только по когортам,
    # у которых было время оплатиться (возраст ≥ LAG_COHORT_MIN_AGE_DAYS).
    today = date(2026, 8, 22)
    # Когорта старше LAG_COHORT_MIN_AGE_DAYS: её хвост уже виден целиком.
    mature = [_ladder_lead("2026-01-05", "spo",
                           paid_on=(date(2026, 1, 5)
                                    + timedelta(days=lag)).isoformat())
              for lag in (30, 35, 40, 45, 50)]
    young = [_ladder_lead("2026-08-15", "spo", paid_on="2026-08-15")] * 20
    # min_age_days=0 воспроизводит прежнее поведение: все когорты подряд.
    censored_p90 = agent_e0._lag_percentile(mature + young, 0.90, today=today,
                                            min_age_days=0)
    honest_p90 = agent_e0._lag_percentile(mature + young, 0.90, today=today)
    assert censored_p90 == 40   # молодые нули утопили хвост
    assert honest_p90 == 50     # только зрелые когорты


def test_lag_percentile_falls_back_when_no_mature_cohorts():
    # Истории меньше порога зрелости — честного замера нет; цензурированная
    # оценка всё же лучше нуля (ноль = «окно не сдвигать» = решения по
    # полностью незрелым дням).
    today = date(2026, 8, 22)
    young = [_ladder_lead("2026-08-10", "spo", paid_on="2026-08-15")] * 5
    assert agent_e0._lag_percentile(young, 0.90, today=today) == 5


def test_ladder_section_uses_uncensored_lag():
    today = date(2026, 8, 22)
    # Когорта старше LAG_COHORT_MIN_AGE_DAYS: её хвост уже виден целиком.
    mature = [_ladder_lead("2026-01-05", "spo",
                           paid_on=(date(2026, 1, 5)
                                    + timedelta(days=lag)).isoformat())
              for lag in (30, 35, 40, 45, 50)]
    young = [_ladder_lead("2026-08-15", "spo", paid_on="2026-08-15")] * 20
    section = agent_e0.funnel_ladder_section([], mature + young, today=today)
    assert section["maturity_days"] == 50


def test_report_carries_the_learning_loop_over_own_actions(monkeypatch, capsys):
    """Э0 печатает, чем закончились СОБСТВЕННЫЕ действия агента.

    Без этой секции петля обучения посчиталась бы в никуда: модуль есть,
    вызова из такта нет — ровно тот класс отказа, против которого поставлен
    tests/test_no_orphan_code.py.
    """
    import json as _json

    _patch_e0_run(monkeypatch)
    monkeypatch.setattr(agent_e0.writer_db, "closed_actions", lambda *a, **k: [
        {"action_kind": "budget.set", "closing_verdict": "improved",
         "expected_leads_delta": 10.0, "observed_leads_delta": 5.0},
        {"action_kind": "budget.set", "closing_verdict": "worsened",
         "expected_leads_delta": 10.0, "observed_leads_delta": 3.0},
    ])

    assert agent_e0.main() == 0
    loop = _json.loads(capsys.readouterr().out)["learning_loop"]

    assert loop["closed_actions"] == 2
    assert loop["track_record"]["budget.set"]["hit_rate"] == 0.5
    assert loop["track_record"]["budget.set"]["hit_rate_up"] == 0.5
    # Ключ калибровки — вид действия ПЛЮС направление.
    assert loop["forecast_bias"]["budget.set:up"]["n"] == 2


def test_unavailable_journal_does_not_kill_the_calculation(monkeypatch, capsys):
    # Петля — отчётный слой поверх расчёта: недоступность журнала обязана быть
    # видна причиной, а не ронять весь такт Э0.
    import json as _json

    def _boom(*a, **k):
        raise RuntimeError("журнал недоступен")

    _patch_e0_run(monkeypatch)
    monkeypatch.setattr(agent_e0.writer_db, "closed_actions", _boom)

    assert agent_e0.main() == 0
    loop = _json.loads(capsys.readouterr().out)["learning_loop"]
    assert "журнал недоступен" in loop["unavailable"]


def test_report_names_what_to_strengthen_and_how_much_fits(monkeypatch, capsys):
    """Э0 печатает не только «что срезать», но и «что усилить».

    Секция обязана быть в каждом такте, в том числе пустая: молчание про
    усиление неотличимо от «усиливать нечего», а агент, отвечающий только на
    первый вопрос, ведёт кабинет к «эффективно и мало».

    Денег в ней ДВА числа, а не одно: доливка бюджета доедет только там, где
    лимит связывает расход (9 кампаний из 62), остальным нужна цена
    конверсии. Задача 11 растит кабинет на room_rub_budget, и сумма обязана
    сходиться с разбивкой.
    """
    import json as _json

    _patch_e0_run(monkeypatch)
    monkeypatch.setattr(agent_e0, "demand_regime",
                        lambda *a, **k: {"vpo": {"regime": "подъём"},
                                         "spo": {"regime": "норма"}})

    assert agent_e0.main() == 0
    growth = _json.loads(capsys.readouterr().out)["growth"]

    # Режим спроса доезжает до списка усиления, а не остаётся сам по себе.
    assert growth["directions_rising"] == ["vpo"]
    assert isinstance(growth["candidates"], list)
    assert growth["room_rub_total"] == (growth["room_rub_budget"]
                                        + growth["room_rub_tcpa"])
    # Качество когорты (задача 14) ещё не считается: тормоза нет, и никого
    # он не снимает.
    assert growth["skipped_by_quality"] == 0


def test_quality_brake_reaches_the_report_and_the_growth_list(monkeypatch, capsys):
    # Тормоз роста бесполезен, если посчитан и никуда не доехал: кандидат с
    # испортившейся когортой обязан выпасть из списка усиления, а не просто
    # получить строчку в отчёте. Проверяются обе половины сразу — раздельно
    # они выживали бы поодиночке.
    _patch_e0_run(monkeypatch)
    seen = {}

    def _drifting(rows, before_from, before_to, after_from, after_to):
        seen["windows"] = (before_from, before_to, after_from, after_to)
        return {"window_before": [before_from, before_to],
                "window_after": [after_from, after_to],
                "flagged": [{"campaign_id": "111", "drop": 0.4}],
                "coverage_alerts": [],
                "drift": {"111": {"drop": 0.4, "flagged": True,
                                  "reason": "качество когорты упало"}}}

    monkeypatch.setattr(agent_e0, "lead_quality_section", _drifting)

    captured = {}
    original = agent_e0.growth_candidates

    def _spy(*args, **kwargs):
        captured["quality_drift"] = kwargs.get("quality_drift")
        return original(*args, **kwargs)

    monkeypatch.setattr(agent_e0, "growth_candidates", _spy)

    assert agent_e0.main() == 0
    # Первый объект вывода: такт печатает отчёт, а следом могут идти
    # служебные строки прогона.
    import json as _json
    report = _json.JSONDecoder().raw_decode(capsys.readouterr().out.lstrip())[0]

    assert report["lead_quality"]["flagged"] == [{"campaign_id": "111", "drop": 0.4}]
    # Полная карта дрейфа в отчёт не едет: это сотня строк служебных чисел,
    # из-за которых важное в логе не найти.
    assert "drift" not in report["lead_quality"]
    assert captured["quality_drift"] == {"111": {"drop": 0.4, "flagged": True,
                                                 "reason": "качество когорты упало"}}
    # Окна смежные и не пересекаются: иначе одна и та же когорта сравнивалась
    # бы сама с собой и падение размывалось.
    before_from, before_to, after_from, after_to = seen["windows"]
    assert before_from < before_to < after_from < after_to


def _spy_portfolio(monkeypatch):
    """Перехват вызовов солвера: чем такт кормит раскладку бюджетов."""
    captured = []
    original = agent_e0.portfolio_targets

    def _spy(*args, **kwargs):
        captured.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(agent_e0, "portfolio_targets", _spy)
    return captured


def test_account_growth_is_proposed_while_the_monthly_cap_is_empty(monkeypatch, capsys):
    """Такт считает запас кабинета и печатает предложение роста.

    Запас «сколько кабинет освоит СЕГОДНЯ» виден только после того, как
    солвер назвал цели, поэтому раскладок две: первая — ради запаса, вторая —
    итоговая, с выросшим бюджетом. Пока потолок месяца не задан, рост не
    применяется: общий бюджет — деньги владельца.
    """
    import json as _json

    _patch_e0_run(monkeypatch)
    captured = _spy_portfolio(monkeypatch)

    assert agent_e0.main() == 0
    report = _json.JSONDecoder().raw_decode(capsys.readouterr().out.lstrip())[0]

    assert len(captured) == 2
    assert "room_rub_by_login" not in captured[0]
    assert isinstance(captured[1]["room_rub_by_login"], dict)
    # Ключ панели пуст — потолка нет, и солвер об этом знает.
    assert captured[1]["monthly_cap_rub"] is None
    assert captured[1]["target_romi"] == 1.0
    # Секция печатается всегда, по кабинету: молчание про рост неотличимо от
    # «расти некуда».
    assert set(report["budget_growth"]) == set(
        report["budget_threshold"]["accounts"])


def test_monthly_cap_from_the_panel_reaches_the_solver(monkeypatch, capsys):
    # Потолок освоения ставит человек в панели настроек, и путь от неё до
    # раскладки обязан быть проверен: настройка, которая никуда не доехала,
    # выглядит применённой.
    _patch_e0_run(monkeypatch)
    monkeypatch.setattr(
        agent_e0.agent_db, "load_agent_config",
        lambda *a, **k: {"preset": None,
                         "overrides": {"monthly_budget_cap_rub": 3_000_000.0,
                                       "target_romi": 2.0}})
    captured = _spy_portfolio(monkeypatch)

    assert agent_e0.main() == 0
    capsys.readouterr()

    assert captured[1]["monthly_cap_rub"] == 3_000_000.0
    assert captured[1]["target_romi"] == 2.0


def test_the_month_plan_reaches_the_solver_by_account(monkeypatch, capsys):
    # Пейсинг считается в такте, а работает у солвера: план, который никуда
    # не доехал, выглядит применённым и молча оставляет кабинет на трейлинге.
    _patch_e0_run(monkeypatch)
    monkeypatch.setattr(
        agent_e0.agent_db, "load_agent_config",
        lambda *a, **k: {"preset": None,
                         "overrides": {"monthly_budget_cap_rub": 3_000_000.0}})
    captured = _spy_portfolio(monkeypatch)

    assert agent_e0.main() == 0
    capsys.readouterr()

    pace = captured[1]["pace_by_login"]
    assert pace, "план месяца обязан доехать до итоговой раскладки"
    assert all(plan["target_rub"] == 3_000_000.0 for plan in pace.values())
    assert all(plan["daily_allowance"] > 0 for plan in pace.values())
    # Первая раскладка идёт БЕЗ плана намеренно: она считает запас по целям,
    # а запас — вход самого плана. План там означал бы, что потолок окна
    # посчитан по числам, которых ещё нет.
    assert "pace_by_login" not in captured[0]


def test_the_month_plan_is_printed_with_what_is_already_spent(monkeypatch, capsys):
    # Число «бюджет кабинета» без плана месяца — число ниоткуда: непонятно,
    # догоняет агент отставание или тормозит перебор. Факт месяца берётся ПО
    # ВЧЕРАШНИЙ день: сегодняшний ещё идёт и в остатке дней уже учтён.
    import json as _json
    from datetime import date as _date

    _patch_e0_run(monkeypatch)
    windows = []
    monkeypatch.setattr(agent_e0.agent_db, "load_cost_by_campaign",
                        lambda *a, **k: (windows.append(a), {})[1])
    monkeypatch.setattr(
        agent_e0.agent_db, "load_agent_config",
        lambda *a, **k: {"preset": None,
                         "overrides": {"monthly_budget_cap_rub": 3_000_000.0}})

    assert agent_e0.main() == 0
    report = _json.JSONDecoder().raw_decode(capsys.readouterr().out.lstrip())[0]

    assert report["pacing"]["month"] == _date.today().strftime("%Y-%m")
    assert report["pacing"]["unavailable"] is None
    month_window = [w for w in windows
                    if w[0] == _date.today().replace(day=1).isoformat()]
    assert month_window and month_window[0][1] < _date.today().isoformat()


def test_a_month_without_facts_does_not_break_the_tact(monkeypatch, capsys):
    # Витрина недоступна — плана нет, и солвер считает потолок окна
    # по-старому. Падать такт не имеет права: пейсинг это слой поверх
    # раскладки, а не её условие.
    import json as _json

    _patch_e0_run(monkeypatch)
    monkeypatch.setattr(agent_e0.agent_db, "load_cost_by_campaign",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("нет связи")))

    assert agent_e0.main() == 0
    report = _json.JSONDecoder().raw_decode(capsys.readouterr().out.lstrip())[0]

    assert report["pacing"]["accounts"] == {}
    assert "нет связи" in report["pacing"]["unavailable"]


def test_blind_share_is_measured_on_both_windows(monkeypatch, capsys):
    """Слепая доля печатается за окно решений и за окно лестницы.

    Замер 25.08.2026: 30,3 % на окне лестницы против 14,4 % на окне решений,
    и вся разница — кампании, которые давно не тратят. Одно число вместо двух
    заставляло бы не доверять живым данным из-за архива.
    """
    import json as _json

    _patch_e0_run(monkeypatch)
    assert agent_e0.main() == 0
    report = _json.JSONDecoder().raw_decode(capsys.readouterr().out.lstrip())[0]

    blind = report["blind_spend"]
    assert set(blind) == {"decision_window", "ladder_window"}
    decision, ladder = blind["decision_window"], blind["ladder_window"]
    # Окно решений короче и кончается там же, поэтому его начало позже.
    assert decision["window"][1] == ladder["window"][1]
    assert decision["window"][0] > ladder["window"][0]
    for section in (decision, ladder):
        assert "blind_share" in section
        assert "cost_blind" in section


def test_forecast_bias_reaches_the_solver(monkeypatch, capsys):
    # Петля обучения замкнута только тогда, когда измеренное смещение
    # доезжает до расчёта. Считать его и не подавать — то же, что не считать.
    _patch_e0_run(monkeypatch)
    captured = _spy_portfolio(monkeypatch)
    bias = {"budget.set:up": {"ratio": 0.4, "shrunk_ratio": 0.7, "n": 12}}
    monkeypatch.setattr(agent_e0, "forecast_bias", lambda closed: bias)

    assert agent_e0.main() == 0
    capsys.readouterr()

    # Предварительная раскладка идёт без поправки намеренно: она считает
    # запас по целям, а цели поправка не трогает.
    assert "forecast_bias" not in captured[0]
    assert captured[1]["forecast_bias"] == bias


# ─────────────────────────────────────────────────────────────────────────────
# Сверка сумм: источник против витрины.
#
# Инцидент 27.08.2026: e0 упал на своей же сверке — 141 287 763 против
# 139 824 423 (1,04 % при пороге 1 %) — и не записал факты. Разница до копейки
# лежала в двух недописанных днях (26.08 недобран, 27.08 отсутствует), ни одна
# кампания не терялась. Клин самоподдерживающийся: пока факты не записаны,
# разрыв растёт, и следующий прогон падает вернее предыдущего.
# ─────────────────────────────────────────────────────────────────────────────

def test_sum_check_ignores_days_the_mart_has_not_seen():
    daily = [("2026-08-24", 1_000_000.0), ("2026-08-25", 900_000.0),
             ("2026-08-26", 1_016_965.0), ("2026-08-27", 535_149.0)]
    # Витрина дописана по 25.08 — свежие два дня её ещё не касались.
    assert agent_e0._cost_up_to(daily, "2026-08-25") == 1_900_000.0


def test_missed_run_no_longer_fails_the_gate():
    """Пропуск прогона отставляет витрину, но сохранность данных не меняет."""
    daily = [("2026-08-24", 1_000_000.0), ("2026-08-25", 900_000.0),
             ("2026-08-26", 1_016_965.0), ("2026-08-27", 535_149.0)]
    mart_last = "2026-08-25"
    check = check_sum_reconciliation(
        agent_e0._cost_up_to(daily, mart_last),
        1_900_000.0,  # витрина за те же дни
    )
    assert check["status"] == "OK"


def test_full_window_comparison_would_have_failed():
    """Контроль: старая форма сверки на тех же данных краснела на ровном месте."""
    daily = [("2026-08-24", 1_000_000.0), ("2026-08-25", 900_000.0),
             ("2026-08-26", 1_016_965.0), ("2026-08-27", 535_149.0)]
    check = check_sum_reconciliation(sum(c for _, c in daily), 1_900_000.0)
    assert check["status"] == "FAIL"


def test_lost_campaigns_still_turn_the_gate_red():
    """Правка окна не ослабила проверку: потеря строк на общих днях краснеет."""
    daily = [("2026-08-24", 1_000_000.0), ("2026-08-25", 900_000.0)]
    check = check_sum_reconciliation(
        agent_e0._cost_up_to(daily, "2026-08-25"),
        1_700_000.0,  # в витрине не хватает 200 000 — это 10,5 %
    )
    assert check["status"] == "FAIL"
    assert check["detail"]["diff_share"] > 0.01


def test_empty_mart_is_not_a_breakage():
    """Первый прогон: витрина пуста, сверять не с чем — это не поломка."""
    daily = [("2026-08-24", 1_000_000.0)]
    check = check_sum_reconciliation(agent_e0._cost_up_to(daily, None), 0.0)
    assert check["status"] == "OK"
