# -*- coding: utf-8 -*-
"""Когорта разделов LIME (фаза 2 unified-mart): учёт покупок по дате привлечения.

Стейт lime_cohort_state: «клиент → последний платный клик (дата, кампания) +
дата первой покупки после него». Покупка дня приписывается клику, если он не
старше COHORT_WINDOW_DAYS. Витрина lime_section_cohort_daily — матрица
«день клика × день покупки»: перезапись по purchase_day идемпотентна, прошлые
click-дни дозревают новыми purchase_day-строками без апдейта старых.

Идемпотентность повторного прогона дня D:
  · клики дня D апдейтят стейт только когда click_date в стейте < D
    (повтор того же дня не сбрасывает first_buy_date);
  · «первая покупка» = first_buy_date IS NULL ИЛИ = D (повтор дня считает
    те же buyers, а не ноль).

Хронология обязательна: дни прогоняются по возрастанию (стейт последователен).
"""
from collections import defaultdict
from datetime import date, timedelta

import psycopg2.extras

from sync.db import get_connection

COHORT_WINDOW_DAYS = 30
STATE_RETENTION_DAYS = 45


def attribute_day(day: str, clicks: dict, orders: list, state: dict):
    """Чистая атрибуция дня. Ничего не знает про БД.

    clicks: {cid: (campaign, channel)} — платные клики дня D
    orders: [(cid, {section: [items, revenue]})] — заказы дня D (по одному элементу на заказ)
    state:  {cid: (click_date iso, campaign, channel, first_buy_date iso|None)} —
            стейт ДО дня D (без кликов D)

    Возвращает (cells, click_rows, first_buy_cids):
      cells: {(click_day, campaign, channel, section): [buyers, orders, items, revenue]}
      click_rows: {(campaign, channel): clicks_users}
      first_buy_cids: клиенты, которым надо проставить first_buy_date = D
    """
    d = date.fromisoformat(day)
    # Клики дня D входят в стейт до атрибуции покупок: same-day покупка = когорта d0.
    merged = dict(state)
    for cid, (camp, ch) in clicks.items():
        prev = merged.get(cid)
        if prev is None or prev[0] < day:
            merged[cid] = (day, camp, ch, None)
        # prev[0] == day: повторный прогон — first_buy_date не сбрасываем

    cells = defaultdict(lambda: [0, 0, 0.0, 0.0])
    first_buy = set()
    for cid, by_sec in orders:
        st = merged.get(cid)
        if st is None:
            continue
        click_day, camp, ch, fbd = st
        age = (d - date.fromisoformat(click_day)).days
        if age < 0 or age > COHORT_WINDOW_DAYS:
            continue
        # fbd == day покрывает повторный прогон дня (в БД уже проставлено D);
        # cid not in first_buy отсекает вторую покупку того же дня в ЭТОМ прогоне.
        is_first = (fbd is None or fbd == day) and cid not in first_buy
        for sec, (items, revenue) in by_sec.items():
            cell = cells[(click_day, camp, ch, sec)]
            if is_first:
                cell[0] += 1
            cell[1] += 1
            cell[2] += items
            cell[3] += revenue
        if is_first:
            first_buy.add(cid)

    click_rows = defaultdict(int)
    for camp, ch in clicks.values():
        click_rows[(camp, ch)] += 1
    return dict(cells), dict(click_rows), first_buy


def sync_cohort_web(day: str, cohort_input: dict) -> None:
    """Шаг когорты после веб-дня: стейт + витрина + клики + retention."""
    clicks = cohort_input.get("clicks") or {}
    orders = cohort_input.get("orders") or []
    buyer_ids = sorted({str(cid) for cid, _ in orders})

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Стейт нужен только по покупателям дня (клики дня вливаются в памяти).
            state = {}
            if buyer_ids:
                cur.execute(
                    "select client_id, click_date, campaign, channel, first_buy_date "
                    "from lime_cohort_state where platform = 'web' and client_id = any(%s)",
                    (buyer_ids,),
                )
                for cid, cd, camp, ch, fbd in cur.fetchall():
                    state[int(cid)] = (cd.isoformat(), camp, ch, fbd.isoformat() if fbd else None)

            cells, click_rows, first_buy = attribute_day(day, clicks, orders, state)

            # 1. Клики дня → стейт (новый клик закрывает прошлое окно).
            if clicks:
                rows = [(str(cid), day, camp, ch) for cid, (camp, ch) in clicks.items()]
                psycopg2.extras.execute_batch(cur, """
                    insert into lime_cohort_state (platform, client_id, click_date, campaign, channel, first_buy_date)
                    values ('web', %s, %s, %s, %s, null)
                    on conflict (platform, client_id) do update
                      set click_date = excluded.click_date, campaign = excluded.campaign,
                          channel = excluded.channel, first_buy_date = null,
                          updated_at = now()
                      where lime_cohort_state.click_date < excluded.click_date
                """, rows, page_size=500)

            # 2. Первая покупка окна — отметить в стейте.
            if first_buy:
                cur.execute(
                    "update lime_cohort_state set first_buy_date = %s, updated_at = now() "
                    "where platform = 'web' and client_id = any(%s) and first_buy_date is null",
                    (day, [str(c) for c in sorted(first_buy)]),
                )

            # 3. Витрина: перезапись дня покупки.
            cur.execute(
                "delete from lime_section_cohort_daily where platform = 'web' and purchase_day = %s",
                (day,),
            )
            if cells:
                rows = [
                    (cd, day, camp, ch, sec, b, o, round(it, 2), round(rev, 2))
                    for (cd, camp, ch, sec), (b, o, it, rev) in sorted(cells.items())
                ]
                psycopg2.extras.execute_batch(cur, """
                    insert into lime_section_cohort_daily
                      (click_day, purchase_day, platform, campaign, channel, section, buyers, orders, items, revenue)
                    values (%s, %s, 'web', %s, %s, %s, %s, %s, %s, %s)
                """, rows, page_size=500)

            # 4. Клики дня (знаменатель CR): перезапись дня клика.
            cur.execute(
                "delete from lime_cohort_click_daily where platform = 'web' and click_day = %s",
                (day,),
            )
            if click_rows:
                psycopg2.extras.execute_batch(cur, """
                    insert into lime_cohort_click_daily (click_day, platform, campaign, channel, clicks_users)
                    values (%s, 'web', %s, %s, %s)
                """, [(day, camp, ch, n) for (camp, ch), n in sorted(click_rows.items())], page_size=500)

            # 5. Retention стейта: окно закрыто + зазор — строка больше не нужна.
            horizon = (date.fromisoformat(day) - timedelta(days=STATE_RETENTION_DAYS)).isoformat()
            cur.execute("delete from lime_cohort_state where click_date < %s", (horizon,))
        conn.commit()
    print(f"  когорта {day}: кликов {sum(click_rows.values()):,}, ячеек {len(cells)}, "
          f"первых покупок {len(first_buy)}")
