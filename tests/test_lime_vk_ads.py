# -*- coding: utf-8 -*-
"""Парсинг статистики VK Реклама (ads.vk.com API v2) в строки lime_vk_ads_stats.
Фикстуры — обрезанные реальные ответы probe 2026-07-22."""
import json, os
from sync.lime_vk_ads import parse_base_stats

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
