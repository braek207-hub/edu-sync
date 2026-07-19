# -*- coding: utf-8 -*-
"""Мост кампаний Метрика↔Triple Whale (добивка П3 на уровне кампаний)."""
import pytest

from sync.gcc_campaign_bridge import (
    bridge_metrika_campaign,
    build_campaign_index,
    resolve_campaign,
)

# Реальные пары из ads_table (зонд 2026-07-19).
ADS_ROWS = [
    {"campaign_id": "120249879465530017", "campaign_name": "CPO_SUMMER_SALE_KSA"},
    {"campaign_id": "120249835221500017", "campaign_name": "CPO_SUMMER_SALE_UAE"},
    {"campaign_id": "120250980621530017", "campaign_name": "CPO_NEW IN_W_27"},
    {"campaign_id": "120248835051720017", "campaign_name": "CPO_NEW IN_W_23"},
    {"campaign_id": "120247612269810017", "campaign_name": "CPO_NEW IN_W"},
    {"campaign_id": "120239706697970017", "campaign_name": "CPO_Catalog_All – NEW"},
]
INDEX = build_campaign_index(ADS_ROWS)


class TestIndex:
    def test_builds_name_to_id(self):
        assert INDEX["CPO_SUMMER_SALE_KSA"] == "120249879465530017"

    def test_skips_rows_without_id_or_name(self):
        idx = build_campaign_index([
            {"campaign_id": "", "campaign_name": "X"},
            {"campaign_id": "1", "campaign_name": ""},
        ])
        assert idx == {}


class TestResolve:
    @pytest.mark.parametrize("utm,expected", [
        # Реальные метки Метрики: {плейсмент}-{имя кампании}
        ("Instagram_Feed-CPO_SUMMER_SALE_KSA", "120249879465530017"),
        ("Facebook_Marketplace-CPO_SUMMER_SALE_UAE", "120249835221500017"),
        ("Facebook_Instream_Video-CPO_SUMMER_SALE_KSA", "120249879465530017"),
        ("an-CPO_SUMMER_SALE_KSA", "120249879465530017"),
        ("Facebook_Mobile_Feed-CPO_Catalog_All – NEW", "120239706697970017"),
    ])
    def test_strips_placement_prefix(self, utm, expected):
        assert resolve_campaign(utm, INDEX) == expected

    def test_exact_name_without_prefix(self):
        assert resolve_campaign("CPO_SUMMER_SALE_UAE", INDEX) == "120249835221500017"

    def test_longest_suffix_wins(self):
        """«CPO_NEW IN_W» — суффикс-ловушка для «CPO_NEW IN_W_27»: без выбора самого
        длинного имени визиты 27-й недели уехали бы в чужую кампанию."""
        assert resolve_campaign("Instagram_Feed-CPO_NEW IN_W_27", INDEX) == "120250980621530017"
        assert resolve_campaign("Instagram_Feed-CPO_NEW IN_W_23", INDEX) == "120248835051720017"
        assert resolve_campaign("Instagram_Feed-CPO_NEW IN_W", INDEX) == "120247612269810017"

    def test_numeric_utm_passes_through(self):
        """Google Ads пишет в utm сразу id — трогать не надо."""
        assert resolve_campaign("21067876545", INDEX) == "21067876545"

    def test_case_insensitive(self):
        assert resolve_campaign("instagram_feed-cpo_summer_sale_ksa", INDEX) == "120249879465530017"

    @pytest.mark.parametrize("utm", ["", None, "   "])
    def test_empty(self, utm):
        assert resolve_campaign(utm, INDEX) is None

    def test_unknown_campaign_is_none(self):
        assert resolve_campaign("Instagram_Feed-CPO_UNKNOWN_2099", INDEX) is None

    def test_does_not_match_mid_word(self):
        """Имя должно начинаться после разделителя, а не с середины слова."""
        idx = build_campaign_index([{"campaign_id": "9", "campaign_name": "SALE_KSA"}])
        assert resolve_campaign("MEGASALE_KSA", idx) is None
        assert resolve_campaign("promo-SALE_KSA", idx) == "9"


class TestBridgeKeepsLabel:
    def test_unknown_keeps_original(self):
        """Неопознанную кампанию не теряем — это всё ещё осмысленный срез трафика."""
        assert bridge_metrika_campaign("Instagram_Feed-CPO_UNKNOWN", INDEX) \
            == "Instagram_Feed-CPO_UNKNOWN"

    def test_known_returns_id(self):
        assert bridge_metrika_campaign("Instagram_Feed-CPO_SUMMER_SALE_KSA", INDEX) \
            == "120249879465530017"

    def test_empty_stays_none(self):
        assert bridge_metrika_campaign("", INDEX) is None
