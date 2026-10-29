# -*- coding: utf-8 -*-
"""Выбор кабинетов модуля (settings.logins) и инвентарь module_cabinets."""

from datetime import timedelta

from sync.modules import cabinets, runner
from sync.modules.accounts import DirectAccount
from sync.modules.types import Context, RunOutput
from sync.placements.direct import DirectError
from tests.test_modules_runner import NOW, FakeStore, _preview, _row

A = DirectAccount("login-a", "tok", "RUB", "X")
B = DirectAccount("Login-B", "tok", "KZT", "X")
C = DirectAccount("login-c", "tok", "RUB", "X")


class LoginsModule:
    """Запоминает кабинеты, которые получил скан."""

    def __init__(self, key="placements"):
        self.KEY = key
        self.seen = []

    def run(self, ctx: Context) -> RunOutput:
        self.seen.append([a.login for a in ctx.accounts])
        return RunOutput(findings=[])


def _go(st, m, accts=(A, B, C), inventory=None):
    return runner.run_project(
        "lime", store=st, accounts=lambda s: (list(accts), []), modules={m.KEY: m}, now=NOW,
        log=lambda x: None, agent_lock_fn=lambda s: None,
        cabinets=inventory or (lambda *a, **k: None))


# --- select_accounts ---------------------------------------------------------------------

def test_empty_logins_means_all_cabinets():
    for empty in ([], None, ["", "  "]):
        assert cabinets.select_accounts([A, B], empty) == ([A, B], [])


def test_logins_filter_case_insensitive_and_keeps_project_order():
    got, why = cabinets.select_accounts([A, B, C], ["login-c", " login-b "])
    assert got == [B, C] and why == []


def test_unknown_login_is_reason_not_exception():
    got, why = cabinets.select_accounts([A, B], ["login-a", "ghost", "ghost"])
    assert got == [A]
    assert why == ["ghost: нет среди кабинетов проекта — пропущен"]


# --- раннер ------------------------------------------------------------------------------

def test_scan_without_logins_sees_all_cabinets():
    m = LoginsModule()
    st = FakeStore([_row()])
    assert _go(st, m) == 0
    assert m.seen == [["login-a", "Login-B", "login-c"]]
    assert "skipped_logins" not in st.runs[1]["summary"]


def test_scan_only_selected_cabinets_and_reconcile_scope(monkeypatch):
    scopes = []
    orig = runner.reconcile

    def spy(mkey, existing, found, now, scanned=None, **kw):
        scopes.append(scanned)
        return orig(mkey, existing, found, now, scanned, **kw)

    monkeypatch.setattr(runner, "reconcile", spy)
    m = LoginsModule()
    st = FakeStore([_row(settings={"logins": ["login-b", "nope"]})])
    assert _go(st, m) == 0
    assert m.seen == [["Login-B"]]
    # невыбранные кабинеты не прочитаны: их находки не закрываются как исправленные
    assert scopes == [["Login-B"]]
    s = st.runs[1]["summary"]
    assert s["logins_total"] == 1
    assert s["skipped_logins"] == ["nope: нет среди кабинетов проекта — пропущен"]
    assert s["currency_by_login"] == {"Login-B": "KZT"}


def test_scan_all_selected_unknown_is_skipped_run():
    m = LoginsModule()
    st = FakeStore([_row(settings={"logins": ["ghost"]})])
    assert _go(st, m) == 0
    assert m.seen == []
    run = st.runs[1]
    assert run["status"] == "skipped"
    assert "нет выбранных кабинетов" in run["summary"]["reason"]
    assert "ghost" in run["summary"]["reason"]


def test_preview_uses_preview_settings_logins():
    m = LoginsModule()
    st = FakeStore([], previews=[_preview(settings={"logins": ["login-a", "zzz"]})])
    _go(st, m)
    p = st._preview(1)
    assert p["status"] == "done" and m.seen == [["login-a"]]
    assert p["result"]["skipped_logins"] == ["zzz: нет среди кабинетов проекта — пропущен"]


def test_preview_with_no_selected_cabinet_is_error():
    st = FakeStore([], previews=[_preview(settings={"logins": ["ghost"]})])
    _go(st, LoginsModule())
    assert "нет выбранных кабинетов" in st._preview(1)["error"]


