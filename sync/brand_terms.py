# -*- coding: utf-8 -*-
"""Написания бренда LIME по регионам — единственный источник правды.

Термины нужны в двух местах и обязаны совпадать: серверный фильтр запроса
(GSC includingRegex / Вебмастер TEXT_CONTAINS) и питоновская проверка строк
после выгрузки. Раньше они разъезжались: API-фильтр можно было расширить, а
is_brand_query оставался на "lime"/"лайм" и выбрасывал всё остальное обратно.

Объёмы каждого написания замерены зондом на живых данных GSC за 8 недель —
см. docs/superpowers/specs/2026-07-18-lime-gcc-brand-traffic-design.md.
"""
import re

# База: латиница, кириллица, опечатки, слепая раскладка (печатают, не переключив язык).
# "lim" ловит lime / limé / limestore / саму опечатку "lim" (2 081 показ по ОАЭ).
BASE_TERMS = ["lim", "laim", "лайм", "лаим", "лиме", "дшьу", "kfqv"]

# Региональные добавки. Кириллица остаётся и в GCC: русскоязычные покупатели
# Залива дают треть кликов корневого домена по ОАЭ.
REGION_EXTRA = {
    "gcc": ["leem", "لايم", "ليم"],
}


def terms_for(region: str) -> list[str]:
    """Написания бренда для региона (база + локальная специфика)."""
    return BASE_TERMS + REGION_EXTRA.get(region, [])


def brand_regex(region: str) -> str:
    """Регексп для серверного фильтра GSC (RE2, includingRegex)."""
    return "(?i)(" + "|".join(re.escape(t) for t in terms_for(region)) + ")"


def is_brand_query(query: str, region: str = "ru") -> bool:
    """Брендовый ли запрос по написаниям региона."""
    s = (query or "").lower()
    return any(t.lower() in s for t in terms_for(region))
