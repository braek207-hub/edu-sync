# -*- coding: utf-8 -*-
"""Агрегация визитов/продаж Роистата в платный/бесплатный для отчёта LIME."""
import json
import os

import sync.lime_roistat_report as rep
from sync.lime_roistat_report import (
    TRAFFIC_TAB, _day_cell_updates, _fetch_agg_guarded, _orders_row, _traffic_row,
    aggregate_day,
)
from sync.roistat_api import parse_analytics

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "roistat_analytics_day.json")


def load_rows():
    with open(FIXTURE, "r", encoding="utf-8") as f:
        return parse_analytics(json.load(f))


def test_total_is_paid_plus_free():
    """Инвариант таблицы: органический + платный = общий (1576+5652=7228 на скрине)."""
    agg = aggregate_day(load_rows())
    assert agg["total_visits"] == agg["paid_visits"] + agg["free_visits"]
    assert agg["total_orders"] == agg["paid_orders"] + agg["free_orders"]


def test_direct_visits_go_to_free():
    """«Прямые визиты» (2666) — бесплатный трафик, не платный."""
    agg = aggregate_day(load_rows())
    assert agg["free_visits"] >= 2666
    assert agg["paid_visits"] > 0  # Google/Meta/Директ в фикстуре есть


def test_paid_bucket_sums_paid_channels_only():
    """Только каналы с traffic_type=Платный идут в paid_visits."""
    rows = [
        {"channel": "Google Ads 1", "level2": "Поиск", "level2_id": "g",
         "visits": 100, "paid_leads": 5, "leads": 8},
        {"channel": "Прямые визиты", "level2": "", "level2_id": "",
         "visits": 40, "paid_leads": 2, "leads": 3},
        {"channel": "SEO", "level2": "Google", "level2_id": "google",
         "visits": 30, "paid_leads": 1, "leads": 4},
    ]
    agg = aggregate_day(rows)
    assert agg["paid_visits"] == 100
    assert agg["free_visits"] == 70          # Direct 40 + SEO 30
    assert agg["total_visits"] == 170
    assert agg["paid_orders"] == 5
    assert agg["free_orders"] == 3
    assert agg["total_leads"] == 15


def test_empty_rows_give_zeros():
    agg = aggregate_day([])
    assert agg["total_visits"] == 0
    assert agg["total_orders"] == 0


def test_row_builders_match_headers():
    """Порядок значений в строке = порядок колонок заголовка (иначе съедет запись)."""
    agg = {"free_visits": 70, "paid_visits": 100, "total_visits": 170,
           "paid_orders": 5, "free_orders": 3, "total_orders": 8}
    tr = _traffic_row("2026-07-04", agg)
    assert tr == ["Sat 04/07/2026", 70, 100, 170, 170, 2026, 7]
    orow = _orders_row("2026-07-04", agg)
    assert orow == ["Sat 04/07/2026", 5, 3, 8, 2026, 7]


def _patch_fetch(monkeypatch, side):
    monkeypatch.setattr(rep, "EMPTY_RETRY_SLEEP", 0)
    monkeypatch.setattr(rep, "EMPTY_RETRIES", 3)
    calls = {"n": 0}

    def fake(day, project, key):
        calls["n"] += 1
        return side(calls["n"])

    monkeypatch.setattr(rep, "fetch_day", fake)
    return calls


def test_guarded_returns_data_when_present(monkeypatch):
    rows = [{"channel": "Google Ads 1", "level2": "Поиск", "level2_id": "g",
             "visits": 100, "paid_leads": 5, "leads": 8}]
    _patch_fetch(monkeypatch, lambda n: rows)
    agg, has = _fetch_agg_guarded("2026-07-04", "k")
    assert has is True
    assert agg["paid_visits"] == 100


def test_guarded_empty_day_is_not_written(monkeypatch):
    """Пустой день (данные не готовы) → has_data=False, чтобы не затереть прежнее нулями."""
    calls = _patch_fetch(monkeypatch, lambda n: [])
    agg, has = _fetch_agg_guarded("2026-07-04", "k")
    assert has is False
    assert agg["total_visits"] == 0
    assert calls["n"] == 3  # исчерпал ретраи пустого ответа


def test_guarded_survives_roistat_error(monkeypatch):
    """Ошибка Роистата после его ретраев не роняет прогон — день просто пропускается."""
    def boom(n):
        raise RuntimeError("не забран после 3 попыток")
    _patch_fetch(monkeypatch, boom)
    agg, has = _fetch_agg_guarded("2026-07-04", "k")
    assert has is False


