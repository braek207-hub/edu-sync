# -*- coding: utf-8 -*-
"""Выбор кабинетов и кампаний модуля (settings.logins, settings.campaigns) и инвентарь
module_cabinets / module_campaigns."""

from datetime import timedelta

from sync.modules import audit, cabinets, queries, runner
from sync.modules.lifecycle import reconcile
from sync.modules.accounts import DirectAccount
from sync.modules.types import Context, Finding, RunOutput, StoredFinding, fingerprint
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


# --- выбор кампаний ----------------------------------------------------------------------

def test_campaigns_go_to_selected_cabinet_only():
    got, why = cabinets.select_accounts([A, B], [], {"LOGIN-A": [5, 3, 5]})
    assert [a.login for a in got] == ["login-a", "Login-B"]
    assert got[0].campaign_ids == (3, 5) and got[1].campaign_ids is None
    assert why == []


def test_campaigns_of_unknown_or_unselected_cabinet_are_reasons():
    got, why = cabinets.select_accounts([A, B], ["login-a"],
                                        {"login-b": [1], "ghost": [2], "login-a": []})
    assert [a.login for a in got] == ["login-a"] and got[0].campaign_ids is None
    assert why == ["login-b: кампании выбраны, но кабинет не выбран — пропущены",
                   "ghost: кампании выбраны, но кабинет не из проекта — пропущены"]


def test_scanned_campaigns_only_for_cabinets_with_choice():
    got, _ = cabinets.select_accounts([A, B], [], {"login-a": [9]})
    assert cabinets.scanned_campaigns(got) == {"login-a": frozenset({9})}


def test_with_ids_keep_rows_unmatched():
    a = cabinets.select_accounts([A], [], {"login-a": [1, 2]})[0][0]
    assert cabinets.with_ids(a, {"States": ["ON"]}) == {"States": ["ON"], "Ids": [1, 2]}
    assert cabinets.with_ids(A, {"States": ["ON"]}) == {"States": ["ON"]}
    rows = [{"campaign_id": "1"}, {"campaign_id": "3"}, {"campaign_id": None}, {}]
    assert cabinets.keep_rows(a, rows) == [{"campaign_id": "1"}]
    assert cabinets.keep_rows(A, rows) == rows
    assert cabinets.unmatched_lines(a, ["1", 7]) == [
        "login-a: кампания 2 не найдена или не подходит модулю — пропущена"]
    assert cabinets.unmatched_lines(A, []) == []


def _f(cid):
    return Finding(rule="junk", severity="info", title="t", login="acc", campaign_id=cid,
                   campaign_name="К", object_type="site", object_id="s.ru",
                   details={}, action=None)


def _stored(f, id):
    return StoredFinding(**f.__dict__, id=id, status="open",
                         fingerprint=fingerprint("placements", f),
                         last_seen_at=NOW - timedelta(hours=100), decided_at=None,
                         outcome_at=None)


def test_reconcile_keeps_findings_of_unselected_campaigns():
    ex = [_stored(_f(1), 1), _stored(_f(2), 2), _stored(_f(None), 3)]
    r = reconcile("placements", ex, [], NOW, scanned_logins=["acc"],
                  scanned_campaigns={"acc": frozenset({1})})
    # кампания 2 не выбрана — её не читали; находка уровня кабинета прочитана
    assert sorted(r.resolve) == [1, 3]
    r = reconcile("placements", ex, [], NOW, scanned_logins=["acc"])
    assert sorted(r.resolve) == [1, 2, 3]


SKIP9 = "login-a: кампания 9 не найдена или не подходит модулю — пропущена"


class CampaignsModule(LoginsModule):
    def run(self, ctx):
        self.seen.append([(a.login, a.campaign_ids) for a in ctx.accounts])
        return RunOutput(findings=[], skipped_campaigns=[SKIP9])


