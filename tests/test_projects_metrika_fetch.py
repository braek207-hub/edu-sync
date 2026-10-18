import json
from pathlib import Path

import pytest

from sync.projects import metrika_fetch as mf

FIX = Path(__file__).parent / "fixtures"


def load(kind):
    return json.loads((FIX / f"projects_metrika_{kind}.json").read_text(encoding="utf-8"))


def row(dims, metrics):
    return {"dimensions": [d if isinstance(d, dict) else {"name": d} for d in dims], "metrics": metrics}


# traffic_source, engine, utm_source, utm_medium, utm_campaign, DirectClickOrder
EMPTY_SOURCE = [
    {"id": "direct", "name": "Прямые заходы"},
    {"name": None},
    {"name": None},
    {"name": None},
    {"name": None},
    {"id": None, "owner": None, "name": None, "direct_id": None},
]


def test_parse_traffic_fixture():
    rows = mf.parse_traffic(load("traffic"))
    assert len(rows) == 5
    for r in rows:
        assert r.date == "2026-10-17"
        assert r.traffic_source
        assert isinstance(r.visits, int) and r.visits > 0
        assert 0 <= r.bounce_rate <= 100
        assert r.page_depth > 0
    direct = rows[2]
    assert direct.source_detail == "Яндекс.Директ"
    assert direct.utm_source and direct.utm_medium and direct.utm_campaign


def test_direct_campaign_id_from_dimension():
    raw = load("traffic")
    rows = mf.parse_traffic(raw)
    assert rows[0].direct_campaign_id == ""
    assert all(r.direct_campaign_id.isdigit() for r in rows[2:])
    # id и direct_id расходятся: берём direct_id без "N-"
    el = raw[3]["dimensions"][6]
    assert (el["id"], el["direct_id"]) == ("218498117", "N-118498117")
    assert rows[3].direct_campaign_id == "118498117"
    assert rows[2].direct_campaign_id == "703083615"


def test_direct_campaign_id_falls_back_to_id_without_direct_id():
    dims = [
        "2026-10-17",
        {"id": "ad", "name": "Переходы по рекламе"},
        {"name": "Яндекс: Директ"},
        {"name": "ya.direct"},
        {"name": "cpc"},
        {"name": "x"},
        {"id": "218498117", "owner": "1", "name": "Кампания", "direct_id": None},
    ]
    r = mf.parse_traffic([row(dims, [10.0, 5.5, 2.0])])[0]
    assert r.direct_campaign_id == "218498117"


def test_direct_campaign_id_falls_back_to_name():
    dims = [
        "2026-10-17",
        {"id": "ad", "name": "Переходы по рекламе"},
        {"name": "Яндекс: Директ"},
        {"name": "ya.direct"},
        {"name": "cpc"},
        {"name": "x"},
        {"name": "703083615-кампания"},
    ]
    r = mf.parse_traffic([row(dims, [10.0, 5.5, 2.0])])[0]
    assert r.direct_campaign_id == "703083615"


def test_parse_goals_fixture():
    ids = [3023504302, 194380276, 340814310]
    rows = mf.parse_goals(load("goals"), ids)
    assert len(rows) == 15
    assert {r.goal_id for r in rows} == set(ids)
    assert all(isinstance(r.reaches, int) and r.reaches > 0 for r in rows)


def test_goal_rows_skip_zero():
    rows = mf.parse_goals([row(["2026-10-17", *EMPTY_SOURCE], [0.0, 3.0])], [1, 2])
    assert [(r.goal_id, r.reaches) for r in rows] == [(2, 3)]


def test_parse_purchases_fixture():
    rows = mf.parse_purchases(load("purchases"))
    assert len(rows) == 5
    for r in rows:
        assert r.order_id and r.purchase_date == "2026-10-17"
        assert isinstance(r.purchases, int) and r.revenue >= 0


def _purchase(day, rev):
    return row([day, "A1", *EMPTY_SOURCE], [1.0, rev])


def test_purchase_dedupe_keeps_latest_date():
    rows = mf.parse_purchases([_purchase("2026-10-16", 100.0), _purchase("2026-10-17", 200.0), _purchase("2026-10-15", 50.0)])
    assert len(rows) == 1
    assert (rows[0].purchase_date, rows[0].revenue) == ("2026-10-17", 200.0)


