# -*- coding: utf-8 -*-
"""Маппер каналов Метрики живёт в нейтральном модуле и переиспользуется KZ и GCC."""
from sync.metrika_channels import map_metrika_channel


def test_paid_sources():
    assert map_metrika_channel("ad", "Google Ads") == ("SEM", "Google.Adwords", "Платный")
    assert map_metrika_channel("ad", "Yandex: Direct") == ("SEM", "Яндекс.Директ", "Платный")
    assert map_metrika_channel("ad", "Instagram") == ("SMM paid", "Meta Ads", "Платный")


def test_free_sources():
    assert map_metrika_channel("organic", "Google") == ("SEO", "SEO Google", "Бесплатный")
    assert map_metrika_channel("direct", None) == ("Direct", "Direct", "Бесплатный")
    assert map_metrika_channel("internal", None) == ("Internal", "Internal", "Бесплатный")
    assert map_metrika_channel("email", None) == ("CRM", "Email", "Бесплатный")


def test_unknown_falls_back_to_others():
    assert map_metrika_channel(None, None) == ("Others", "Unknown", "Бесплатный")


def test_gcc_module_still_exports_it():
    """Реэкспорт: sync/lime_gcc.py и его тесты не должны сломаться."""
    from sync.gcc_channels import map_metrika_channel as gcc_version
    assert gcc_version is map_metrika_channel
