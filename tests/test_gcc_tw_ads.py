# -*- coding: utf-8 -*-
"""Расход GCC по кампаниям из SQL-эндпоинта TW (П2)."""
import pytest

from sync.gcc_tw_ads import (
    ads_spend_rows,
    country_from_campaign_name,
    spend_metrics_covered,
)
from sync.gcc_triplewhale import SPEND_METRIC_MAP


class TestCountryFromCampaignName:
    @pytest.mark.parametrize("name,expected", [
        ("CPO_SUMMER_SALE_UAE", "ОАЭ"),
        ("CPO_SUMMER_SALE_KSA", "Саудовская Аравия"),
        ("CPO_SUMMER_SALE_QAT", "Катар"),
        ("CPO_SUMMER_SALE_KWT", "Кувейт"),
        ("CPO_SUMMER_SALE_OMN", "Оман"),
        ("CPO_SUMMER_SALE_VIDEO_UAE", "ОАЭ"),
    ])
    def test_reads_country_suffix(self, name, expected):
        assert country_from_campaign_name(name) == expected

    @pytest.mark.parametrize("name", [
        "CPO_Catalog_All – NEW", "CPO_NEW IN_W_27", "CPO_LINEN_W", "", None,
    ])
    def test_no_country_stays_none(self, name):
        # Кампании без страны в имени НЕ размазываем — они честно идут в тотал GCC.
        assert country_from_campaign_name(name) is None

    @pytest.mark.parametrize("name", ["CPO_SALE70_UAE", "SUMMER_SALE_GLOBAL", "PROMO_AED_ONLY"])
    def test_substrings_do_not_false_match(self, name):
        """«SA» не должна ловиться внутри SALE, «AE» — внутри AED."""
        got = country_from_campaign_name(name)
        assert got in (None, "ОАЭ")
        assert got != "Саудовская Аравия"


class TestAdsSpendRows:
    def _row(self, channel="facebook-ads", name="CPO_SUMMER_SALE_KSA", spend=100.0):
        return {"event_date": "2026-07-01", "channel": channel, "campaign_id": "120249879465530017",
                "campaign_name": name, "spend": spend, "currency": "USD"}

    def test_meta_maps_to_canon_subchannel(self):
        rows = ads_spend_rows([self._row()])
        assert len(rows) == 1
        assert rows[0]["channel"] == "SMM paid"
        assert rows[0]["subchannel"] == "Meta Ads"
        assert rows[0]["traffic_type"] == "Платный"
        assert rows[0]["country"] == "Саудовская Аравия"
        assert rows[0]["campaign_id"] == "120249879465530017"

    def test_google_is_skipped(self):
        """Google берём из кабинета — там гео измеренное. Здесь его быть не должно,
        иначе расход посчитается дважды."""
        assert ads_spend_rows([self._row(channel="google-ads")]) == []

    def test_unknown_channel_skipped(self):
        assert ads_spend_rows([self._row(channel="carrier-pigeon")]) == []

    def test_zero_spend_skipped(self):
        assert ads_spend_rows([self._row(spend=0)]) == []

    def test_currency_column_is_ignored(self):
        """`currency` = валюта биллинга кабинета, а НЕ единица spend (spend уже в AED).
        Если модуль начнёт её учитывать, Meta вырастет в 3.67 раза."""
        usd = ads_spend_rows([self._row(spend=100.0)])[0]["cost"]
        row_aed = {**self._row(spend=100.0), "currency": "AED"}
        assert usd == ads_spend_rows([row_aed])[0]["cost"] == 100.0


class TestNoDoubleCounting:
    def test_covered_metrics_exist_in_summary_map(self):
        """Каждая перекрытая метрика обязана существовать в SPEND_METRIC_MAP — иначе
        оркестратор «выбросит» несуществующий ключ и расход задвоится молча."""
        for metric in spend_metrics_covered():
            assert metric in SPEND_METRIC_MAP, metric

    def test_google_metric_not_covered(self):
        """ga_adCost выбрасывается своей веткой (кабинет), не этой."""
        assert "ga_adCost" not in spend_metrics_covered()
