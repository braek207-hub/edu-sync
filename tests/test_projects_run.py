# -*- coding: utf-8 -*-
"""Оркестратор синка проектов: ночной прогон и очередь заданий на двойниках store/accounts/fetch.

Двойник базы — «мир»: строки project_sources в памяти, журнал событий и счётчик
незакоммиченных записей. Проверки границ транзакции живут в самих двойниках:
access_token и mark_reconnect коммитят, поэтому падают, если на conn висит запись окна.
"""

import copy
import time
from datetime import date, datetime, timezone

import pytest

from sync.projects import accounts, direct_fetch, metrika_fetch, run, store
from sync.projects.store import Source

TOKEN = "y0_SECRETtoken123"
METRIKA_SETTINGS = {"goals": [], "ecommerce": False, "result": "purchase"}


def src(sid, kind="direct", ext=None, acc=1, settings=None, status="ok", backfill=None, slug="acme"):
    if ext is None:
        ext = f"login{sid}" if kind == "direct" else str(1000 + sid)
    if settings is None:
        settings = {"currency": "RUB"} if kind == "direct" else dict(METRIKA_SETTINGS)
    return Source(sid, slug, kind, ext, acc, settings, status, backfill)


class Conn:
    def __init__(self, world):
        self.w = world
        self.pending = 0  # любые записи после последнего commit/rollback
        self.data_pending = 0  # записи окон фактовых таблиц

    def commit(self):
        self.w.log.append(("commit",))
        self.pending = self.data_pending = 0

    def rollback(self):
        self.w.log.append(("rollback",))
        self.pending = self.data_pending = 0


class World:
    def __init__(self, sources):
        self.sources = {s.id: s for s in sources}
        self.log: list[tuple] = []
        self.locked_elsewhere: set[int] = set()
        self.held: set[int] = set()
        self.reconnect_accounts: set[int] = set()
        self.token_errors: dict[int, Exception] = {}
        self.fetch_errors: dict[tuple, Exception] = {}  # (fn, external_id, date_from) или (fn, external_id)
        self.write_errors: dict[str, Exception] = {}
        self.on_backfill = None  # хук (world, sid, backfill)
        self.conn = Conn(self)

    # --- store ---
    def _load(self, pred, slug):
        return [
            copy.deepcopy(s)
            for _, s in sorted(self.sources.items())
            if pred(s) and (slug is None or s.slug == slug)
        ]

    def load_daily_sources(self, conn, slug=None):
        return self._load(lambda s: s.status in ("ok", "error"), slug)

    def load_queued_sources(self, conn, slug=None):
        self.queue_reads = getattr(self, "queue_reads", 0) + 1
        assert self.queue_reads < 50, "очередь перечитывается без конца"
        return self._load(lambda s: (s.backfill or {}).get("state") in ("queued", "running"), slug)

    def try_lock_source(self, conn, sid):
        self.log.append(("try_lock", sid))
        if sid in self.locked_elsewhere or sid in self.held:
            return False
        self.held.add(sid)
        return True

    def unlock_source(self, conn, sid):
        assert sid in self.held, f"unlock без lock: {sid}"
        self.held.discard(sid)
        self.log.append(("unlock", sid))

    def replace_window(self, conn, table, source, date_from, date_to, rows):
        if table in self.write_errors:
            raise self.write_errors[table]
        conn.pending += 1
        conn.data_pending += 1
        self.log.append(("write", table, source.id, date_from, date_to, len(rows)))
        return len(rows)

    def replace_purchases(self, conn, source, date_from, date_to, rows):
        return self.replace_window(conn, "project_metrika_purchases", source, date_from, date_to, rows)

    def mark_ok(self, conn, sid):
        conn.pending += 1
        self.sources[sid].status = "ok"
        self.log.append(("ok", sid))

    def mark_error(self, conn, sid, message):
        assert conn.data_pending == 0, "mark_error в одной транзакции с недописанным окном"
        conn.pending += 1
        self.sources[sid].status = "error"
        self.log.append(("error", sid, message))

    def save_backfill(self, conn, sid, backfill, status=None):
        conn.pending += 1
        self.sources[sid].backfill = copy.deepcopy(backfill)
        if status is not None:
            self.sources[sid].status = status
        self.log.append(("backfill", sid, copy.deepcopy(backfill), status))
        if self.on_backfill:
            self.on_backfill(self, sid, backfill)

    # --- accounts ---
    def access_token(self, conn, account_id, *, key_b64, now=None):
        assert conn.pending == 0, "access_token коммитит: звать на границе транзакции"
        assert key_b64 == "KEY"
        self.log.append(("token", account_id))
        if account_id in self.reconnect_accounts:
            raise accounts.NeedsReconnect("Доступ к Яндексу отозван — переподключите аккаунт")
        if account_id in self.token_errors:
            raise self.token_errors[account_id]
        return TOKEN

    def mark_reconnect(self, conn, account_id, reason):
        assert conn.data_pending == 0, "mark_reconnect коммитит: сначала rollback"
        self.reconnect_accounts.add(account_id)
        for s in self.sources.values():
            if s.account_id == account_id:
                s.status = "reconnect"
        self.log.append(("reconnect", account_id, reason))
        conn.commit()

    # --- fetch ---
    def _fetch(self, fn, ext, token, date_from, date_to, extra=None):
        assert token == TOKEN
        self.log.append(("fetch", fn, ext, date_from, date_to, extra))
        for key in ((fn, ext, date_from), (fn, ext)):
            if key in self.fetch_errors:
                raise self.fetch_errors[key]
        return [object()]

    def fetch_direct_daily(self, login, token, date_from, date_to, *, max_wait_sec):
        return self._fetch("direct", login, token, date_from, date_to, max_wait_sec)

    def fetch_traffic(self, counter, token, date_from, date_to):
        assert isinstance(counter, int)
        return self._fetch("traffic", str(counter), token, date_from, date_to)

    def fetch_goals(self, counter, token, date_from, date_to, goal_ids):
        assert isinstance(counter, int)
        return self._fetch("goals", str(counter), token, date_from, date_to, list(goal_ids))

    def fetch_purchases(self, counter, token, date_from, date_to):
        return self._fetch("purchases", str(counter), token, date_from, date_to)

    def fetch_products(self, counter, token, date_from, date_to):
        return self._fetch("products", str(counter), token, date_from, date_to)

    # --- выборки по журналу ---
    def events(self, name):
        return [e for e in self.log if e[0] == name]

    def fetches(self, fn=None):
        return [e for e in self.events("fetch") if fn is None or e[1] == fn]


