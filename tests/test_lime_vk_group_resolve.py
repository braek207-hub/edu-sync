import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync import lime

# Справочник группа VK → (кампания ad_plan, имя) — как его отдаёт load_vk_group_map.
GROUPS = {"149891557": ("27806937", "APP: iOs, Хиты продаж, 08/26")}


def _row(**over):
    base = dict(
        date="2026-09-01", data_source="app", region="ru", source="vk_ads", medium="cpc",
        campaign="149891557", ad_platform="VK Ads", campaign_id="(not set)",
        campaign_name="(not set)", cost=0, clicks=0, impressions=0, sessions=10, users=8,
        clients=0, purchases_count=1, purchases_revenue=5000.0, customers=1, new_users=0,
        new_customers=0, new_customers_revenue=0,
    )
    base.update(over)
    return base


def _keys(rows, groups=GROUPS):
    return {k[6:8] for k in lime.aggregate(rows, groups)}


def test_vk_group_id_without_campaign_resolves_to_ad_plan():
    """PROCONTEXT кладёт id ГРУППЫ VK в `campaign`, а campaign_id не резолвит —
    строка обязана сесть на кампанию (ad_plan) с её именем."""
    assert _keys([_row()]) == {("27806937", "APP: iOs, Хиты продаж, 08/26")}


def test_resolved_campaign_id_from_procontext_wins():
    """Если PROCONTEXT campaign_id уже резолвнул — его значение не перебиваем."""
    rows = [_row(campaign_id="22348498", campaign_name="APP: iOs, Женственные новинки 06/26")]
    assert _keys(rows) == {("22348498", "APP: iOs, Женственные новинки 06/26")}


def test_unknown_group_stays_unattributed():
    assert _keys([_row(campaign="999")]) == {("", "")}


def test_non_vk_source_is_not_resolved_by_group_dict():
    """Числовой `campaign` у другого источника — не id группы VK, справочник не применяем."""
    assert _keys([_row(source="yandex", ad_platform="(not set)")]) == {("", "")}


def test_resolved_rows_merge_with_procontext_rows_of_same_campaign():
    """Резолвнутая строка и строка, где PROCONTEXT сам поставил campaign_id, — один ключ."""
    rows = [
        _row(),
        _row(campaign_id="27806937", campaign_name="APP: iOs, Хиты продаж, 08/26", sessions=5),
    ]
    agg = lime.aggregate(rows, GROUPS)
    assert len(agg) == 1
    assert next(iter(agg.values()))["sessions"] == 15


def test_aggregate_without_dict_keeps_old_behaviour():
    assert _keys([_row()], groups=None) == {("", "")}