def test_dry_run_respects_logins():
    m = LoginsModule()
    st = FakeStore([_row(mode="report", settings={"logins": ["login-c"]})])
    runner.run_project("lime", store=st, accounts=lambda s: ([A, B, C], []),
                       modules={"placements": m}, now=NOW, dry_run=True, log=lambda x: None)
    assert m.seen == [["login-c"]]


def test_inventory_called_with_all_project_cabinets_under_lock():
    calls = []
    m = LoginsModule()
    st = FakeStore([_row(settings={"logins": ["login-a"]})])
    _go(st, m, inventory=lambda slug, accts, store, now, log: calls.append(
        (slug, [a.login for a in accts])))
    assert calls == [("lime", ["login-a", "Login-B", "login-c"])]
    st = FakeStore([_row()], lock=False)
    calls.clear()
    _go(st, m, inventory=lambda *a: calls.append(a))
    assert calls == []


def test_default_inventory_without_store_support_does_not_break_run():
    # FakeStore без cabinets_checked_at: как база без таблицы — только строка в лог
    lines = []
    m = LoginsModule()
    st = FakeStore([_row()])
    rc = runner.run_project("lime", store=st, accounts=lambda s: ([A], []),
                            modules={"placements": m}, now=NOW, log=lines.append,
                            agent_lock_fn=lambda s: None)
    assert rc == 0 and m.seen == [["login-a"]]
    assert any("инвентарь не записан" in x for x in lines)


# --- инвентарь ---------------------------------------------------------------------------

def _caller(by_login):
    def call_fn(token, login, service, payload):
        assert service == "campaigns"
        assert payload["params"]["FieldNames"] == ["Id"]
        res = by_login[login]
        if isinstance(res, Exception):
            raise res
        return {"Campaigns": [{"Id": i} for i in range(res)]}
    return call_fn


def test_inventory_counts_campaigns_and_marks_denied():
    log = []
    rows = cabinets.inventory([A, B, C], log.append, _caller({
        "login-a": 3,
        "Login-B": DirectError("campaigns [54] Нет прав: нет доступа к клиенту"),
        "login-c": DirectError("campaigns [1000] Сервис временно недоступен: x"),
    }))
    assert rows == [
        {"login": "login-a", "currency": "RUB", "campaigns": 3, "access": "ok"},
        {"login": "Login-B", "currency": "KZT", "campaigns": None, "access": "denied"},
    ]
    # временный сбой строки не даёт: последнее известное состояние сохраняется
    assert any("login-c" in x for x in log)


def test_access_codes():
    for code in (53, 54, 58, 513):
        assert cabinets.access_denied(DirectError("campaigns [%d] x: y" % code))
    for code in (52, 152, 1000, 8000):
        assert not cabinets.access_denied(DirectError("campaigns [%d] x: y" % code))
    assert not cabinets.access_denied(DirectError("campaigns: Директ API 204 (недоступен)"))


class InvStore:
    def __init__(self, last=None, fail=None):
        self.last, self.fail, self.saved = last, fail, []

    def cabinets_checked_at(self, slug):
        if self.fail:
            raise self.fail
        return self.last

    def upsert_cabinets(self, slug, rows):
        self.saved.append((slug, rows))


def test_refresh_writes_when_stale_and_skips_when_fresh():
    st = InvStore(last=NOW - timedelta(hours=2))
    assert cabinets.refresh("lime", [A], st, NOW, lambda x: None, _caller({"login-a": 2})) == 1
    assert st.saved == [("lime", [{"login": "login-a", "currency": "RUB", "campaigns": 2,
                                   "access": "ok"}])]

    def no_call(*a):
        raise AssertionError("свежий инвентарь не должен ходить в Директ")
    st = InvStore(last=NOW - timedelta(minutes=10))
    assert cabinets.refresh("lime", [A], st, NOW, lambda x: None, no_call) is None
    assert st.saved == []


def test_refresh_missing_table_is_logged_not_raised():
    class UndefinedTable(Exception):
        pass

    log = []

    def no_call(*a):
        raise AssertionError("без таблицы в Директ не ходим")
    st = InvStore(fail=UndefinedTable('relation "module_cabinets" does not exist'))
    assert cabinets.refresh("lime", [A], st, NOW, log.append, no_call) is None
    assert log == ["кабинеты: инвентарь не записан (нет таблицы module_cabinets — "
                   "миграция Panda не накатана)"]


def test_refresh_without_accounts_does_nothing():
    st = InvStore(fail=AssertionError("не должен читать базу"))
    assert cabinets.refresh("lime", [], st, NOW, lambda x: None) is None