def test_guarded_recovers_on_late_data(monkeypatch):
    """Первые попытки пусто, затем данные пришли → пишем их."""
    rows = [{"channel": "Прямые визиты", "level2": "", "level2_id": "",
             "visits": 40, "paid_leads": 2, "leads": 3}]
    _patch_fetch(monkeypatch, lambda n: rows if n >= 2 else [])
    agg, has = _fetch_agg_guarded("2026-07-04", "k")
    assert has is True
    assert agg["free_visits"] == 40


# Колонки съехали: заказчик вставил пустой столбец перед «Дата» → данные в B:H.
_SHIFTED_COLS = {"Дата": 1, "Трафик органический": 2, "Трафик платный": 3,
                 "Трафик сайт": 4, "Трафик общий": 5, "Год": 6, "Месяц": 7}
_AGG = {"free_visits": 70, "paid_visits": 100, "total_visits": 170,
        "paid_orders": 5, "free_orders": 3, "total_orders": 8}


def test_updates_go_to_named_columns_not_A():
    """Баг-регресс: запись идёт в колонки ПО ИМЕНИ (B:H), а не в фиксированную A."""
    ups = _day_cell_updates(TRAFFIC_TAB, "2026-07-28", _AGG, _SHIFTED_COLS,
                            formulas=[], ri0=28, is_new=False)
    cells = dict(ups)
    # ri0=28 → 1-based строка 29; органический=C, платный=D, сайт=E, общий=F
    assert cells[f"{TRAFFIC_TAB}!C29"] == [[70]]
    assert cells[f"{TRAFFIC_TAB}!D29"] == [[100]]
    assert cells[f"{TRAFFIC_TAB}!E29"] == [[170]]
    assert cells[f"{TRAFFIC_TAB}!F29"] == [[170]]
    assert not any(r.endswith("!A29") for r, _ in ups)  # колонку A не трогаем


def test_existing_row_skips_formula_cells():
    """На существующей строке ячейку-формулу не перезаписываем."""
    formulas = [[""] * 8 for _ in range(30)]
    formulas[28][4] = "=C29+D29"  # «Трафик сайт» — формула
    ups = _day_cell_updates(TRAFFIC_TAB, "2026-07-28", _AGG, _SHIFTED_COLS,
                            formulas=formulas, ri0=28, is_new=False)
    cells = dict(ups)
    assert f"{TRAFFIC_TAB}!E29" not in cells      # формула сохранена
    assert cells[f"{TRAFFIC_TAB}!D29"] == [[100]]  # платный записан


def test_msk_today_is_utc_plus_3():
    """Окно считаем по московской дате (крон 23:44 UTC = 02:44 МСК): раннер в UTC,
    иначе до полуночи UTC «вчера» отставал бы на день."""
    from datetime import datetime, timedelta, timezone

    from sync.lime_roistat_report import _msk_today
    assert _msk_today() == (datetime.now(timezone.utc) + timedelta(hours=3)).date()


def test_new_row_fills_date_year_month():
    """Новая дата пишется целиком: Дата/Год/Месяц тоже (формул на пустой строке нет)."""
    ups = _day_cell_updates(TRAFFIC_TAB, "2026-07-28", _AGG, _SHIFTED_COLS,
                            formulas=[], ri0=28, is_new=True)
    cells = dict(ups)
    assert cells[f"{TRAFFIC_TAB}!B29"] == [["Tue 28/07/2026"]]  # Дата в колонке B (слэши, англ.)
    assert cells[f"{TRAFFIC_TAB}!G29"] == [[2026]]
    assert cells[f"{TRAFFIC_TAB}!H29"] == [[7]]


def test_date_re_matches_slashes_and_dots():
    """Регексп даты понимает и слэши (новый формат заказчика), и точки (старый)."""
    for label, iso in (("Sun 05/07/2026", "2026-07-05"), ("Сб 04.07.2026", "2026-07-04"),
                       ("05/07/2026", "2026-07-05")):
        m = rep._DATE_RE.search(label)
        assert m, label
        assert f"{m[3]}-{m[2]}-{m[1]}" == iso


def test_orders_value_map_has_new_names():
    """Новые имена колонок заказов маппятся; APP-колонку не трогаем (Роистат KZ без app)."""
    om = rep._VALUE_MAP[rep.ORDERS_TAB]
    assert om["Orders (Paid Traffic)"] == "paid_orders"
    assert om["Orders (Organic Traffic)"] == "free_orders"
    assert om["Orders (Org + Paid)"] == "total_orders"
    assert "Orders (APP Traffic)" not in om
