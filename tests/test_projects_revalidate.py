# -*- coding: utf-8 -*-
"""Сброс кэша Panda-BI после синка проектов: sync/projects/revalidate.py и вызовы из run.py."""

import json
import urllib.error

import pytest

from sync.projects import revalidate as rv
from sync.projects import run
from tests import test_projects_run as prun
from tests.test_projects_run import daily, history_job, queue, src

# Фикстура из соседнего модуля: присваивание, а не import — иначе ruff F811 на каждом параметре world.
world = prun.world


class FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


@pytest.fixture
def urlopen(monkeypatch):
    calls = []

    def fake(req, timeout=None):
        calls.append((req, timeout))
        return FakeResp()

    monkeypatch.setattr(rv.urllib.request, "urlopen", fake)
    return calls


def test_empty_slugs_do_nothing(monkeypatch, urlopen):
    monkeypatch.setenv("PANDA_BI_URL", "https://panda.example")
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    rv.revalidate([])
    assert urlopen == []


@pytest.mark.parametrize("missing", ["PANDA_BI_URL", "CRON_SECRET"])
def test_no_env_no_request(monkeypatch, urlopen, capsys, missing):
    monkeypatch.setenv("PANDA_BI_URL", "https://panda.example")
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    monkeypatch.delenv(missing)
    rv.revalidate(["acme"])
    assert urlopen == []
    assert missing in capsys.readouterr().out


def test_posts_slugs_with_bearer(monkeypatch, urlopen):
    monkeypatch.setenv("PANDA_BI_URL", "https://panda.example/")
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    rv.revalidate(["acme"])
    assert len(urlopen) == 1
    req, timeout = urlopen[0]
    assert req.full_url == "https://panda.example/api/revalidate"
    assert req.get_method() == "POST"
    assert req.data == b'{"slugs": ["acme"]}'
    assert req.get_header("Authorization") == "Bearer s3cret"
    assert req.get_header("Content-type") == "application/json; charset=utf-8"
    assert timeout == 60


def test_one_post_per_slug_sorted_deduped_utf8(monkeypatch, urlopen):
    monkeypatch.setenv("PANDA_BI_URL", "https://panda.example")
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    rv.revalidate(iter(["б", "a", "б"]))
    bodies = [json.loads(r.data.decode("utf-8")) for r, _ in urlopen]
    assert bodies == [{"slugs": ["a"]}, {"slugs": ["б"]}]
    assert "б".encode("utf-8") in urlopen[1][0].data


def test_bad_slug_does_not_block_next(monkeypatch, capsys):
    monkeypatch.setenv("PANDA_BI_URL", "https://panda.example")
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    sent = []

    def fake(req, timeout=None):
        sent.append(json.loads(req.data.decode("utf-8"))["slugs"][0])
        if len(sent) == 1:
            raise urllib.error.HTTPError("u", 400, "bad", {}, None)
        return FakeResp()

    monkeypatch.setattr(rv.urllib.request, "urlopen", fake)
    rv.revalidate(["a", "b"])
    out = capsys.readouterr().out
    assert sent == ["a", "b"]
    assert "a" in out and "s3cret" not in out


def test_url_error_does_not_propagate_or_leak_secret(monkeypatch, capsys):
    monkeypatch.setenv("PANDA_BI_URL", "https://panda.example")
    monkeypatch.setenv("CRON_SECRET", "s3cret")

    def boom(req, timeout=None):
        raise urllib.error.URLError("down s3cret")

    monkeypatch.setattr(rv.urllib.request, "urlopen", boom)
    rv.revalidate(["acme"])
    assert "s3cret" not in capsys.readouterr().out


def test_http_error_does_not_propagate(monkeypatch):
    monkeypatch.setenv("PANDA_BI_URL", "https://panda.example")
    monkeypatch.setenv("CRON_SECRET", "s3cret")

    def boom(req, timeout=None):
        raise urllib.error.HTTPError("u", 400, "bad", {}, None)

    monkeypatch.setattr(rv.urllib.request, "urlopen", boom)
    rv.revalidate(["acme"])


# ---------------- вызовы из run.py ----------------


@pytest.fixture
def calls(monkeypatch):
    got = []
    monkeypatch.setattr(run, "revalidate", lambda slugs: got.append(sorted(slugs)))
    return got


def test_daily_revalidates_only_successful_slugs(world, calls):
    w = world(src(1, slug="good"), src(2, slug="bad"))
    w.fetch_errors[("direct", "login2")] = RuntimeError("boom")
    assert daily(w) == 1
    assert calls == [["good"]]


def test_daily_dedupes_slug_and_calls_once(world, calls):
    w = world(src(1, slug="a"), src(2, slug="a"))
    assert daily(w) == 0
    assert calls == [["a"]]


def test_daily_all_failed_calls_with_empty(world, calls):
    w = world(src(1))
    w.fetch_errors[("direct", "login1")] = RuntimeError("boom")
    assert daily(w) == 1
    assert calls == [[]]


def test_queue_revalidates_only_successful_jobs(world, calls):
    w = world(
        src(1, slug="good", status="new", backfill=history_job()),
        src(2, slug="bad", status="new", backfill=history_job()),
    )
    w.fetch_errors[("direct", "login2")] = RuntimeError("boom")
    assert queue(w) == 1
    assert calls == [["good"]]


def test_daily_unexpected_exception_still_revalidates_synced(world, calls, monkeypatch):
    w = world(src(1, slug="good"), src(2, slug="other"))
    real = run.store.load_queued_sources
    n = {"c": 0}

    def flaky(conn, slug=None):
        n["c"] += 1
        if n["c"] == 2:
            raise RuntimeError("db gone")
        return real(conn, slug)

    monkeypatch.setattr(run.store, "load_queued_sources", flaky)
    with pytest.raises(RuntimeError):
        daily(w)
    assert calls == [["good"]]
