"""Корзины из аудита RU 11.09.2026, которые лежали в Others без причины (500+ тыс. сессий
за 30 дней). Источник фактов — probe_lime_unlabeled.py и probe_lime_metrika_unlabeled.py."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.lime import classify


def test_direct_click_without_utm_is_outside_sem_but_named():
    # PROCONTEXT подписывает клики без UTM id рекламной системы Метрики + medium `ad`.
    # В SEM без campaign_id они падали бы в Display, а витрина их там не держит
    # (сверка W33 2026 с ручной книгой агентства, 15.09.2026) — вне SEM, но не безымянные.
    assert classify("ya_direct", "ad") == ("Others", "Яндекс.Директ (без UTM)")
    assert classify("google_adwords", "ad") == ("Others", "Google.Adwords (без UTM)")


def test_direct_install_attributed_app_sessions_stay_in_sem():
    # сессии приложения без UTM, источник = установка: у витрины они в SEM → App Display
    # (W33 2026: 516 = 65 + 452), отчёт обязан сходиться — в SEM без campaign_id
    assert classify("yandex.direct", "(not set)") == ("SEM", "Яндекс.Директ")
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
