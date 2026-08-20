# -*- coding: utf-8 -*-
"""
tests/test_agent_e0.py — тесты расчётной стороны Э0.

Чистые функции, без сети и без БД.

Дефект 1: расчёт вычисленных настроек идёт в цикле по кабинетам, но результат
складывался в один общий список и записывался с захардкоженным идентификатором
объекта. Первичный ключ таблицы включает этот идентификатор, запись построчная —
дубликаты не падали, а тихо перетирали друг друга: в базе выживали числа того
кабинета, который дописался последним. Кабинет обязан ехать вместе с числами.
"""

import sync.agent_e0 as agent_e0


def _segment_rows(clicks: int, p_pay: float):
    return [{"segment_kind": "device", "segment_key": "MOBILE",
             "clicks": clicks, "sum_p_pay": p_pay}]


def test_computed_rows_carry_their_own_account():
    job_a = {"purpose": "computed", "login": "acc-1", "kind": "device",
             "rows": _segment_rows(clicks=1000, p_pay=0.0)}
    job_b = {"purpose": "computed", "login": "acc-2", "kind": "device",
             "rows": _segment_rows(clicks=1000, p_pay=0.0)}

    login_a, rows_a = agent_e0.computed_rows_for_job(job_a, base_conv=1.0, base_expected=2000.0)
    login_b, rows_b = agent_e0.computed_rows_for_job(job_b, base_conv=1.0, base_expected=1000.0)

    assert login_a == "acc-1"
    assert login_b == "acc-2"
    # Числа кабинетов разные (разный объём ожидаемых оплат) — именно поэтому
    # их нельзя писать под общим ключом: один набор перетирал бы другой.
    assert rows_a and rows_b
    assert rows_a[0]["value"] != rows_b[0]["value"]


def test_computed_rows_are_empty_when_segment_below_support():
    job = {"purpose": "computed", "login": "acc-1", "kind": "device",
           "rows": _segment_rows(clicks=5, p_pay=100.0)}

    login, rows = agent_e0.computed_rows_for_job(job, base_conv=1.0, base_expected=100.0)

    assert login == "acc-1"
    assert rows == []


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

# Отчёты Директа по кабинетам: у каждого свой сегмент и свой объём кликов —
# именно поэтому их числа нельзя писать под общим ключом.
_REPORTS_BY_LOGIN = {
    "acc-1": [{"segment_kind": "device", "segment_key": "MOBILE", "clicks": 900}],
    "acc-2": [{"segment_kind": "device", "segment_key": "DESKTOP", "clicks": 400}],
}


def _fact(today):
    return {
        "fact_date": today, "campaign_id": "111", "campaign_name": "к1",
        "direction": "vuz", "cost": 1000.0, "clicks": 1000, "leads": 10,
        "eff_leads": 5, "sum_p_pay": 500.0,
    }


def _patch_e0_run(monkeypatch):
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
    monkeypatch.setattr(
        agent_e0, "fetch_segment_report",
        # Числа отдаёт только срез по устройствам: пол и возраст оставлены
        # пустыми, чтобы в проверке участвовали ровно две строки — по одной
        # на кабинет, и разбивка отчёта читалась однозначно.
        lambda login, kind, date_from, date_to, by_campaign=False, goals=():
            [] if (by_campaign or kind != "device")
            else list(_REPORTS_BY_LOGIN.get(login, [])),
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
    assert keys_1 == {"MOBILE"}
    assert keys_2 == {"DESKTOP"}


def test_main_reports_computed_settings_per_account(monkeypatch, capsys):
    # Тот же инвариант в отчёте прогона: разбивка по кабинетам обязана быть
    # видна глазами, иначе схлопывание в один ключ снова пройдёт незамеченным.
    import json as _json

    _patch_e0_run(monkeypatch)

    assert agent_e0.main() == 0

    report = _json.loads(capsys.readouterr().out)
    assert set(report["computed_settings_by_account"]) == {"acc-1", "acc-2"}
    assert report["computed_settings"] == 2