def test_runner_passes_campaigns_and_scope(monkeypatch):
    scopes = []
    orig = runner.reconcile

    def spy(mkey, existing, found, now, scanned=None, **kw):
        scopes.append(kw.get("scanned_campaigns"))
        return orig(mkey, existing, found, now, scanned, **kw)

    monkeypatch.setattr(runner, "reconcile", spy)
    m = CampaignsModule()
    st = FakeStore([_row(settings={"campaigns": {"login-a": [9, "4"]}})])
    assert _go(st, m) == 0
    assert m.seen == [[("login-a", (4, 9)), ("Login-B", None), ("login-c", None)]]
    assert scopes == [{"login-a": frozenset({4, 9})}]
    assert st.runs[1]["summary"]["skipped_campaigns"] == [SKIP9]


def test_preview_result_has_skipped_campaigns():
    st = FakeStore([], previews=[_preview(settings={"campaigns": {"login-a": [9]}})])
    _go(st, CampaignsModule())
    assert st._preview(1)["result"]["skipped_campaigns"] == [SKIP9]


# --- модули: выбор уходит в запрос к Директу ---------------------------------------------

SEL = DirectAccount("login-a", "tok", "RUB", "X", campaign_ids=(11, 12))


def test_queries_and_audit_ask_direct_by_ids(monkeypatch):
    crits = []

    def get_all(token, login, service, criteria, fields, **kw):
        assert service == "campaigns"
        crits.append(dict(criteria))
        return [{"Id": 11, "Type": "TEXT_CAMPAIGN", "State": "ON"}]

    monkeypatch.setattr(queries.direct_api, "get_all", get_all)
    queries._campaigns(SEL)
    audit._campaigns(SEL, {})
    assert crits == [{"Ids": [11, 12]}, {"States": ["ON"], "Ids": [11, 12]}]
    crits.clear()
    queries._campaigns(A)
    audit._campaigns(A, {})
    assert crits == [{}, {"States": ["ON"]}]


def test_audit_run_reports_unmatched_campaign(monkeypatch):
    monkeypatch.setattr(audit, "fetch_snapshot", lambda acc, win, step=None: audit.Snapshot(
        login=acc.login, campaigns=[{"Id": 11}], groups=[], ads=[], keywords=[],
        audiences_by_group={}, impressions_3d={}, unavailable={}, impressions_from=win[0]))
    monkeypatch.setattr(audit, "judge", lambda snap, settings: [])
    ctx = Context(slug="lime", module_key="audit", mode="report", settings={},
                  accounts=[SEL], now=NOW, log=lambda x: None)
    out = audit.run(ctx)
    assert out.skipped_campaigns == [
        "login-a: кампания 12 не найдена или не подходит модулю — пропущена"]


# --- инвентарь ---------------------------------------------------------------------------

def _camp(i, kind="TEXT_CAMPAIGN", state="ON", search="HIGHEST_POSITION", network=None):
    c = {"Id": i, "Name": "К%d" % i, "Type": kind, "State": state}
    block = cabinets.STRATEGY_BLOCKS.get(kind)
    if block and (search or network):
        bs = {}
        if search:
            bs["Search"] = {"BiddingStrategyType": search}
        if network:
            bs["Network"] = {"BiddingStrategyType": network}
        c[block] = {"BiddingStrategy": bs}
    return c


def _caller(by_login, payloads=None):
    def call_fn(token, login, service, payload):
        assert service == "campaigns"
        if payloads is not None:
            payloads.append(payload["params"])
        res = by_login[login]
        if isinstance(res, Exception):
            raise res
        if isinstance(res, int):
            res = [_camp(i + 1) for i in range(res)]
        return {"Campaigns": res}
    return call_fn


def test_read_campaigns_fields_states_and_channels():
    params = []
    got = cabinets.read_campaigns(A, _caller({"login-a": [
        _camp(1, search="HIGHEST_POSITION", network="SERVING_OFF"),
        _camp(2, kind="UNIFIED_CAMPAIGN", search="SERVING_OFF", network="AVERAGE_CPC"),
        _camp(3, kind="DYNAMIC_TEXT_CAMPAIGN"),
        _camp(4, state="ARCHIVED"),
    ]}, params))
    p = params[0]
    assert p["FieldNames"] == ["Id", "Name", "Type", "State"]
    assert p["SelectionCriteria"] == {"States": cabinets.LIVE_STATES}
    assert p["TextCampaignFieldNames"] == ["BiddingStrategy"]
    assert p["UnifiedCampaignFieldNames"] == ["BiddingStrategy"]
    assert got == [
        {"id": 1, "name": "К1", "type": "TEXT_CAMPAIGN", "state": "ON", "search": True,
         "network": False},
        {"id": 2, "name": "К2", "type": "UNIFIED_CAMPAIGN", "state": "ON", "search": False,
         "network": True},
        {"id": 3, "name": "К3", "type": "DYNAMIC_TEXT_CAMPAIGN", "state": "ON", "search": None,
         "network": None},
    ]


