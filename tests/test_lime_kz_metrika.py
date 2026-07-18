# -*- coding: utf-8 -*-
"""Свёртка строк KZ-Метрики в кортежи lime_stats."""
from sync.lime_kz_metrika import COLUMNS, REGION, build_rows

DIRECT_MAP = {"Поиск. Бренд": ("119566511", True), "ТК муж.": ("706806515", False)}
GOOGLE_MAP = {"23952118304": "PMax Retargeting"}
ADGROUP_MAP = {}
MAPS = (DIRECT_MAP, GOOGLE_MAP, ADGROUP_MAP)

I = {name: i for i, name in enumerate(COLUMNS)}


def _row(**kw):
    base = {
        "date": "2026-07-15", "traffic_source": "ad", "source_engine": "Yandex: Direct",
        "direct_campaign_name": None, "utm_campaign": None, "utm_content": None,
        "visits": 100.0, "users": 80.0, "new_users": 40.0,
        "bounce_rate": 20.0, "page_depth": 5.0,
        "cart_reaches": 30.0, "checkout_reaches": 10.0,
        "orders": 5.0, "revenue": 60000.0,
    }
    base.update(kw)
    return base


def test_revenue_converted_to_rubles_and_region_tagged():
    rows = build_rows([_row()], MAPS, {}, 0.162, "2026-07-15")
    assert len(rows) == 1
    r = rows[0]
    assert r[I["region"]] == REGION == "kz_metrika"
    assert r[I["date"]] == "2026-07-15"
    assert r[I["purchases_revenue"]] == 9720.0     # 60000 тенге × 0.162
    assert r[I["sessions"]] == 100
    assert r[I["new_users"]] == 40


def test_cost_written_only_for_kz_cabinet_campaign():
    cost_map = {("2026-07-15", "119566511"): 4450.0, ("2026-07-15", "706806515"): 99999.0}
    rows = build_rows(
        [_row(direct_campaign_name="Поиск. Бренд"), _row(direct_campaign_name="ТК муж.")],
        MAPS, cost_map, 0.162, "2026-07-15")
    by_campaign = {r[I["campaign_id"]]: r for r in rows}
    assert by_campaign["119566511"][I["cost"]] == 4450.0   # KZ-кабинет → расход переносим
    assert by_campaign["706806515"][I["cost"]] == 0.0      # пролив RU → расход остаётся в RU


def test_unresolved_campaign_rows_collapse_to_channel():
    """Две строки без кампании в одном канале дают одну строку."""
    rows = build_rows([_row(), _row(visits=50.0, orders=1.0, revenue=10000.0)],
                      MAPS, {}, 0.162, "2026-07-15")
    assert len(rows) == 1
    assert rows[0][I["sessions"]] == 150
    assert rows[0][I["purchases_count"]] == 6
    assert rows[0][I["campaign_id"]] == ""


def test_bounce_and_depth_are_visit_weighted_on_collapse():
    """20% на 100 визитов + 40% на 300 → 35% на 400 визитов."""
    rows = build_rows(
        [_row(bounce_rate=20.0, page_depth=4.0, visits=100.0),
         _row(bounce_rate=40.0, page_depth=8.0, visits=300.0)],
        MAPS, {}, 0.162, "2026-07-15")
    assert rows[0][I["sessions"]] == 400
    assert rows[0][I["bounce_rate"]] == 35.0
    assert rows[0][I["page_depth"]] == 7.0


def test_channel_taxonomy_and_traffic_type():
    rows = build_rows([_row(traffic_source="organic", source_engine="Google")],
                      MAPS, {}, 0.162, "2026-07-15")
    assert rows[0][I["channel"]] == "SEO"
    assert rows[0][I["subchannel"]] == "SEO Google"
    assert rows[0][I["traffic_type"]] == "Бесплатный"


def test_paid_row_marked_paid_so_drr_and_cpo_work():
    rows = build_rows([_row(direct_campaign_name="Поиск. Бренд")], MAPS, {}, 0.162, "2026-07-15")
    assert rows[0][I["traffic_type"]] == "Платный"


def test_cost_counted_once_per_campaign_per_day():
    """Расход кампании не задваивается, если её визиты пришли двумя строками каналов."""
    cost_map = {("2026-07-15", "119566511"): 4450.0}
    rows = build_rows(
        [_row(direct_campaign_name="Поиск. Бренд", source_engine="Yandex: Direct"),
         _row(direct_campaign_name="Поиск. Бренд", source_engine="Yandex.Direct: Undetermined")],
        MAPS, cost_map, 0.162, "2026-07-15")
    assert sum(r[I["cost"]] for r in rows) == 4450.0


def test_warns_when_paid_google_rows_have_zero_cost(capsys):
    """Google-расход завозит отдельный синк (google_ads_fx); если он отвалится — cost_map
    не получит запись для google-кампании, и расход молча станет 0. Должно быть громко."""
    rows = build_rows(
        [_row(traffic_source="ad", source_engine="Google Ads", utm_campaign="23952118304")],
        MAPS, {}, 0.162, "2026-07-15")
    assert rows[0][I["cost"]] == 0.0
    out = capsys.readouterr().out
    assert "lime_kz_metrika: WARN" in out
    assert "2026-07-15" in out


def test_no_warning_when_google_cost_present(capsys):
    """Тот же платный Google, но расход есть — предупреждения быть не должно."""
    cost_map = {("2026-07-15", "23952118304"): 5000.0}
    rows = build_rows(
        [_row(traffic_source="ad", source_engine="Google Ads", utm_campaign="23952118304")],
        MAPS, cost_map, 0.162, "2026-07-15")
    assert rows[0][I["cost"]] == 5000.0
    out = capsys.readouterr().out
    assert "WARN" not in out
