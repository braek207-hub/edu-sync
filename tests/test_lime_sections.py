# -*- coding: utf-8 -*-
"""Чистые функции витрин разделов LIME: словари каналов и корзин.

Сетевые конвейеры (Logs API, AppMetrica) юнитами не покрываются — их сверяет
check-режим синка (LIME_SECTIONS_CHECK=1) против уже залитой истории.
"""
from sync.lime_sections_common import (
    CH_IDX, CHANNEL_PRIORITY, SRC_PRIORITY, WEB_BUCKET, app_campaign_of, bucket,
    channel_of, pub_norm,
)


def test_channel_of_direct_click_wins():
    # Номер заказа Директа — самый сильный признак, перебивает любые метки.
    assert channel_of("organic", "google", "cpc", "12345") == "SEM"


def test_channel_of_sem_by_utm():
    assert channel_of("ad", "yandex", "cpc", "") == "SEM"
    assert channel_of("ad", "google_ads", "cpm", "") == "SEM"


def test_channel_of_smm_paid_vs_organic():
    # Платная соцсеть — по связке source+medium; та же соцсеть без cpc — органика.
    assert channel_of("social", "instagram", "cpc", "") == "SMM paid"
    assert channel_of("social", "instagram", "", "") == "SMM (organic)"
    assert channel_of("social", "vk", "cpm", "") == "SMM paid"


def test_channel_of_crm_and_retargeting():
    assert channel_of("ad", "manual_mindbox", "banner", "") == "CRM"
    assert channel_of("ad", "soloway", "cpm", "") == "Retargeting"
    assert channel_of("ad", "email", "", "") == "CRM"


def test_channel_of_traffic_fallbacks():
    assert channel_of("organic", "", "", "") == "SEO"
    assert channel_of("direct", "", "", "") == "Direct"
    assert channel_of("referral", "", "", "") == "Referrals"
    assert channel_of("ad", "someban", "", "") == "SEM"
    assert channel_of("", "", "", "") == "Others"


def test_channel_priority_covers_all_channels():
    # Каждый канал словаря обязан иметь приоритет: иначе KeyError на живом визите.
    produced = {
        channel_of(t, s, m, d)
        for t, s, m, d in [
            ("organic", "", "", ""), ("direct", "", "", ""), ("social", "", "", ""),
            ("referral", "", "", ""), ("ad", "", "", ""), ("", "", "", ""),
            ("ad", "yandex", "cpc", ""), ("ad", "vk", "cpc", ""), ("ad", "soloway", "", ""),
            ("ad", "mindbox", "", ""), ("social", "instagram", "", ""), ("x", "", "", "1"),
        ]
    }
    assert produced <= set(CHANNEL_PRIORITY)
    assert set(CH_IDX) == set(CHANNEL_PRIORITY)


def test_web_bucket_priorities_complete():
    # Все корзины словаря имеют приоритет; неизвестный трафик падает в unknown.
    assert set(WEB_BUCKET.values()) <= set(SRC_PRIORITY)
    assert "unknown" in SRC_PRIORITY


def test_app_bucket():
    assert bucket("") == "organic"                    # пустой паблишер = органика
    assert bucket("Yandex Search") == "organic"
    assert bucket("Yandex.Direct") == "paid"
    assert bucket("Mindbox") == "owned"
    assert bucket("Какой-то новый партнёр") == "other"


def test_pub_norm():
    assert pub_norm("") == "Без атрибуции"
    assert pub_norm("  VK Ads (ex. myTarget) ") == "VK Ads (ex. myTarget)"


def test_app_campaign_of():
    # Автотрекинг Директа: имя трекера = ID кампании.
    assert app_campaign_of("Yandex.Direct Auto-Tracking", "118498117") == "direct:118498117"
    # Ручные трекеры и другие паблишеры кампаний не несут.
    assert app_campaign_of("Yandex.Direct", "118498117") is None
    assert app_campaign_of("Yandex.Direct Auto-Tracking", "Тест роута") is None
    assert app_campaign_of("Mindbox", "Mindbox_catalog") is None
    assert app_campaign_of("", "") is None
