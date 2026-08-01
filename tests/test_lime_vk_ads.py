# -*- coding: utf-8 -*-
"""Парсинг статистики VK Реклама (ads.vk.com API v2) в строки lime_vk_ads_stats.
Фикстуры — обрезанные реальные ответы probe 2026-07-22."""
import json, os
from sync.lime_vk_ads import parse_base_stats, parse_goal_stats, _campaigns_from_json

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def test_parse_base_stats_maps_by_date_campaign():
    out = parse_base_stats(_load("vk_stats_base.json"))
    assert out[("2026-07-17", "122821840")] == {
        "shows": 7423, "clicks": 62, "spent": 1000.0, "goals_total": 79, "vk_result": 2,
    }
    # total/агрегатная секция не попадает в построчный map
    assert ("2026-07-16", "122821840") in out
    assert len(out) == 2


def test_parse_goal_stats_sums_same_goal():
    out = parse_goal_stats(_load("vk_stats_goals.json"))
    key = ("2026-07-15", "122821840")
    assert out[key]["jse:vk_ecom_product"] == {"count": 2, "value": 24998.0, "view_through": 0}
    # две строки ec:detail за дату суммируются
    assert out[key]["ec:detail"] == {"count": 5, "value": 0.0, "view_through": 1}


def test_build_rows_merges_base_goals_meta():
    from sync.lime_vk_ads import build_rows
    base = {("2026-07-15", "122821840"): {"shows": 100, "clicks": 5, "spent": 250.0,
                                          "goals_total": 8, "vk_result": 1}}
    goals = {("2026-07-15", "122821840"): {"ec:detail": {"count": 5, "value": 0.0, "view_through": 0}}}
    meta = {"122821840": {"name": "Внутренняя, Ж", "objective": "site_conversions", "status": "active"}}
    rows = build_rows(base, goals, meta)
    assert len(rows) == 1
    r = rows[0]
    assert r["region"] == "ru"
    assert r["campaign_name"] == "Внутренняя, Ж"
    assert r["objective"] == "site_conversions"
    assert r["spent"] == 250.0
    assert json.loads(r["conversions"]) == {"ec:detail": {"count": 5, "value": 0.0, "view_through": 0}}


def test_build_rows_row_without_goals_gets_empty_jsonb():
    from sync.lime_vk_ads import build_rows
    base = {("2026-07-16", "999"): {"shows": 1, "clicks": 0, "spent": 0.0, "goals_total": 0, "vk_result": 0}}
    rows = build_rows(base, {}, {})
    assert json.loads(rows[0]["conversions"]) == {}
    assert rows[0]["campaign_name"] is None


def test_campaigns_from_json():
    js = {"items": [
        {"id": 122821840, "name": "A", "objective": "site_conversions", "status": "active"},
        {"id": 70911932, "name": "B", "objective": "storeproductssales", "status": "blocked"},
    ]}
    out = _campaigns_from_json(js)
    assert out["122821840"] == {"name": "A", "objective": "site_conversions", "status": "active"}
    assert "70911932" in out


def test_build_rows_stamps_cabinet():
    base = {("2026-07-15", "15760469"): {"shows": 10, "clicks": 2, "spent": 50.0,
                                         "goals_total": 1, "vk_result": 1}}
    rows = __import__("sync.lime_vk_ads", fromlist=["build_rows"]).build_rows(base, {}, {}, cabinet="vkads_814620282")
    assert rows[0]["cabinet"] == "vkads_814620282"
    # дефолт без метки — пустая строка, не падает
    rows2 = __import__("sync.lime_vk_ads", fromlist=["build_rows"]).build_rows(base, {}, {})
    assert rows2[0]["cabinet"] == ""


def test_cabinets_collects_base_and_numbered(monkeypatch):
    from sync.lime_vk_ads import _cabinets
    for k in list(__import__("os").environ):
        if k.startswith("VK_CLIENT_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VK_CLIENT_ID", "b_id"); monkeypatch.setenv("VK_CLIENT_SECRET", "b_sec")
    monkeypatch.setenv("VK_CLIENT_ID_2", "id2"); monkeypatch.setenv("VK_CLIENT_SECRET_2", "sec2")
    monkeypatch.setenv("VK_CLIENT_ID_3", "id3"); monkeypatch.setenv("VK_CLIENT_SECRET_3", "sec3")
    # разрыв нумерации останавливает сбор
    assert _cabinets() == [("b_id", "b_sec"), ("id2", "sec2"), ("id3", "sec3")]
