# -*- coding: utf-8 -*-
"""Страж методики: наш gross против витрины PROCONTEXT, ежедневно.

Методика вкладки «Категории» обещает охват ≥97% выручки магазина (сверено при
первичной заливке копейка в копейку по покупкам из логов). Если словарь каналов,
парсер логов или фид «поплывут» — расхождение вырастет молча. Страж считает
отношение нашей дневной выручки/заказов (lime_section_daily, web) к витрине
(lime_stats, region=ru, data_source=web) и валит джобу, когда выходит за створ.

Створ по умолчанию: покрытие 90–103%. Нижняя граница мягче методических 97%,
потому что витрина получает поздние правки заказов (возвраты/отмены доезжают
днями), а свежий день всегда неполный с их стороны или нашей.
"""
import os

from sync.db import get_connection

DEFAULT_MIN, DEFAULT_MAX = 0.90, 1.03


def verify_web_window(frm: str, to: str) -> bool:
    """True — сверка в створе; False — методика поплыла (джобу надо валить)."""
    lo = float(os.environ.get("LIME_SECTIONS_VERIFY_MIN", DEFAULT_MIN))
    hi = float(os.environ.get("LIME_SECTIONS_VERIFY_MAX", DEFAULT_MAX))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                with ours as (
                  select day, sum(revenue) rev, sum(orders) ord
                  from lime_section_daily
                  where platform = 'web' and day between %s and %s
                  group by day
                ), vitrina as (
                  select date as day, sum(purchases_revenue) rev, sum(purchases_count) ord
                  from lime_stats
                  where region = 'ru' and data_source = 'web' and date between %s and %s
                  group by date
                )
                select v.day, v.rev, o.rev, v.ord, o.ord
                from vitrina v left join ours o using (day)
                order by v.day
                """,
                (frm, to, frm, to),
            )
            rows = cur.fetchall()
    if not rows:
        print("  страж сверки: витрина пуста в окне — пропуск (нечего сверять)")
        return True
    ok = True
    print(f"  страж сверки gross↔витрина (створ {lo:.0%}–{hi:.0%}):")
    for day, v_rev, o_rev, v_ord, o_ord in rows:
        v_rev, o_rev = float(v_rev or 0), float(o_rev or 0)
        v_ord, o_ord = float(v_ord or 0), float(o_ord or 0)
        r_rev = o_rev / v_rev if v_rev else 0
        r_ord = o_ord / v_ord if v_ord else 0
        good = lo <= r_rev <= hi
        # Заказы — справочно: наши дробные доли на источниках дают шум больше денег.
        print(f"    {day}: выручка {o_rev:,.0f} / {v_rev:,.0f} = {r_rev:.1%}"
              f"  заказы {o_ord:,.0f}/{v_ord:,.0f} = {r_ord:.1%}"
              f"  {'OK' if good else '!! ВНЕ СТВОРА'}")
        if not good:
            ok = False
    if not ok:
        print("  страж сверки: расхождение с витриной вне створа — проверь словарь "
              "каналов, парсер визитов и фид (методика могла поплыть)")
    return ok