@pytest.fixture
def world(monkeypatch):
    holder = {}

    def make(*sources):
        w = World(sources)
        for name in (
            "load_daily_sources",
            "load_queued_sources",
            "try_lock_source",
            "unlock_source",
            "replace_window",
            "replace_purchases",
            "mark_ok",
            "mark_error",
            "save_backfill",
        ):
            monkeypatch.setattr(store, name, getattr(w, name))
        monkeypatch.setattr(accounts, "access_token", w.access_token)
        monkeypatch.setattr(accounts, "mark_reconnect", w.mark_reconnect)
        monkeypatch.setattr(direct_fetch, "fetch_direct_daily", w.fetch_direct_daily)
        for name in ("fetch_traffic", "fetch_goals", "fetch_purchases", "fetch_products"):
            monkeypatch.setattr(metrika_fetch, name, getattr(w, name))
        holder["w"] = w
        return w

    yield make
    if "w" in holder:
        assert holder["w"].held == set(), "advisory lock не снят"


TODAY = date(2026, 10, 19)  # вчера по МСК = 2026-10-18


def daily(w, slug=None):
    return run.run_daily(w.conn, today_msk=TODAY, key_b64="KEY", slug=slug)


def queue(w, slug=None, today=TODAY):
    return run.run_queue(w.conn, today_msk=today, key_b64="KEY", slug=slug)


# ---------------- даты и ошибки ----------------


def test_constants():
    assert (run.DAILY_DAYS, run.BACKFILL_DAYS, run.SOURCE_BUDGET_SEC) == (7, 90, 1200)


