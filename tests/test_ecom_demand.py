import datetime as dt

from sync.ecom_demand import ECOM_PHRASE_SETS


def test_phrase_sets_cover_three_projects():
    assert set(ECOM_PHRASE_SETS) == {"bjorn", "polinarepik", "meshnflesh"}
    # bjorn — оба набора; polina/mesh — только бренд.
    assert set(ECOM_PHRASE_SETS["bjorn"]) == {"niche", "brand"}
    assert set(ECOM_PHRASE_SETS["polinarepik"]) == {"brand"}
    assert set(ECOM_PHRASE_SETS["meshnflesh"]) == {"brand"}
    # bjorn niche = переиспользованный список bjorn_demand (без копипаста).
    from sync.bjorn_demand import BJORN_DEMAND_PHRASES
    assert ECOM_PHRASE_SETS["bjorn"]["niche"] == BJORN_DEMAND_PHRASES
    # Все наборы непустые.
    for slug, kinds in ECOM_PHRASE_SETS.items():
        for kind, phrases in kinds.items():
            assert phrases, f"{slug}/{kind} пуст"


def test_sync_writes_slug_kind(monkeypatch):
    """Обобщённый синк должен писать slug и kind в общую таблицу, а не в per-project."""
    import sync.ecom_demand as m
    captured = {}

    class FakeCur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def executemany(self, sql, rows):
            captured["sql"] = sql
            captured["rows"] = list(rows)

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return FakeCur()
        def commit(self): captured["committed"] = True

    monkeypatch.setattr(m, "fetch_phrase", lambda p, f, t: {})
    monkeypatch.setattr(m, "aggregate_weekly_by_phrase", lambda p, resp: {"2026-08-11": 100})
    import sync.db
    monkeypatch.setattr(sync.db, "get_connection", lambda: FakeConn())

    n = m.sync_ecom_wordstat_demand("polinarepik", "brand", ["полина репик"], "2026-08-01", "2026-08-17")
    assert n == 1
    assert "ecom_wordstat_demand" in captured["sql"]
    # Строка несёт slug и kind.
    row = captured["rows"][0]
    assert "polinarepik" in row and "brand" in row
    assert captured.get("committed")


def test_sync_daily_writes_slug_kind_and_day(monkeypatch):
    """Регресс порядка колонок дневного INSERT (slug, kind, region, phrase, day, frequency):
    rows собираются как (day, phrase, freq) — значения в кортеже для БД должны идти
    в порядке колонок, а не в порядке сборки rows."""
    import sync.ecom_demand as m
    captured = {}

    class FakeCur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def executemany(self, sql, rows):
            captured["sql"] = sql
            captured["rows"] = list(rows)

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return FakeCur()
        def commit(self): captured["committed"] = True

    monkeypatch.setattr(m, "fetch_phrase_daily", lambda p, f, t: {})
    monkeypatch.setattr(m, "aggregate_daily_by_phrase", lambda resp: {"2026-08-15": 42})
    import sync.db
    monkeypatch.setattr(sync.db, "get_connection", lambda: FakeConn())

    today = dt.date.today()
    frm = (today - dt.timedelta(days=5)).isoformat()
    to = today.isoformat()
    n = m.sync_ecom_wordstat_demand_daily("polinarepik", "brand", ["полина репик"], frm, to)
    assert n == 1
    assert "ecom_wordstat_demand_daily" in captured["sql"]
    row = captured["rows"][0]
    # Позиции обязаны совпадать с колонками INSERT: slug, kind, region, phrase, day, frequency.
    assert row[0] == "polinarepik"
    assert row[1] == "brand"
    assert row[2] == "ru"
    assert row[3] == "полина репик"  # phrase — 4-я позиция, не day
    assert row[4] == "2026-08-15"     # day — 5-я позиция, не phrase
    assert row[5] == 42
    assert captured.get("committed")


def test_driver_iterates_all_sets(monkeypatch):
    import importlib
    calls = []
    import sync.ecom_demand as m
    monkeypatch.setattr(m, "sync_ecom_wordstat_demand",
                        lambda slug, kind, phrases, frm, to, region="ru": calls.append((slug, kind)) or len(phrases))
    monkeypatch.setattr(m, "sync_ecom_wordstat_demand_daily",
                        lambda slug, kind, phrases, frm, to, region="ru": 0)
    monkeypatch.setattr(m, "ecom_demand_up_to_date", lambda slug, kind, **k: False)
    monkeypatch.setattr(m, "ecom_daily_demand_up_to_date", lambda slug, kind, **k: True)
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("YANDEX_SEARCHAPI_KEY", "x")
    monkeypatch.setenv("WORDSTAT_FROM", "2024-01-01")
    drv = importlib.import_module("sync_ecom_demand")
    drv.main()
    # Все 4 набора (bjorn niche+brand, polina brand, mesh brand) прошли недельный синк.
    assert set(calls) == {("bjorn", "niche"), ("bjorn", "brand"),
                          ("polinarepik", "brand"), ("meshnflesh", "brand")}
