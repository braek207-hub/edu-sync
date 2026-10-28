# -*- coding: utf-8 -*-
"""Ставка НДС РФ для перевода между фактами расхода Директа и лимитами кабинета.

Факты расхода EDU — с НДС (Reports API, IncludeVAT=YES). Лимиты и цели кабинета
(WeeklySpendLimit, DailyBudget, AverageCpa) Campaigns.get отдаёт БЕЗ НДС: дока API
про них молчит, курс Яндекса (yard.yandex.ru/courses/direct-bazovyy/oplata-reklamy)
— «большинство денежных показателей в интерфейсе Директа указаны без учёта НДС»,
включая бюджеты и целевую цену конверсии. Сверка 24.10.2026 по базе Panda-BI:
кампании, упёршиеся в недельный лимит, за 12–18.10 потратили ровно лимит × 1,22
(708070153, 713806986, 714028940, 715072926 и др.).

22% — с 01.01.2026 (425-ФЗ), до этого 20%. Ставка берётся по дате дня расхода:
окно фактов через 01.01.2026 получает среднюю ставку своих дней.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Optional

VAT_22_FROM = date(2026, 1, 1)

_MSK = timezone(timedelta(hours=3))


def vat_rate(day: date) -> float:
    """Множитель НДС на календарный день расхода."""
    if isinstance(day, datetime) or not isinstance(day, date):
        raise TypeError(f"vat_rate: ожидалась date, пришло {day!r}")
    return 1.2 if day < VAT_22_FROM else 1.22


def window_vat_rate(end: date, days: int) -> float:
    """Средний множитель по `days` дням, заканчивающимся `end` включительно."""
    if days < 1:
        raise ValueError(f"window_vat_rate: окно {days} дн.")
    return sum(vat_rate(end - timedelta(days=i)) for i in range(days)) / days


def facts_vat_rate(today: Optional[date] = None, days: int = 28) -> float:
    """Ставка окна фактов агента: `days` дней до вчера по Москве.

    Окно агента — 28 зрелых дней; его точная граница зрелости отличается на
    считанные дни, а ставка внутри года постоянна, так что расхождение возможно
    только в окнах, задевающих 01.01, и не превышает долей процента.
    """
    if today is None:
        today = datetime.now(_MSK).date()
    return window_vat_rate(today - timedelta(days=1), days)