def test_yesterday_msk_crosses_utc_midnight():
    assert run.yesterday_msk(datetime(2026, 10, 18, 22, 30, tzinfo=timezone.utc)) == date(2026, 10, 18)
    assert run.yesterday_msk(datetime(2026, 10, 18, 20, 30, tzinfo=timezone.utc)) == date(2026, 10, 17)


def test_month_chunks():
    chunks = run.month_chunks(date(2026, 7, 21), date(2026, 10, 18))
    assert chunks == [
        (date(2026, 7, 21), date(2026, 7, 31)),
        (date(2026, 8, 1), date(2026, 8, 31)),
        (date(2026, 9, 1), date(2026, 9, 30)),
        (date(2026, 10, 1), date(2026, 10, 18)),
    ]
    assert run.month_chunks(date(2026, 10, 12), date(2026, 10, 18)) == [(date(2026, 10, 12), date(2026, 10, 18))]
    assert run.month_chunks(date(2026, 12, 20), date(2027, 1, 5)) == [
        (date(2026, 12, 20), date(2026, 12, 31)),
        (date(2027, 1, 1), date(2027, 1, 5)),
    ]


def test_short_error_strips_tokens():
    exc = RuntimeError(
        f"Direct API error: {TOKEN} v1.AbC-12_x.YZ refresh=y0_other "
        "client_secret=s3cr3tValue&x=1 {\"client_secret\": \"jsonSecret\"} " + "z" * 1000
    )
    msg = run.short_error(exc)
    assert len(msg) <= 500
    for secret in ("SECRETtoken123", "y0_other", "AbC-12_x", "s3cr3tValue", "jsonSecret"):
        assert secret not in msg
    assert msg.startswith("Direct API error")


def test_short_error_empty_message_names_type():
    assert run.short_error(TimeoutError()) == "TimeoutError"


# ---------------- ночной прогон ----------------


def test_daily_window_is_last_7_days(world):
    w = world(src(1))
    assert daily(w) == 0
    assert [e[3:5] for e in w.fetches("direct")] == [("2026-10-12", "2026-10-18")]
    assert w.events("write") == [("write", "project_direct_daily", 1, "2026-10-12", "2026-10-18", 1)]
    assert w.sources[1].status == "ok"


def test_daily_one_failure_others_continue(world):
    w = world(src(1), src(2), src(3))
    w.fetch_errors[("direct", "login2")] = RuntimeError(f"boom with {TOKEN}")
    assert daily(w) == 1
    assert [e[1] for e in w.events("ok")] == [1, 3]
    errors = w.events("error")
    assert [e[1] for e in errors] == [2]
    assert "boom" in errors[0][2] and "SECRET" not in errors[0][2]
    # сбой второго — откат до записи статуса
    i = w.log.index(errors[0])
    assert w.log[i - 1] == ("rollback",)
    assert [e[1] for e in w.events("unlock")] == [1, 2, 3]


def test_daily_skips_locked_source(world, capsys):
    w = world(src(1), src(2), src(3))
    w.locked_elsewhere = {2}
    assert daily(w) == 0
    assert [e[2] for e in w.fetches()] == ["login1", "login3"]
    assert 2 not in [e[1] for e in w.events("unlock")]
    assert "источник 2 занят" in capsys.readouterr().out


def test_daily_only_ok_and_error_sources_and_slug(world):
    w = world(
        src(1),
        src(2, status="error"),
        src(3, status="new"),
        src(4, status="backfilling"),
        src(5, status="reconnect"),
        src(6, slug="other"),
    )
    assert daily(w, slug="acme") == 0
    assert [e[2] for e in w.fetches()] == ["login1", "login2"]


def test_reconnect_isolates_account(world):
    w = world(src(1, acc=10), src(2, acc=20), src(3, acc=10))
    w.reconnect_accounts = {10}
    assert daily(w) == 1
    assert [e[1] for e in w.events("ok")] == [2]
    assert {e[1] for e in w.events("reconnect")} == {10}
    assert [e[2] for e in w.fetches()] == ["login2"]
    assert (w.sources[1].status, w.sources[2].status, w.sources[3].status) == ("reconnect", "ok", "reconnect")
    assert not w.events("error")


