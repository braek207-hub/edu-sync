# -*- coding: utf-8 -*-
"""Парсинг статистики VK Реклама (ads.vk.com API v2) в строки lime_vk_ads_stats.
Фикстуры — обрезанные реальные ответы probe 2026-07-22."""
import json, os
from unittest.mock import patch

from sync.lime_vk_ads import parse_base_stats, parse_goal_stats, _campaigns_from_json
from sync.lime_vk_ads import _ad_groups_from_json, fetch_ad_groups, self_plan_rows

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


def test_ad_groups_from_json_maps_group_to_ad_plan():
    """id группы (макрос `c` в ссылке трекера AppMetrica) → id кампании (ad_plan)."""
    js = {"items": [
        {"id": 140704359, "name": "Группа Ж", "ad_plan_id": 22293644},
        {"id": 140704360, "name": "Группа М", "ad_plan_id": 22293644},
    ]}
    rows = _ad_groups_from_json(js, "vkads_809330054@vk")
    assert rows[0] == {"entity_id": "140704359", "kind": "ad_group",
                       "ad_plan_id": "22293644", "cabinet": "vkads_809330054@vk",
                       "name": "Группа Ж"}
    assert [r["ad_plan_id"] for r in rows] == ["22293644", "22293644"]


def test_ad_groups_from_json_skips_rows_without_plan():
    """Группа без ad_plan_id бесполезна для резолва — в справочник не попадает."""
    js = {"items": [{"id": 1, "name": "битая"}, {"id": 2, "ad_plan_id": None},
                    {"id": 3, "name": "ок", "ad_plan_id": 99}]}
    rows = _ad_groups_from_json(js, "cab")
    assert [r["entity_id"] for r in rows] == ["3"]


def test_self_plan_rows_point_ad_plan_to_itself():
    """Макрос `c` иногда несёт id кампании, а не группы — резолв должен работать и так."""
    rows = self_plan_rows(["22293644", 22293645], "cab")
    assert rows[0] == {"entity_id": "22293644", "kind": "ad_plan",
                       "ad_plan_id": "22293644", "cabinet": "cab", "name": None}
    assert rows[1]["entity_id"] == "22293645"


def test_sync_survives_entity_collection_failure_and_still_writes_spend():
    """Находка ревью: справочник — вспомогательный слой. Отказ на сборе сущностей ОДНОГО
    кабинета (5xx/таймаут — _api_get ретраит только 429) не должен ронять запись расхода —
    критичный путь. Мокаем сеть/БД, реальной БД не касаемся."""
    from sync import lime_vk_ads as m

    with patch.dict(os.environ, {"VK_CLIENT_ID": "cid", "VK_CLIENT_SECRET": "sec"}, clear=False), \
         patch.object(m, "_get_token", return_value="tok"), \
         patch.object(m, "_cabinet_login", return_value="cab1"), \
         patch.object(m, "fetch_ad_plans", return_value={"100": {"name": "A", "objective": "o", "status": "active"}}), \
         patch.object(m, "_fetch_cabinet_rows", return_value=[{"date": "2026-07-01", "region": "ru",
             "cabinet": "cab1", "campaign_id": "100", "campaign_name": "A", "objective": "o",
             "status": "active", "shows": 1, "clicks": 1, "spent": 10.0, "goals_total": 0,
             "vk_result": 0, "conversions": "{}"}]), \
         patch.object(m, "fetch_ad_groups", side_effect=RuntimeError("HTTP 503")), \
         patch.object(m, "_upsert") as mock_upsert, \
         patch.object(m, "_upsert_entities") as mock_upsert_entities:
        mock_upsert.return_value = 1
        mock_upsert_entities.return_value = 0
        n = m.sync_lime_vk_ads(days_back=1)

    assert n == 1
    # Расход дошёл до записи несмотря на упавший сбор справочника.
    spend_rows = mock_upsert.call_args[0][0]
    assert len(spend_rows) == 1 and spend_rows[0]["campaign_id"] == "100"
    # self_plan_rows (не сетевой вызов) для этого кабинета тоже пропущен — весь try-блок
    # прерван исключением из fetch_ad_groups до self_plan_rows.
    mock_upsert_entities.assert_called_once_with([])


def test_fetch_ad_groups_paginates_until_short_page():
    """VK отдаёт максимум 50 на страницу: короткая страница = последняя."""
    pages = [
        {"items": [{"id": i, "ad_plan_id": 100} for i in range(50)]},
        {"items": [{"id": 900, "ad_plan_id": 101}]},
    ]
    calls = []

    def fake_get(token, path, **kw):
        calls.append(path)
        return pages[len(calls) - 1]

    with patch("sync.lime_vk_ads._api_get", side_effect=fake_get), \
         patch("sync.lime_vk_ads.time.sleep"):
        rows = fetch_ad_groups("tok", "cab")

    assert len(rows) == 51
    assert "offset=0" in calls[0] and "offset=50" in calls[1]
    assert "fields=id,name,ad_plan_id" in calls[0]
