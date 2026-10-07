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


def test_regions_country_of_user_method():
    """KZ — общий хост с гео Казахстан. GCC (решение 2026-10-07) — строка страны =
    её пользователи: у каждой витрины свой гео-код, корень подмешивается отдельно."""
    kz = REGIONS["kz"]
    assert kz["sites"] == {"https://limestore.com/": ""}
    assert kz["country_filter"] == "kaz"  # без него на общем хосте — Россия

    gcc = REGIONS["gcc"]
    assert "https://limestore.com/" not in gcc["sites"]  # корень — не витрина, а добавка
    assert gcc["root"] == "https://limestore.com/"
    assert len(gcc["sites"]) == 6 and set(gcc["codes"]) == set(gcc["sites"])
    assert gcc["sites"]["https://ae.limestore.com/"] == "ОАЭ"
    assert gcc["codes"]["https://ae.limestore.com/"] == "are"
    assert gcc["codes"]["https://bh.limestore.com/"] == "bhr"


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
    assert [b["startDate"] for b in captured] == ["2026-06-22", "2026-06-22"]  # тотал + небренд


def test_sync_gsc_seo_quality_brand_two_queries(monkeypatch):
    """«Качественный бренд» = тотал − excludingRegex(бренд), оба dims=[date]
    (анонимные остаются в разности — они выпадают из любой query-выборки).
    KZ дополнительно шлёт гео-фильтр kaz в ОБА запроса (общий с RU хост),
    GCC — без гео (страна строки = витрина)."""
    import sync.gsc as gsc

    kz_bodies = []
    monkeypatch.setattr(
        gsc, "get_searchconsole_service",
        lambda: _fake_service(kz_bodies, ["https://limestore.com/"]),
    )
    gsc.sync_gsc_seo("2026-06-22", "2026-08-19", "kz")
    assert len(kz_bodies) == 2  # тотал + видимый небренд
    total_b, nb_b = kz_bodies
    assert total_b["dimensions"] == ["date"] and nb_b["dimensions"] == ["date"]
    assert total_b["dimensionFilterGroups"][0]["filters"] == [
        {"dimension": "country", "operator": "equals", "expression": "kaz"}
    ]
    nb_filters = nb_b["dimensionFilterGroups"][0]["filters"]
    assert {"dimension": "country", "operator": "equals", "expression": "kaz"} in nb_filters
    assert any(f["dimension"] == "query" and f["operator"] == "excludingRegex"
               for f in nb_filters)

    # GCC без доступа к корню: 6 витрин × (тотал + небренд), каждый — с гео страны витрины
    gcc_bodies = []
    monkeypatch.setattr(
        gsc, "get_searchconsole_service",
        lambda: _fake_service(gcc_bodies, list(gsc.REGIONS["gcc"]["sites"])),
    )
    gsc.sync_gsc_seo("2026-06-22", "2026-08-19", "gcc")
    assert len(gcc_bodies) == 12
    codes = set(gsc.REGIONS["gcc"]["codes"].values())
    for b in gcc_bodies:
        fil = b["dimensionFilterGroups"][0]["filters"]
        assert fil[0]["dimension"] == "country" and fil[0]["expression"] in codes
    nbs = [b for b in gcc_bodies if len(b["dimensionFilterGroups"][0]["filters"]) == 2]
    assert len(nbs) == 6
    for b in nbs:
        q = b["dimensionFilterGroups"][0]["filters"][1]
        assert q["operator"] == "excludingRegex"
        assert "لايم" in q["expression"]  # арабские написания в GCC-регексе


class _RowsService:
    """Фейк GSC: ответ выбирается по (сайт, измерения, есть ли фильтр запроса и какой)."""

    def __init__(self, table):
        self.table = table

    def searchanalytics(self):
        svc = self

        class SA:
            def query(self, siteUrl, body):
                fil = body.get("dimensionFilterGroups", [{}])[0].get("filters", [])
                op = next((f["operator"] for f in fil if f["dimension"] == "query"), None)
                rows = svc.table.get((siteUrl, tuple(body["dimensions"]), op), [])

                class R:
                    def execute(self_inner):
                        return {"rows": rows}
                return R()
        return SA()