def test_access_token_error_is_source_error_not_reconnect(world):
    w = world(src(1, acc=10), src(2, acc=20))
    w.token_errors[10] = accounts.AccessTokenError("Яндекс не ответил на обновление токена")
    assert daily(w) == 1
    assert not w.events("reconnect")
    assert w.events("error") == [("error", 1, "Яндекс не ответил на обновление токена")]
    assert [e[1] for e in w.events("ok")] == [2]


def test_metrika_401_marks_reconnect(world):
    w = world(src(1, kind="metrika", acc=10), src(2, kind="metrika", acc=20), src(3, kind="direct", acc=30))
    w.fetch_errors[("traffic", "1001")] = RuntimeError('Metrica API error 401: {"message":"Invalid oauth_token"}')
    w.fetch_errors[("traffic", "1002")] = RuntimeError('Metrica API error 403: {"message":"Access denied"}')
    assert daily(w) == 1
    assert [e[1] for e in w.events("reconnect")] == [10]
    assert w.events("reconnect")[0][2].startswith("Metrica API error 401")
    assert [e[1] for e in w.events("error")] == [2]
    assert [e[1] for e in w.events("ok")] == [3]


def test_midwrite_failure_rolls_back_before_mark(world):
    s = src(1, kind="metrika", settings={"goals": [], "ecommerce": True, "result": "purchase"})
    w = world(s)
    w.write_errors["project_metrika_purchases"] = RuntimeError("deadlock detected")
    assert daily(w) == 1
    names = [e[0] for e in w.log]
    assert names.index("rollback") < names.index("error")
    assert not w.events("ok")


def test_deadline_passed_to_direct(world):
    w = world(src(1))
    daily(w)
    max_wait = w.fetches("direct")[0][5]
    assert 0 < max_wait <= run.SOURCE_BUDGET_SEC


def test_deadline_already_passed_raises_before_fetch(world):
    w = world(src(1))
    with pytest.raises(TimeoutError):
        run.sync_source(w.conn, w.sources[1], "2026-10-12", "2026-10-18", key_b64="KEY", deadline=time.monotonic() - 1)
    assert not w.fetches()


def test_metrika_without_ecommerce_skips_purchases_products(world):
    w = world(src(1, kind="metrika"))
    n = run.sync_source(w.conn, w.sources[1], "2026-10-12", "2026-10-18", key_b64="KEY", deadline=time.monotonic() + 60)
    assert [e[1] for e in w.fetches()] == ["traffic"]
    assert n == 1
    # окна целей и e-com стираются: настройки выключили — старых строк в окне быть не должно
    assert {e[1]: e[5] for e in w.events("write")} == {
        "project_metrika_traffic": 1,
        "project_metrika_goals": 0,
        "project_metrika_purchases": 0,
        "project_metrika_products": 0,
    }


def test_metrika_with_goals_and_ecommerce(world):
    settings = {
        "goals": [{"id": 5, "name": "Лид", "role": "lead"}, {"id": 7, "name": "Звонок", "role": "step"}],
        "ecommerce": True,
        "result": "goal",
    }
    w = world(src(1, kind="metrika", ext="23504302", settings=settings))
    n = run.sync_source(w.conn, w.sources[1], "2026-10-12", "2026-10-18", key_b64="KEY", deadline=time.monotonic() + 60)
    assert [e[1] for e in w.fetches()] == ["traffic", "goals", "purchases", "products"]
    assert w.fetches("goals")[0][5] == [5, 7]
    assert {e[2] for e in w.fetches()} == {"23504302"}
    assert n == 4
    # все запросы до первой записи: окно не держит транзакцию на время сети
    names = [e[0] for e in w.log]
    assert max(i for i, x in enumerate(names) if x == "fetch") < names.index("write")


# ---------------- очередь заданий ----------------


def history_job():
    return {"state": "queued", "kind": "history", "days": 90, "requested_at": "2026-10-18T10:00:00Z", "requested_by": "u1"}


def refresh_job():
    return {"state": "queued", "kind": "refresh", "days": 7, "requested_at": "2026-10-18T10:00:00Z", "requested_by": "u1"}


