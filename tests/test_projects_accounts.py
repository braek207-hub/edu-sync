# -*- coding: utf-8 -*-
"""Токен подключения Яндекса: зеркало lib/yandex/accounts.ts getAccessToken."""
from datetime import datetime, timedelta, timezone

import pytest
import requests

from sync.projects import accounts as A
from sync.projects.secret_box import decrypt, encrypt

KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
OLD_ACCESS = "old-access-token"
OLD_REFRESH = "old-refresh-token"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.conn.rows.pop(0) if self.conn.rows else self.conn.row


class FakeConn:
    def __init__(self, row):
        self.row = row
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.rows = []  # очередь ответов fetchone; пуста — всегда self.row

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def updates(self):
        return [e for e in self.executed if e[0].upper().startswith("UPDATE")]


def make_conn(expires_in_days, *, status="ok", with_refresh=True):
    exp = None if expires_in_days is None else NOW + timedelta(days=expires_in_days)
    return FakeConn(
        (
            encrypt(OLD_ACCESS, KEY),
            encrypt(OLD_REFRESH, KEY) if with_refresh else None,
            exp,
            status,
        )
    )


class Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("YANDEX_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("YANDEX_OAUTH_CLIENT_SECRET", "SECRET-VALUE")


@pytest.fixture
def post(monkeypatch):
    state = {"calls": [], "resp": None, "exc": None}

    def fake(url, data=None, timeout=None, **kw):
        state["calls"].append({"url": url, "data": data, "timeout": timeout})
        if state["exc"]:
            raise state["exc"]
        return state["resp"]

    monkeypatch.setattr(requests, "post", fake)
    return state


def test_fresh_token_no_refresh(post):
    conn = make_conn(30)
    assert A.access_token(conn, 1, key_b64=KEY, now=NOW) == OLD_ACCESS
    assert post["calls"] == []


def test_null_expiry_no_refresh(post):
    conn = make_conn(None)
    assert A.access_token(conn, 1, key_b64=KEY, now=NOW) == OLD_ACCESS
    assert post["calls"] == []


def test_no_refresh_token_no_refresh(post):
    conn = make_conn(1, with_refresh=False)
    assert A.access_token(conn, 1, key_b64=KEY, now=NOW) == OLD_ACCESS
    assert post["calls"] == []


def test_refresh_near_expiry_writes_back(post):
    post["resp"] = Resp(200, {"access_token": "new", "refresh_token": "r2", "expires_in": 31536000})
    conn = make_conn(3)

    assert A.access_token(conn, 1, key_b64=KEY, now=NOW) == "new"

    call = post["calls"][0]
    assert call["url"] == "https://oauth.yandex.ru/token"
    assert call["timeout"] == 15
    assert call["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": OLD_REFRESH,
        "client_id": "cid",
        "client_secret": "SECRET-VALUE",
    }
    assert any("FOR UPDATE" in sql for sql, _ in conn.executed)
    (sql, params), = conn.updates()
    assert "access_token_enc" in sql and "refresh_token_enc" in sql
    assert "status = 'ok'" in sql
    enc = [p for p in params if isinstance(p, str) and p.startswith("v1.")]
    assert sorted(decrypt(p, KEY) for p in enc) == ["new", "r2"]
    exp = [p for p in params if isinstance(p, datetime)][0]
    assert abs(exp - (NOW + timedelta(days=365))) < timedelta(seconds=1)
    assert conn.commits >= 1


def test_refresh_without_new_refresh_keeps_old(post):
    post["resp"] = Resp(200, {"access_token": "new", "expires_in": 1000})
    conn = make_conn(3)

    assert A.access_token(conn, 1, key_b64=KEY, now=NOW) == "new"

    (sql, params), = conn.updates()
    assert "refresh_token_enc" not in sql
    assert [decrypt(p, KEY) for p in params if isinstance(p, str) and p.startswith("v1.")] == ["new"]


@pytest.mark.parametrize("code", ["invalid_grant", "bad_verification_code"])
def test_invalid_grant_marks_reconnect(post, code):
    post["resp"] = Resp(400, {"error": code, "error_description": "x"})
    conn = make_conn(3)

    with pytest.raises(A.NeedsReconnect):
        A.access_token(conn, 7, key_b64=KEY, now=NOW)

    ups = conn.updates()
    assert len(ups) == 2
    assert "yandex_accounts" in ups[0][0] and "project_sources" in ups[1][0]
    assert conn.commits >= 1


def test_upstream_error_falls_back_while_valid(post):
    post["resp"] = Resp(500, {})
    conn = make_conn(3)

    assert A.access_token(conn, 1, key_b64=KEY, now=NOW) == OLD_ACCESS
    assert conn.updates() == []
    assert conn.commits >= 1


def test_network_error_falls_back_while_valid(post):
    post["exc"] = requests.ConnectionError("boom")
    conn = make_conn(3)

    assert A.access_token(conn, 1, key_b64=KEY, now=NOW) == OLD_ACCESS
    assert conn.updates() == []


def test_upstream_error_when_expired_raises(post):
    post["resp"] = Resp(500, {})
    conn = make_conn(-1)

    with pytest.raises(A.AccessTokenError):
        A.access_token(conn, 1, key_b64=KEY, now=NOW)


def test_reconnect_status_raises(post):
    conn = make_conn(30, status="reconnect")
    with pytest.raises(A.NeedsReconnect):
        A.access_token(conn, 1, key_b64=KEY, now=NOW)
    assert post["calls"] == []


def test_secret_not_in_errors(post):
    post["exc"] = requests.ConnectionError(f"failed client_secret=SECRET-VALUE {OLD_REFRESH}")
    conn = make_conn(-1)

    with pytest.raises(A.AccessTokenError) as ei:
        A.access_token(conn, 1, key_b64=KEY, now=NOW)

    text = str(ei.value) + repr(ei.value)
    for bad in ("client_secret", "SECRET-VALUE", OLD_REFRESH, OLD_ACCESS):
        assert bad not in text
    assert ei.value.__cause__ is None
    assert ei.value.__context__ is None


def test_mark_reconnect_updates_both_tables():
    conn = make_conn(30)
    A.mark_reconnect(conn, 5, "доступ отозван")
    ups = conn.updates()
    assert "yandex_accounts" in ups[0][0] and "status = 'reconnect'" in ups[0][0]
    assert "project_sources" in ups[1][0] and "last_error" in ups[1][0]
    assert "доступ отозван" in ups[1][1]


@pytest.mark.parametrize(
    "body",
    [{"access_token": ""}, {"expires_in": 5}, {"access_token": "x", "error": "oops"}],
)
def test_malformed_200_is_failure(post, body):
    post["resp"] = Resp(200, body)
    conn = make_conn(-1)
    with pytest.raises(A.AccessTokenError):
        A.access_token(conn, 1, key_b64=KEY, now=NOW)
    assert conn.updates() == []


def test_non_2xx_with_token_is_failure(post):
    post["resp"] = Resp(302, {"access_token": "x"})
    conn = make_conn(-1)
    with pytest.raises(A.AccessTokenError):
        A.access_token(conn, 1, key_b64=KEY, now=NOW)


def test_decrypt_failure_under_lock_rolls_back(post):
    wrong = "BwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwcHBwc="
    conn = make_conn(3)
    with pytest.raises(ValueError):
        A.access_token(conn, 1, key_b64=wrong, now=NOW)
    assert conn.rollbacks == 1
    assert post["calls"] == []


def test_race_recheck_under_lock_skips_refresh(post):
    stale = make_conn(3).row
    fresh = (encrypt("fresh-by-peer", KEY), encrypt(OLD_REFRESH, KEY), NOW + timedelta(days=30), "ok")
    conn = FakeConn(fresh)
    conn.rows = [stale, fresh]

    assert A.access_token(conn, 1, key_b64=KEY, now=NOW) == "fresh-by-peer"
    assert post["calls"] == []
    assert conn.updates() == []
