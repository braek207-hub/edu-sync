# -*- coding: utf-8 -*-
"""Спам-площадки: слишком хорошие, чтобы быть честными.

Автостратегия учится на цели «Спасибо», и площадка, которая достигает её
в разы чаще кабинета, кормит стратегию пустыми заявками — Директ считает
их успехом и льёт туда бюджет (память edu-spam-leads-autostrategy-goal).
Словарь по имени такую площадку не видит: umkaplay.ru выглядит как обычный
сайт. Отличает её только статистика.

Пример Павла 14.09.2026, кабинет vsekolledzhi: umkaplay.ru — 93 клика,
12 достижений, CR 12,9 % при 3,36 % по кабинету; за неделю на двух
кампаниях 131 клик и 18 достижений, ни одной оплаты.
"""

from typing import Any, Dict, Iterable, List, Tuple

from sync.placements.classify import ALLOW_EXACT, ALLOW_PREFIX, normalize
from sync.placements.llm import is_protected

# Меньше — шум: три заявки с двух кликов дают CR 150 % на пустом месте.
MIN_CONVERSIONS = 5
# Абсолютный порог: ниже него «высокой» конверсия не бывает ни у кого.
MIN_CR = 0.08
# И относительный: площадка обязана быть в разы лучше своего кабинета —
# честная площадка от кабинета отличается процентами, спамовая — кратно.
CR_RATIO = 2.5


def _own_conversions(row: Dict[str, Any], goals: Dict[str, int]) -> int:
    """Достижения по цели стратегии кампании; без цели — самая массовая.

    Максимум, а не сумма: цели Директа дублируют друг друга (одна заявка под
    двумя идентификаторами), сумма считала бы её дважды.
    """
    conv = row.get("conversions") or {}
    own = goals.get(str(row.get("campaign_id")))
    if own in conv:
        return int(conv[own])
    return int(max(conv.values())) if conv else 0


def suspicious_sites(rows: Iterable[Dict[str, Any]],
                     goals: Dict[str, int],
                     min_conversions: int = MIN_CONVERSIONS,
                     min_cr: float = MIN_CR,
                     cr_ratio: float = CR_RATIO) -> Dict[str, str]:
    """Строки недельного отчёта → площадка → причина запрета.

    Считается по площадке целиком, поверх всех кампаний логина: спам-сайт
    спамит везде, и по одной кампании его иногда не видно.

    Известные приложения и крупные порталы правило не трогает: ВК за неделю
    даёт шесть заявок с 55 кликов, и решать, спам это или нет, — человеку.
    """
    total_clicks = 0
    total_conv = 0
    per_site: Dict[str, List[int]] = {}
    for row in rows:
        site = normalize(row.get("placement"))
        if not site:
            continue
        clicks = int(row.get("clicks") or 0)
        conv = _own_conversions(row, goals)
        total_clicks += clicks
        total_conv += conv
        slot = per_site.setdefault(site, [0, 0])
        slot[0] += clicks
        slot[1] += conv
    if total_clicks <= 0:
        return {}
    account_cr = total_conv / float(total_clicks)
    threshold = max(min_cr, account_cr * cr_ratio)

    out: Dict[str, str] = {}
    for site, (clicks, conv) in per_site.items():
        if conv < min_conversions or clicks <= 0:
            continue
        cr = conv / float(clicks)
        if cr < threshold:
            continue
        if site in ALLOW_EXACT or is_protected(site) \
                or any(site.startswith(p) for p in ALLOW_PREFIX):
            continue
        out[site] = ("спам-площадка: %d достижений с %d кликов, CR %.1f%% "
                     "при %.1f%% по кабинету"
                     % (conv, clicks, 100 * cr, 100 * account_cr))
    return out
