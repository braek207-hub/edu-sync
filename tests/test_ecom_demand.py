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