def test_read_campaigns_unknown_block_falls_back_without_strategy():
    params = []

    def call_fn(token, login, service, payload):
        params.append(payload["params"])
        if "TextCampaignFieldNames" in payload["params"]:
            raise DirectError("campaigns [8000] Неверный запрос: неизвестное поле")
        return {"Campaigns": [{"Id": 5, "Name": "x", "Type": "TEXT_CAMPAIGN", "State": "OFF"}]}

    got = cabinets.read_campaigns(A, call_fn)
    assert len(params) == 2
    assert got == [{"id": 5, "name": "x", "type": "TEXT_CAMPAIGN", "state": "OFF",
                    "search": None, "network": None}]


def test_inventory_counts_campaigns_and_marks_denied():
    log = []
    rows, camps = cabinets.inventory([A, B, C], log.append, _caller({
        "login-a": 3,
        "Login-B": DirectError("campaigns [54] Нет прав: нет доступа к клиенту"),
        "login-c": DirectError("campaigns [1000] Сервис временно недоступен: x"),
    }))
    assert rows == [
        {"login": "login-a", "currency": "RUB", "campaigns": 3, "access": "ok"},
        {"login": "Login-B", "currency": "KZT", "campaigns": None, "access": "denied"},
    ]
    # кампании — только прочитанного кабинета; у denied и упавшего список не трогаем
    assert list(camps) == ["login-a"] and [c["id"] for c in camps["login-a"]] == [1, 2, 3]
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
        self.last, self.fail, self.saved, self.camps = last, fail, [], []

    def cabinets_checked_at(self, slug):
        if self.fail:
            raise self.fail
        return self.last

    def upsert_cabinets(self, slug, rows):
        self.saved.append((slug, rows))

    def replace_campaigns(self, slug, by_login):
        self.camps.append((slug, by_login))


def test_refresh_writes_when_stale_and_skips_when_fresh():
    st = InvStore(last=NOW - timedelta(hours=2))
    assert cabinets.refresh("lime", [A], st, NOW, lambda x: None, _caller({"login-a": 2})) == 1
    assert st.saved == [("lime", [{"login": "login-a", "currency": "RUB", "campaigns": 2,
                                   "access": "ok"}])]
    assert [(s, {k: [c["id"] for c in v] for k, v in b.items()}) for s, b in st.camps] == [
        ("lime", {"login-a": [1, 2]})]

    def no_call(*a):
        raise AssertionError("свежий инвентарь не должен ходить в Директ")
    st = InvStore(last=NOW - timedelta(minutes=10))
    assert cabinets.refresh("lime", [A], st, NOW, lambda x: None, no_call) is None
    assert st.saved == [] and st.camps == []


def test_refresh_denied_only_does_not_touch_campaigns():
    st = InvStore()
    cabinets.refresh("lime", [A], st, NOW, lambda x: None,
                     _caller({"login-a": DirectError("campaigns [53] Авторизация: x")}))
    assert st.saved and st.camps == []


def test_refresh_missing_table_is_logged_not_raised():
    class UndefinedTable(Exception):
        pass

    log = []

    def no_call(*a):
        raise AssertionError("без таблицы в Директ не ходим")
    st = InvStore(fail=UndefinedTable('relation "module_cabinets" does not exist'))
    assert cabinets.refresh("lime", [A], st, NOW, log.append, no_call) is None
    assert log == ["кабинеты: инвентарь не записан (нет таблицы module_cabinets или "
                   "module_campaigns — миграция Panda не накатана)"]