def test_backfill_progress_and_done(world):
    w = world(src(1, status="new", backfill=history_job()))
    assert queue(w) == 0
    saves = w.events("backfill")
    first = saves[0]
    assert first[3] == "backfilling"
    assert first[2]["state"] == "running" and first[2]["kind"] == "history"
    assert (first[2]["from"], first[2]["to"]) == ("2026-07-21", "2026-10-18")
    assert (first[2]["chunks_total"], first[2]["chunks_done"]) == (4, 0)
    assert first[2]["requested_by"] == "u1" and "started_at" in first[2]
    progress = [s for s in saves[1:] if s[2]["state"] == "running"]
    assert [p[2]["chunks_done"] for p in progress] == [1, 2, 3, 4]
    assert all(p[3] is None for p in progress)
    # прогресс коммитится сразу после каждого куска
    for p in progress:
        assert w.log[w.log.index(p) + 1] == ("commit",)
    assert [e[3:5] for e in w.fetches("direct")] == [
        ("2026-07-21", "2026-07-31"),
        ("2026-08-01", "2026-08-31"),
        ("2026-09-01", "2026-09-30"),
        ("2026-10-01", "2026-10-18"),
    ]
    assert saves[-1][2]["state"] == "done" and "finished_at" in saves[-1][2]
    assert w.events("ok") == [("ok", 1)]
    assert w.sources[1].status == "ok"


def test_backfill_resume_from_running(world):
    job = {
        "state": "running",
        "kind": "history",
        "days": 90,
        "from": "2026-07-21",
        "to": "2026-10-18",
        "chunks_total": 4,
        "chunks_done": 2,
        "started_at": "2026-10-18T10:01:00Z",
    }
    w = world(src(1, status="backfilling", backfill=job))
    assert queue(w, today=date(2026, 10, 21)) == 0  # границы — из сохранённых from/to
    assert [e[3:5] for e in w.fetches("direct")] == [("2026-09-01", "2026-09-30"), ("2026-10-01", "2026-10-18")]
    assert [s[2]["chunks_done"] for s in w.events("backfill") if s[2]["state"] == "running"] == [3, 4]
    assert w.sources[1].backfill["state"] == "done"


def test_backfill_picks_request_arrived_mid_run(world):
    w = world(src(1, backfill=refresh_job()))

    def arrive(world_, sid, backfill):
        if sid == 1 and backfill["state"] == "done" and 2 not in world_.sources:
            world_.sources[2] = src(2, slug="other", backfill=refresh_job())

    w.on_backfill = arrive
    assert queue(w) == 0
    assert [e[2] for e in w.fetches()] == ["login1", "login2"]
    assert w.sources[2].backfill["state"] == "done"


def test_refresh_job_one_source_7_days(world):
    w = world(src(1, backfill=refresh_job()), src(2), src(3, slug="other"))
    assert queue(w) == 0
    assert [e[2:5] for e in w.fetches()] == [("login1", "2026-10-12", "2026-10-18")]
    saves = w.events("backfill")
    assert all(s[1] == 1 for s in saves)
    assert all(s[3] is None for s in saves)  # статус refresh не трогает до итога
    assert saves[0][2]["chunks_total"] == 1
    assert saves[-1][2]["state"] == "done"
    assert w.events("ok") == [("ok", 1)]
    assert w.sources[2].backfill is None and w.sources[3].backfill is None


def test_queue_locked_source_left_queued(world):
    w = world(src(1, backfill=refresh_job()), src(2, backfill=refresh_job()))
    w.locked_elsewhere = {1}
    assert queue(w) == 0
    assert w.sources[1].backfill["state"] == "queued"
    assert w.events("try_lock").count(("try_lock", 1)) == 1
    assert [e[2] for e in w.fetches()] == ["login2"]
    assert not [s for s in w.events("backfill") if s[1] == 1]