def test_purchase_dedupe_tie_keeps_first():
    rows = mf.parse_purchases([_purchase("2026-10-17", 100.0), _purchase("2026-10-17", 300.0)])
    assert [r.revenue for r in rows] == [100.0]


def test_products_skip_zero_and_none_literal():
    data = [
        row(["2026-10-17", "1", "Платье"], [2.0, 1999.999]),
        row(["2026-10-17", "None", "None"], [1.0, 10.0]),
        row(["2026-10-17", "3", "Юбка"], [0.0, 0.0]),
    ]
    rows = mf.parse_products(data)
    assert [(r.product_id, r.product_name, r.quantity, r.revenue) for r in rows] == [
        ("1", "Платье", 2, 2000.0),
        ("", "", 1, 10.0),
    ]


def test_parse_products_fixture():
    rows = mf.parse_products(load("products"))
    assert len(rows) == 5 and all(r.quantity > 0 and r.product_name for r in rows)


@pytest.fixture
def calls(monkeypatch):
    log = []

    def fake(params, token, **kw):
        log.append(params)
        return []

    monkeypatch.setattr(mf, "metrica_stat_rows", fake)
    return log


def test_goals_batches_of_10(calls):
    mf.fetch_goals(1, "t", "2026-10-01", "2026-10-02", list(range(100, 123)))
    assert [len(p["metrics"].split(",")) for p in calls] == [10, 10, 3]


def test_params_use_automatic_attribution(calls):
    mf.fetch_traffic(1, "t", "2026-10-01", "2026-10-02")
    mf.fetch_goals(1, "t", "2026-10-01", "2026-10-02", [5])
    mf.fetch_purchases(1, "t", "2026-10-01", "2026-10-02")
    mf.fetch_products(1, "t", "2026-10-01", "2026-10-02")
    assert len(calls) == 4
    for p in calls:
        assert p["attribution"] == "automatic"
        assert p["accuracy"] == "full"
        assert p["proposed_accuracy"] == "false"
        assert "<attribution>" not in p["dimensions"]
    assert "DirectClickOrder" in calls[0]["dimensions"]
    assert "clientID" not in calls[2]["dimensions"]
    assert calls[2]["filters"] == "ym:s:ecommercePurchases>0"


def _traffic_dims(utm_campaign):
    return [
        "2026-10-17",
        {"id": "ad", "name": "Переходы по рекламе"},
        {"name": "Яндекс: Директ"},
        {"name": "ya.direct"},
        {"name": "cpc"},
        {"name": utm_campaign},
        {"id": None, "owner": None, "name": None, "direct_id": None},
    ]


def test_traffic_merge_colliding_keys_weighted_and_skip_zero_visits():
    data = [
        row(_traffic_dims("703083615-x"), [100.0, 10.0, 2.0]),
        row(_traffic_dims("703083615"), [300.0, 30.0, 4.0]),
        row(_traffic_dims("999999999"), [0.0, 50.0, 9.0]),
    ]
    rows = mf.parse_traffic(data)
    assert len(rows) == 1
    r = rows[0]
    assert r.utm_campaign == "703083615"
    assert r.visits == 400
    assert r.bounce_rate == 25.0  # (10*100 + 30*300) / 400
    assert r.page_depth == 3.5  # (2*100 + 4*300) / 400


def test_goal_merge_colliding_keys_sums_reaches():
    data = [
        row(_traffic_dims("703083615-x"), [2.0, 0.0]),
        row(_traffic_dims("703083615"), [3.0, 4.0]),
    ]
    rows = mf.parse_goals(data, [1, 2])
    assert {(r.goal_id, r.reaches) for r in rows} == {(1, 5), (2, 4)}


def test_products_merge_colliding_keys_sums():
    data = [
        row(["2026-10-17", "None", "None"], [1.0, 10.0]),
        row(["2026-10-17", "", ""], [2.0, 5.5]),
    ]
    rows = mf.parse_products(data)
    assert [(r.product_id, r.product_name, r.quantity, r.revenue) for r in rows] == [("", "", 3, 15.5)]


def test_fetch_goals_dedupes_goal_ids(calls):
    mf.fetch_goals(1, "t", "2026-10-01", "2026-10-02", [7, 8, 7, 8, 9])
    assert [p["metrics"] for p in calls] == ["ym:s:goal7reaches,ym:s:goal8reaches,ym:s:goal9reaches"]