def test_refresh_without_accounts_does_nothing():
    st = InvStore(fail=AssertionError("не должен читать базу"))
    assert cabinets.refresh("lime", [], st, NOW, lambda x: None) is None


def test_inventory_skips_duplicate_login():
    # дубль логина в конфиге проекта (BJORN_DIRECT_CLIENTS_JSON) ронял upsert: «cannot affect
    # row a second time» — кабинет читается и пишется один раз
    rows, camps = cabinets.inventory([A, A, B], [].append, _caller({"login-a": 3, "Login-B": 1}))
    assert [r["login"] for r in rows] == ["login-a", "Login-B"]
    assert sorted(camps) == ["Login-B", "login-a"]


# --- modules_run --inventory: юнит <slug>_cabinets по кнопке «Обновить» -----------------

def _inv(st, accts=(A, B), why=(), call=None, log=None, tracked=None):
    def track(items, stage, label=None, total=None):
        if tracked is not None:
            tracked.append((stage, label, [a.login for a in items]))
        return items
    return runner.run_inventory(
        "lime", store=st, accounts=lambda s: (list(accts), list(why)),
        log=(log if log is not None else (lambda x: None)),
        call_fn=call or _caller({"login-a": 2, "Login-B": 1}), track=track)


def test_inventory_run_writes_now_even_if_fresh_and_exits_zero():
    # свежий checked_at (10 минут) такт пропустил бы; ручной прогон пишет сразу
    st = InvStore(last=NOW - timedelta(minutes=10))
    tracked = []
    assert _inv(st, tracked=tracked) == 0
    assert [r["login"] for _, rows in st.saved for r in rows] == ["login-a", "Login-B"]
    assert [sorted(b) for _, b in st.camps] == [["Login-B", "login-a"]]
    assert tracked == [("modules.inventory", "Кабинеты · кампании", ["login-a", "Login-B"])]


def test_inventory_run_partial_read_is_success():
    st = InvStore()
    log = []
    assert _inv(st, log=log.append, call=_caller(
        {"login-a": 2, "Login-B": DirectError("campaigns [1000] Сервис недоступен")})) == 0
    assert [r["login"] for _, rows in st.saved for r in rows] == ["login-a"]
    assert log[-1] == "кабинеты: инвентарь обновлён, кабинетов 1 из 2"


def test_inventory_run_nothing_read_is_failure():
    st = InvStore()
    err = DirectError("campaigns [1000] Сервис недоступен")
    assert _inv(st, call=_caller({"login-a": err, "Login-B": err})) == 1
    assert st.saved == [] and st.camps == []


def test_inventory_run_write_failure_is_exit_1():
    class Boom(InvStore):
        def upsert_cabinets(self, slug, rows):
            raise RuntimeError("connection reset")
    log = []
    assert _inv(Boom(), log=log.append) == 1
    assert log == ["кабинеты: инвентарь не записан (RuntimeError: connection reset)"]


def test_inventory_run_without_cabinets_is_failure_with_reason():
    log = []

    def no_call(*a):
        raise AssertionError("без кабинетов в Директ не ходим")
    assert _inv(InvStore(), accts=(), why=["нет токена в DIRECT_TOKEN"], call=no_call,
                log=log.append) == 1
    assert log == ["кабинеты: нет токена в DIRECT_TOKEN",
                   "кабинеты: у проекта lime нет кабинетов Директа"]


def test_cli_inventory_runs_only_inventory(monkeypatch):
    from sync import modules_run
    calls = []
    monkeypatch.setattr(modules_run, "run_inventory",
                        lambda slug, log: calls.append(("inv", slug)) or 0)
    monkeypatch.setattr(modules_run, "run_project",
                        lambda *a, **k: calls.append(("project",)) or 0)
    monkeypatch.setattr(modules_run.cancel, "install", lambda: calls.append(("cancel",)))
    assert modules_run.main(["--project", "synergy", "--inventory"]) == 0
    assert calls == [("inv", "synergy")]
    for bad in (["--module", "audit"], ["--dry-run"], ["--preview-id", "3"]):
        try:
            modules_run.main(["--project", "lime", "--inventory"] + bad)
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError(bad)
