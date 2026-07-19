# -*- coding: utf-8 -*-
"""Таксономия GCC/kz_metrika обязана совпадать с каноном витрины (sync.lime.classify).

Регрессия 2026-07-19: витрина писала «SEO Google», Метрика — «SEO Google, search results»;
витрина «Реферал», Метрика «Referral». Один канал в разных регионах назывался по-разному,
и при группировке через все регионы распадался на две строки. Плюс TW сваливал органику
и соцсети в один подканал «Organic & Social»: SMM (organic) стоял с нулём заказов
при живом трафике.
"""
import pytest

from sync.gcc_channels import map_tw_source, split_organic_and_social
from sync.lime import classify
from sync.metrika_channels import map_metrika_channel


class TestMetrikaMatchesCanon:
    @pytest.mark.parametrize("engine", [
        "Google, search results", "Google: mobile app", "Google",
    ])
    def test_google_variants_collapse_to_canon(self, engine):
        assert map_metrika_channel("organic", engine)[:2] == classify("google", "organic")

    @pytest.mark.parametrize("engine", [
        "Yandex, search results", "Yandex Mobile", "Yandex Smart Camera", "Yandex.Images",
    ])
    def test_yandex_variants_collapse_to_canon(self, engine):
        assert map_metrika_channel("organic", engine)[:2] == classify("yandex", "organic")

    @pytest.mark.parametrize("engine", ["Bing, search results", "DuckDuckGo", "Ecosia", None])
    def test_other_engines_are_seo_others(self, engine):
        assert map_metrika_channel("organic", engine)[:2] == ("SEO", "SEO Others")

    def test_referral_without_engine_uses_canon_name(self):
        # Канон витрины: medium=referral → «Реферал». Было «Referral».
        assert map_metrika_channel("referral", None)[:2] == classify("somewhere", "referral")

    def test_referral_keeps_domain_when_known(self):
        assert map_metrika_channel("referral", "chatgpt.com")[:2] == ("Referrals", "chatgpt.com")

    def test_social_unknown_is_others_like_canon(self):
        # Было «Social» — имени, которого канон не знает.
        assert map_metrika_channel("social", None)[:2] == ("SMM (organic)", "Others")

    def test_social_known_network_capitalized(self):
        assert map_metrika_channel("social", "Instagram")[:2] == classify("instagram", "social")

    def test_mindbox_mail_separated_from_plain_email(self):
        assert map_metrika_channel("email", None, "mindbox_bcat")[:2] == ("CRM", "Mindbox")
        assert map_metrika_channel("email", None, None)[:2] == ("CRM", "Email")

    def test_paid_platforms_unchanged(self):
        assert map_metrika_channel("ad", "Google Ads") == ("SEM", "Google.Adwords", "Платный")
        assert map_metrika_channel("ad", "Instagram") == ("SMM paid", "Meta Ads", "Платный")
        assert map_metrika_channel("ad", None)[2] == "Платный"


class TestOrganicAndSocialSplit:
    @pytest.mark.parametrize("referrer,expected", [
        ("google", ("SEO", "SEO Google")),
        ("yandex.ru", ("SEO", "SEO Yandex")),
        ("bing.com", ("SEO", "SEO Others")),
        ("instagram", ("SMM (organic)", "Instagram")),
        ("facebook.com", ("SMM (organic)", "Facebook")),
        ("limeshop-uae.maestra.io", ("CRM", "Mindbox")),
    ])
    def test_referrer_decides_channel(self, referrer, expected):
        assert split_organic_and_social(referrer)[:2] == expected

    @pytest.mark.parametrize("referrer", [
        "limestore.com", "sa.limestore.com", "lime-shop.com",
    ])
    def test_own_domains_are_internal(self, referrer):
        # Переход со своей витрины на свою — внутренний трафик, не органика.
        assert split_organic_and_social(referrer)[:2] == ("Internal", "Internal")

    def test_unknown_referrer_falls_back_to_canon_referral(self):
        assert split_organic_and_social("shop.app")[:2] == ("Referrals", "Реферал")

    def test_empty_referrer_stays_visible_not_guessed(self):
        # Расщепить нечем — честнее отдельная корзина, чем приписать SEO.
        assert split_organic_and_social("")[:2] == ("Others", "Organic & Social")

    def test_map_tw_source_routes_referrer_through(self):
        assert map_tw_source("organic_and_social", "instagram")[:2] == ("SMM (organic)", "Instagram")

    def test_ad_platforms_ignore_referrer_arg(self):
        # У платных источников campaignId — настоящий id кампании, не реферер.
        assert map_tw_source("google-ads", "21067876545")[:2] == ("SEM", "Google.Adwords")


class TestTwMeetsMetrika:
    """Ключевая проверка П3: заказы TW и визиты Метрики обязаны встать в ОДИН подканал."""

    @pytest.mark.parametrize("metrika_engine,tw_referrer", [
        ("Google, search results", "google"),
        ("Yandex, search results", "yandex.ru"),
        ("Instagram", "instagram"),
    ])
    def test_same_subchannel_from_both_sources(self, metrika_engine, tw_referrer):
        source_id = "social" if tw_referrer == "instagram" else "organic"
        traffic = map_metrika_channel(source_id, metrika_engine)[:2]
        orders = split_organic_and_social(tw_referrer)[:2]
        assert traffic == orders, f"визиты {traffic} и заказы {orders} не встретятся"


class TestCrmByUtmTag:
    """Метка utm достовернее эвристики Метрики (замер 2026-07-19).

    Почтовый клиент срезает реферер, и Метрика пишет половину кликов из триггерных
    писем в Direct: mindbox_bv — 63 визита Mailing и 61 Direct, mindbox_bcat — 40 и 57.
    Метка utm_source при этом стоит. Без приоритета метки заказы CRM не встречались
    со своими визитами: сходилось 53% CRM-заказов.
    """

    @pytest.mark.parametrize("traffic_source", ["direct", "email", "ad", "referral", None])
    def test_mindbox_tag_wins_over_metrika_guess(self, traffic_source):
        assert map_metrika_channel(traffic_source, None, "mindbox_bv")[:2] == ("CRM", "Mindbox")

    @pytest.mark.parametrize("utm", [
        "manual_mindbox", "mindbox_bcat", "mindbox_pd_view", "mindbox_welcome",
        "limeshop-uae.maestra.io",
    ])
    def test_all_mindbox_flavours(self, utm):
        assert map_metrika_channel("direct", None, utm)[:2] == ("CRM", "Mindbox")

    def test_klaviyo_is_plain_email(self):
        assert map_metrika_channel("direct", None, "klaviyo")[:2] == ("CRM", "Email")

    def test_crm_traffic_is_free(self):
        assert map_metrika_channel("ad", "Google Ads", "mindbox_bk")[2] == "Бесплатный"

    def test_plain_direct_untouched(self):
        """Обычный direct без метки остаётся Direct — ветка не должна ловить лишнее."""
        assert map_metrika_channel("direct", None, None)[:2] == ("Direct", "Direct")
        assert map_metrika_channel("direct", None, "google")[:2] == ("Direct", "Direct")

    def test_real_ads_untouched(self):
        assert map_metrika_channel("ad", "Google Ads", "google")[:2] == ("SEM", "Google.Adwords")