def test_backfill_failed_not_retried_in_same_run(world):
    w = world(src(1, status="new", backfill=history_job()), src(2, backfill=refresh_job()))
    w.fetch_errors[("direct", "login1", "2026-08-01")] = RuntimeError("Direct API error for login1: 500")

    def requeue(world_, sid, backfill):
        # пользователь сразу нажал ещё раз — задание снова в очереди, но в этом прогоне не берётся
        if sid == 1 and backfill["state"] == "failed":
            world_.sources[1].backfill = history_job()

    w.on_backfill = requeue
    assert queue(w) == 1
    assert len(w.fetches("direct")) == 3  # 2 куска первого + 1 второго
    assert w.events("try_lock").count(("try_lock", 1)) == 1
    failed = [s for s in w.events("backfill") if s[2]["state"] == "failed"]
    assert len(failed) == 1 and failed[0][2]["error"].startswith("Direct API error")
    assert failed[0][2]["chunks_done"] == 1
    assert [e[1] for e in w.events("error")] == [1]
    assert w.sources[1].status == "error"
    assert w.sources[2].backfill["state"] == "done"


def test_queue_reconnect_fails_job_and_marks_account(world):
    w = world(src(1, acc=10, status="new", backfill=history_job()), src(2, acc=20, backfill=refresh_job()))
    w.reconnect_accounts = {10}
    assert queue(w) == 1
    assert [e[1] for e in w.events("reconnect")] == [10]
    assert w.sources[1].backfill["state"] == "failed"
    assert w.sources[1].status == "reconnect"
    assert w.sources[2].backfill["state"] == "done"


def test_queue_midwrite_failure_rolls_back_before_failed_state(world):
    s = src(1, kind="metrika", settings={"goals": [], "ecommerce": True, "result": "purchase"}, backfill=refresh_job())
    w = world(s)
    w.write_errors["project_metrika_purchases"] = RuntimeError("deadlock detected")
    assert queue(w) == 1
    names = [e[0] for e in w.log]
    failed_save = next(e for e in w.log if e[0] == "backfill" and e[2]["state"] == "failed")
    assert names.index("rollback") < w.log.index(failed_save) < names.index("error")
    assert failed_save[2]["error"] == "deadlock detected"
    assert w.log[names.index("error") + 1] == ("commit",)


def test_queue_bad_job_kind_fails(world):
    w = world(src(1, backfill={"state": "queued", "kind": "everything", "days": 3}))
    assert queue(w) == 1
    assert w.sources[1].backfill["state"] == "failed"
    assert w.sources[1].status == "error"


def test_queue_slug_filter(world):
    w = world(src(1, backfill=refresh_job()), src(2, slug="other", backfill=refresh_job()))
    assert queue(w, slug="other") == 0
    assert [e[2] for e in w.fetches()] == ["login2"]
    assert w.sources[1].backfill["state"] == "queued"


# ---------------- CLI ----------------


def test_main_missing_key_exits_1(monkeypatch, capsys):
    monkeypatch.delenv("PANDA_SECRET_KEY", raising=False)

    def no_db():
        raise AssertionError("без ключа в базу не ходим")

    monkeypatch.setattr(run.db, "get_connection", no_db)
    assert run.main(["daily"]) == 1
    assert "PANDA_SECRET_KEY" in capsys.readouterr().out


def test_main_bad_key_exits_1_without_value(monkeypatch, capsys):
    monkeypatch.setenv("PANDA_SECRET_KEY", "not-a-key-value-xyz")
    monkeypatch.setattr(run.db, "get_connection", lambda: (_ for _ in ()).throw(AssertionError("db")))
    assert run.main(["queue"]) == 1
    out = capsys.readouterr().out
    assert "PANDA_SECRET_KEY" in out and "not-a-key-value-xyz" not in out


def test_main_runs_mode_with_slug(monkeypatch):
    import base64
    from contextlib import contextmanager

    monkeypatch.setenv("PANDA_SECRET_KEY", base64.b64encode(b"k" * 32).decode())
    sentinel = object()

    @contextmanager
    def fake_conn():
        yield sentinel

    calls = []
    monkeypatch.setattr(run.db, "get_connection", fake_conn)
    monkeypatch.setattr(run, "run_queue", lambda conn, **kw: calls.append((conn, kw)) or 1)
    assert run.main(["queue", "--slug", "acme"]) == 1
    assert calls[0][0] is sentinel
    assert calls[0][1]["slug"] == "acme"
    assert isinstance(calls[0][1]["today_msk"], date)