def test_root_extra_impressions_counts_only_searches_without_storefront():
    from sync.gsc import root_extra_impressions

    home = [{"keys": ["2026-09-07", "lime", "MOBILE"], "impressions": 10}]
    root = [
        {"keys": ["2026-09-07", "lime", "MOBILE"], "impressions": 8},    # та же выдача → 0
        {"keys": ["2026-09-07", "lime", "DESKTOP"], "impressions": 3},   # витрины не было → 3
        {"keys": ["2026-09-08", "limestore", "MOBILE"], "impressions": 5},
    ]
    root.append({"keys": ["2026-09-07", "lime uae", "MOBILE"], "impressions": 4})
    home.append({"keys": ["2026-09-07", "lime uae", "MOBILE"], "impressions": 1})  # превышение 3
    assert root_extra_impressions(home, root) == {"2026-09-07": 6, "2026-09-08": 5}


def test_fetch_gcc_country_adds_root_clicks_and_extra_impressions():
    """Клики корня прибавляются целиком, показы — только сверх витрины."""
    from sync.gsc import fetch_gcc_country

    ae, root = "https://ae.limestore.com/", "https://limestore.com/"
    d = "2026-09-07"
    svc = _RowsService({
        (ae, ("date",), None): [{"keys": [d], "clicks": 120, "impressions": 2000}],
        (ae, ("date",), "excludingRegex"): [{"keys": [d], "clicks": 20, "impressions": 1000}],
        (root, ("date",), None): [{"keys": [d], "clicks": 40, "impressions": 900}],
        (root, ("date",), "excludingRegex"): [{"keys": [d], "clicks": 5, "impressions": 100}],
        (ae, ("date", "query", "device"), "includingRegex"):
            [{"keys": [d, "lime", "MOBILE"], "impressions": 700}],
        (root, ("date", "query", "device"), "includingRegex"):
            [{"keys": [d, "lime", "MOBILE"], "impressions": 600},
             {"keys": [d, "lime", "DESKTOP"], "impressions": 30}],
    })
    assert fetch_gcc_country(svc, ae, "are", root, d, d) == [
        {"date": d, "clicks": 100 + 35, "impressions": 1000 + 30},
    ]
    # без корня — только витрина
    assert fetch_gcc_country(svc, ae, "are", None, d, d) == [
        {"date": d, "clicks": 100, "impressions": 1000},
    ]


def test_subtract_days_clamps_negative():
    from sync.gsc import subtract_days

    total = [{"date": "2026-08-10", "clicks": 100, "impressions": 1000},
             {"date": "2026-08-11", "clicks": 5, "impressions": 50}]
    nonbrand = [{"date": "2026-08-10", "clicks": 30, "impressions": 400},
                {"date": "2026-08-11", "clicks": 7, "impressions": 60}]  # рассинхрон выборок
    assert subtract_days(total, nonbrand) == [
        {"date": "2026-08-10", "clicks": 70, "impressions": 600},
        {"date": "2026-08-11", "clicks": 0, "impressions": 0},
    ]


def test_aggregate_daily_sums_same_day_and_country():
    """Дневная свёртка складывает строки одного дня и страны (две витрины одной страны),
    а разные страны держит порознь — иначе executemany с ON CONFLICT записал бы
    последнюю строку вместо суммы."""
    from sync.gsc import aggregate_daily

    rows = [
        {"date": "2026-08-10", "clicks": 10, "impressions": 100, "country": "ОАЭ"},
        {"date": "2026-08-10", "clicks": 5, "impressions": 50, "country": "ОАЭ"},
        {"date": "2026-08-10", "clicks": 2, "impressions": 20, "country": "Катар"},
        {"date": "2026-08-11", "clicks": 1, "impressions": 9, "country": "ОАЭ"},
    ]
    assert aggregate_daily(rows) == {
        ("2026-08-10", "ОАЭ"): {"clicks": 15, "impressions": 150},
        ("2026-08-10", "Катар"): {"clicks": 2, "impressions": 20},
        ("2026-08-11", "ОАЭ"): {"clicks": 1, "impressions": 9},
    }


def test_aggregate_daily_defaults_country_to_empty():
    """KZ приходит без страны (регион целиком) — ключ страны пустой, как в таблице."""
    from sync.gsc import aggregate_daily

    assert aggregate_daily([{"date": "2026-08-10", "clicks": 3, "impressions": 30}]) == {
        ("2026-08-10", ""): {"clicks": 3, "impressions": 30},
    }
