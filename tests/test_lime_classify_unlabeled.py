"""Корзины из аудита RU 11.09.2026, которые лежали в Others без причины (500+ тыс. сессий
за 30 дней). Источник фактов — probe_lime_unlabeled.py и probe_lime_metrika_unlabeled.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.lime import classify


def test_direct_click_without_utm_is_direct_channel():
    # PROCONTEXT подписывает клики без UTM id рекламной системы Метрики + medium `ad`
    assert classify("ya_direct", "ad") == ("SEM", "Яндекс.Директ")
    assert classify("google_adwords", "ad") == ("SEM", "Google.Adwords")


def test_direct_install_attributed_app_sessions_are_not_paid():
    # сессии приложения без UTM, источник = установка; расхода нет — как у VK, в органике
    assert classify("yandex.direct", "(not set)") == ("Direct", "Yandex.Direct (установки)")
    assert classify("ya.direct", "cpc") == ("SEM", "Яндекс.Директ")
    assert classify("yandex.direct", "cpc") == ("SEM", "Яндекс.Директ")


def test_app_direct_opens_are_direct():
    assert classify("direct", "none") == ("Direct", "Direct")
    assert classify("(not+set)", "(not+set)") == ("Direct", "Direct")
    assert classify("(not set)", "(not set)") == ("Direct", "Direct")


def test_broken_tracker_macros_stay_visible():
    # незаполненные макросы трекера — чинится у источника, в отчёте не прячем
    assert classify("{utm_source}", "{utm_medium}") == ("Others", "{utm_source}")


def test_named_source_with_medium_none_is_not_direct():
    assert classify("somesite.ru", "none") == ("Others", "somesite.ru")
