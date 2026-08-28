# -*- coding: utf-8 -*-
"""Тесты витрины целей Метрики: разбор ответа Stat API и свёртка в строки витрины."""
from sync.lime_metrika_goals import COLUMNS, build_rows
from sync.lime_metrika_goals_api import chunk_goals, fetch_goal_catalog, parse_goal_rows


def _col(row, name):
    return row[COLUMNS.index(name)]


def _resp(items):
    return {
        "query": {"dimensions": [
            "ym:s:date",
            "ym:s:lastsignTrafficSource",
            "ym:s:lastsignSourceEngine",
            "ym:s:lastsignDirectClickOrderName",
            "ym:s:lastsignUTMCampaign",
        ]},
        "data": items,
    }


def _item(date_s, source_id, engine, campaign, metrics):
    return {
        "dimensions": [
            {"name": date_s},
            {"id": source_id, "name": source_id},
            {"name": engine},
            {"name": "Кампания"},
            {"name": campaign},
        ],
        "metrics": metrics,
    }


def test_parse_splits_metrics_by_requested_goal_order():
    # Одна строка ответа = N метрик, по одной на запрошенную цель в порядке запроса.
    rows = parse_goal_rows(
        _resp([_item("2026-08-01", "ad", "Yandex.Direct", "709091521", [5.0, 0.0, 2.0])]),
        ["111", "222", "333"],
    )
    assert [(r["goal_id"], r["reaches"]) for r in rows] == [("111", 5.0), ("222", 0.0), ("333", 2.0)]
    assert rows[0]["utm_campaign"] == "709091521"
    assert rows[0]["traffic_source"] == "ad"


def test_parse_survives_short_metrics_list():
    # Метрика может вернуть меньше метрик, чем запрошено, — это не должно ронять разбор.
    rows = parse_goal_rows(
        _resp([_item("2026-08-01", "organic", "Yandex", "", [7.0])]),
        ["111", "222"],
    )
    assert [(r["goal_id"], r["reaches"]) for r in rows] == [("111", 7.0), ("222", 0.0)]


def test_parse_reads_dimension_positions_from_query_echo():
    # Позиции измерений берутся из эха query, а не из константы: порядок в ответе
    # задаёт API, и жёсткий индекс молча склеил бы кампанию с движком.
    resp = _resp([])
    resp["query"]["dimensions"] = ["ym:s:lastsignUTMCampaign", "ym:s:date"]
    resp["data"] = [{"dimensions": [{"name": "709091521"}, {"name": "2026-08-01"}],
                     "metrics": [3.0]}]
    rows = parse_goal_rows(resp, ["111"])
    assert rows[0]["date"] == "2026-08-01"
    assert rows[0]["utm_campaign"] == "709091521"
    assert rows[0]["source_engine"] is None


def test_chunk_goals_respects_metric_limit():
    ids = [str(i) for i in range(40)]
    chunks = chunk_goals(ids, size=18)
    assert [len(c) for c in chunks] == [18, 18, 4]
    assert [g for c in chunks for g in c] == ids


def test_build_rows_aggregates_by_channel_and_goal():
    # Две строки одной кампании и одной цели (разные utm_content и т.п. на стороне API)
    # должны сложиться в одну строку витрины.
    goal_rows = [
        {"traffic_source": "ad", "source_engine": "Yandex.Direct",
         "utm_campaign": "709091521", "goal_id": "111", "reaches": 5.0},
        {"traffic_source": "ad", "source_engine": "Yandex.Direct",
         "utm_campaign": "709091521", "goal_id": "111", "reaches": 3.0},
        {"traffic_source": "ad", "source_engine": "Yandex.Direct",
         "utm_campaign": "709091521", "goal_id": "222", "reaches": 1.0},
    ]
    rows = build_rows(goal_rows, "2026-08-01")
    by_goal = {_col(r, "goal_id"): r for r in rows}
    assert set(by_goal) == {"111", "222"}
    assert _col(by_goal["111"], "reaches") == 8
    assert _col(by_goal["111"], "channel") == "SEM"
    assert _col(by_goal["111"], "subchannel") == "Яндекс.Директ"
    assert _col(by_goal["111"], "traffic_type") == "Платный"
    assert _col(by_goal["111"], "campaign_id") == "709091521"
    assert _col(by_goal["111"], "date") == "2026-08-01"


def test_build_rows_drops_zero_reaches():
    # Нули — основная масса декартова продукта «цель × разрез»; в витрину не идут.
    rows = build_rows([
        {"traffic_source": "organic", "source_engine": "Yandex",
         "utm_campaign": "", "goal_id": "111", "reaches": 0.0},
        {"traffic_source": "organic", "source_engine": "Yandex",
         "utm_campaign": "", "goal_id": "222", "reaches": 4.0},
    ], "2026-08-01")
    assert [_col(r, "goal_id") for r in rows] == ["222"]
    assert _col(rows[0], "channel") == "SEO"
    assert _col(rows[0], "subchannel") == "SEO Yandex"


def test_build_rows_keeps_channels_separate():
    # Одна цель на разных каналах — разные строки: иначе достижения органики
    # приписались бы рекламе.
    rows = build_rows([
        {"traffic_source": "ad", "source_engine": "Yandex.Direct",
         "utm_campaign": "709091521", "goal_id": "111", "reaches": 5.0},
        {"traffic_source": "direct", "source_engine": None,
         "utm_campaign": "", "goal_id": "111", "reaches": 2.0},
    ], "2026-08-01")
    assert sorted((_col(r, "channel"), _col(r, "reaches")) for r in rows) == [
        ("Direct", 2), ("SEM", 5),
    ]


def test_goal_catalog_marks_autogoals(monkeypatch):
    # Автоцели Директа отличаются goal_source: без него дашборд не сможет их скрыть.
    payload = {"goals": [
        {"id": 111, "name": "Регистрация", "type": "action", "goal_source": "user"},
        {"id": 222, "name": "Автоцель: заказ", "type": "url", "goal_source": "auto",
         "is_retargeting": 1},
        {"name": "Цель без id"},
    ]}
    monkeypatch.setattr("sync.lime_metrika_goals_api._request", lambda *a, **k: payload)
    catalog = fetch_goal_catalog("23504302", "token")
    assert [g["goal_id"] for g in catalog] == ["111", "222"]
    assert catalog[0]["source"] == "user"
    assert catalog[1]["source"] == "auto"
    assert catalog[1]["is_retargeting"] is True
    assert catalog[0]["is_retargeting"] is False
