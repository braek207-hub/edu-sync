import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.gsc import REGIONS, aggregate_weekly, parse_daily_totals


def test_parse_daily_totals_maps_date_rows():
    # структура ответа searchanalytics.query при dims=[date] (тоталы, вкл. анонимные)
    resp = {
        "rows": [
            {"keys": ["2026-08-10"], "clicks": 300.0, "impressions": 3500.0,
             "ctr": 0.086, "position": 2.1},
            {"keys": ["2026-08-11"], "clicks": 280.0, "impressions": 3300.0},
            {"keys": [], "clicks": 5.0, "impressions": 9.0},  # нет ключей → пропуск
        ]
    }
    assert parse_daily_totals(resp) == [
        {"date": "2026-08-10", "clicks": 300, "impressions": 3500},
        {"date": "2026-08-11", "clicks": 280, "impressions": 3300},
    ]


def test_parse_daily_totals_empty():
    assert parse_daily_totals({}) == []
    assert parse_daily_totals({"rows": []}) == []


def test_regions_match_pavel_sheets_method():
    """Методика листов 1-в-1 (решение 2026-08-20): KZ — общий хост с гео-фильтром
    Казахстан; GCC — витрины целиком, страна строки = страна витрины."""
    kz = REGIONS["kz"]
    assert kz["sites"] == {"https://limestore.com/": ""}
    assert kz["country_filter"] == "kaz"  # без него на общем хосте — Россия

    gcc = REGIONS["gcc"]
    assert gcc["country_filter"] is None  # витрина целиком, любые страны пользователей
    # Только витрины Залива: корневой домен в GCC не входит (клики с него ведут на
    # глобальный сайт, а не в магазин) — см. докстринг sync/gsc.py.
    assert "https://limestore.com/" not in gcc["sites"]
    assert len(gcc["sites"]) == 6
    assert all(s.endswith(".limestore.com/") for s in gcc["sites"])
    assert gcc["sites"]["https://ae.limestore.com/"] == "ОАЭ"
    assert gcc["sites"]["https://sa.limestore.com/"] == "Саудовская Аравия"


def test_aggregate_weekly_sums_days_into_iso_week():
    rows = [
        {"date": "2025-06-02", "clicks": 10, "impressions": 100, "country": "ОАЭ"},  # Пн
        {"date": "2025-06-03", "clicks": 4, "impressions": 40, "country": "ОАЭ"},   # Вт → та же неделя
        {"date": "2025-06-09", "clicks": 5, "impressions": 50, "country": "ОАЭ"},   # след. неделя
    ]
    assert aggregate_weekly(rows) == {
        ("2025-06-02", "ОАЭ"): {"clicks": 14, "impressions": 140},
        ("2025-06-09", "ОАЭ"): {"clicks": 5, "impressions": 50},
    }


def test_aggregate_weekly_keeps_countries_apart():
    rows = [
        {"date": "2025-06-02", "clicks": 10, "impressions": 100, "country": "ОАЭ"},
        {"date": "2025-06-03", "clicks": 5, "impressions": 50, "country": "Катар"},
    ]
    out = aggregate_weekly(rows)
    assert out[("2025-06-02", "ОАЭ")] == {"clicks": 10, "impressions": 100}
    assert out[("2025-06-02", "Катар")] == {"clicks": 5, "impressions": 50}  # та же ISO-неделя


def _fake_service(captured, site_urls):
    class FakeQuery:
        def __init__(self, body):
            self.body = body

        def execute(self):
            captured.append(self.body)
            return {}

    class FakeSA:
        def query(self, siteUrl, body):
            return FakeQuery(body)

    class FakeSites:
        def list(self):
            class R:
                def execute(self_inner):
                    return {"siteEntry": [{"siteUrl": u} for u in site_urls]}
            return R()

    class FakeService:
        def searchanalytics(self):
            return FakeSA()

        def sites(self):
            return FakeSites()

    return FakeService()


def test_sync_gsc_seo_requests_from_monday(monkeypatch):
    """Инкремент «сегодня − 8 недель» падает в середину недели; запрос обязан уезжать
    к понедельнику, иначе граничная неделя перезаписывается усечённой суммой
    (порча недель 2026-05-18…06-22, обнаружена 2026-08-19)."""
    import sync.gsc as gsc

    captured = []
    monkeypatch.setattr(
        gsc, "get_searchconsole_service",
        lambda: _fake_service(captured, ["https://limestore.com/"]),
    )
    # 2026-06-24 — среда; пустой ответ → weekly пуст → до БД не доходит
    n = gsc.sync_gsc_seo("2026-06-24", "2026-08-19", "kz")
    assert n == 0
    assert [b["startDate"] for b in captured] == ["2026-06-22"]  # понедельник той недели


def test_sync_gsc_seo_kz_filters_country_gcc_does_not(monkeypatch):
    """KZ обязан слать гео-фильтр kaz (общий с RU хост), GCC — без фильтров вовсе,
    и оба — БЕЗ измерения query (иначе GSC прячет анонимные и клики теряются)."""
    import sync.gsc as gsc

    kz_bodies = []
    monkeypatch.setattr(
        gsc, "get_searchconsole_service",
        lambda: _fake_service(kz_bodies, ["https://limestore.com/"]),
    )
    gsc.sync_gsc_seo("2026-06-22", "2026-08-19", "kz")
    assert len(kz_bodies) == 1
    assert kz_bodies[0]["dimensions"] == ["date"]
    fil = kz_bodies[0]["dimensionFilterGroups"][0]["filters"]
    assert fil == [{"dimension": "country", "operator": "equals", "expression": "kaz"}]

    gcc_bodies = []
    monkeypatch.setattr(
        gsc, "get_searchconsole_service",
        lambda: _fake_service(gcc_bodies, list(gsc.REGIONS["gcc"]["sites"])),
    )
    gsc.sync_gsc_seo("2026-06-22", "2026-08-19", "gcc")
    assert len(gcc_bodies) == 6  # по запросу на витрину
    for b in gcc_bodies:
        assert b["dimensions"] == ["date"]
        assert "dimensionFilterGroups" not in b
