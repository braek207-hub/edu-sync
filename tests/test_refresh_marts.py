"""Пересчёт витрин после синка.

Проверяется не «функция вызвалась», а два свойства, из-за которых её вообще написали:
без DATABASE_URL шаг не роняет синк, и отказ одной витрины не прекращает остальные —
иначе непримененная миграция красила бы весь ночной прогон.
"""
import sync.refresh_marts as rm


def test_no_database_url_is_skip_not_failure(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert rm.refresh_marts(["mart_lime_cabinet_daily"]) == 0


def test_failure_of_one_mart_does_not_stop_the_rest(monkeypatch):
    calls = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            calls.append(params[0])
            if params[0] == "mart_broken":
                raise rm.psycopg2.Error("витрины нет")

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setattr(rm.psycopg2, "connect", lambda *a, **k: FakeConn())
    assert rm.refresh_marts(["mart_broken", "mart_ok"]) == 1
    assert calls == ["mart_broken", "mart_ok"]
