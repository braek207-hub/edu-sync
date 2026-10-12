# -*- coding: utf-8 -*-
"""probe_payments_design.py — факты под решение «как учитывать все оплаты».

Павел подтвердил (12.10.2026): orders=0 — доплата по той же сделке, выручку суммируем,
число оплат не растёт; оплаты без лида тоже надо учитывать. Перед правкой нужны три числа:

  1. У строк оплат ЕСТЬ ли «Дата создания» — если да, безлидовую оплату можно положить
     и на когортную ось, а не только в gross.
  2. Насколько доплата отстоит от основной оплаты (тот же день / месяц / позже) — от этого
     зависит, можно ли держать грань лида или нужна грань платежа.
  3. Полнота полей у безлидовых оплат: кампания, ленд, дата создания.

ENV: GOOGLE_SHEETS_ID + ключ сервис-аккаунта + DATABASE_URL. Только чтение.
"""
import collections
import os
from datetime import date

import psycopg2.extras

from sync.crm import crm_leads_sheets, crm_payments_sheets, find_orders_index, find_revenue_index
from sync.db import get_connection
from sync.sheets import get_sheets_service, read_sheet
from sync.utils import normalize_campaign_id, pick_index_loose, to_iso_date, to_num


def _cell(row, i):
    return "" if i == -1 or i >= len(row) else str(row[i] or "").strip()


def _d(iso):
    return date.fromisoformat(iso) if iso else None


def main() -> int:
    svc = get_sheets_service()
    sid = os.environ["GOOGLE_SHEETS_ID"]

    rows = []
    for sheet in crm_payments_sheets():
        values = read_sheet(svc, sid, sheet)
        if len(values) < 2:
            continue
        h = [str(x) for x in values[0]]
        # «Дата создания» в листе оплат встречается дважды (с временем и ISO) — берём обе.
        i_created = [i for i, x in enumerate(h) if "дата создания" in str(x or "").lower()]
        i_lead = pick_index_loose(h, ["id лида в scrm", "lead id", "id лида"])
        i_pay = pick_index_loose(h, ["date pay", "дата оплаты"])
        i_camp = pick_index_loose(h, ["кампания", "utm campaign", "utm_campaign"])
        i_land = pick_index_loose(h, ["ленд", "land"])
        i_ord, i_rev = find_orders_index(h), find_revenue_index(h)
        print(f"[{sheet}] колонки «Дата создания»: {[h[i] for i in i_created]}")
        for r in values[1:]:
            lid = normalize_campaign_id(_cell(r, i_lead))
            created = ""
            for i in i_created:
                created = created or to_iso_date(_cell(r, i))
            rows.append({
                "lead": lid,
                "pay": to_iso_date(_cell(r, i_pay)) if i_pay != -1 else "",
                "created": created,
                "cid": normalize_campaign_id(_cell(r, i_camp)) if i_camp != -1 else "",
                "land": _cell(r, i_land).lower() if i_land != -1 else "",
                "orders": round(to_num(_cell(r, i_ord))) if i_ord != -1 else 1,
                "rev": to_num(_cell(r, i_rev)) if i_rev != -1 else 0.0,
            })

    print(f"\nвсего строк оплат: {len(rows)}")
    filled = sum(1 for r in rows if r["created"])
    print(f"из них с заполненной «Датой создания»: {filled} ({filled * 100 // max(len(rows), 1)}%)")

    # --- 2. Доплаты: как далеко от основной оплаты ---
    main_pay = {}
    for r in rows:
        if r["orders"] == 1 and r["lead"] and r["pay"]:
            p = main_pay.get(r["lead"])
            if p is None or r["pay"] < p:
                main_pay[r["lead"]] = r["pay"]
    gap = collections.Counter()
    gap_money = collections.Counter()
    for r in rows:
        if r["orders"] == 1 or not r["lead"]:
            continue
        base = main_pay.get(r["lead"])
        if not base or not r["pay"]:
            gap["нет даты"] += 1
            gap_money["нет даты"] += r["rev"]
            continue
        dd = (_d(r["pay"]) - _d(base)).days
        k = ("тот же день" if dd == 0 else "в пределах месяца" if abs(dd) <= 31
             else "другой месяц" if abs(dd) <= 365 else "больше года")
        gap[k] += 1
        gap_money[k] += r["rev"]
    print("\n-- доплаты (orders≠1) относительно основной оплаты лида --")
    for k, n in gap.most_common():
        print(f"   {k:<20} {n:5d} строк / {gap_money[k]:12.0f}")

    # --- 3. Безлидовые оплаты: полнота полей ---
    lead_ids = set()
    for sheet in crm_leads_sheets():
        values = read_sheet(svc, sid, sheet)
        if len(values) < 2:
            continue
        h = [str(x) for x in values[0]]
        i_id = pick_index_loose(h, ["id"])
        for r in values[1:]:
            lid = normalize_campaign_id(_cell(r, i_id))
            if lid:
                lead_ids.add(lid)
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT lead_id FROM crm_lead_details")
        in_db = {r["lead_id"] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT campaign_id FROM direct_stats")
        direct = {r["campaign_id"] for r in cur.fetchall()}

    orph = [r for r in rows if r["lead"] and r["lead"] not in lead_ids and r["lead"] not in in_db]
    print(f"\n-- строки оплат без лида: {len(orph)}, выручка {sum(r['rev'] for r in orph):.0f} --")
    print(f"   с кампанией:        {sum(1 for r in orph if r['cid'])}")
    print(f"   кампания в Директе: {sum(1 for r in orph if r['cid'] in direct)}")
    print(f"   с лендом:           {sum(1 for r in orph if r['land'])}")
    print(f"   с датой создания:   {sum(1 for r in orph if r['created'])}")
    print(f"   с датой оплаты:     {sum(1 for r in orph if r['pay'])}")
    lag = [(_d(r["pay"]) - _d(r["created"])).days for r in orph if r["pay"] and r["created"]]
    if lag:
        lag.sort()
        print(f"   лаг создание→оплата, дней: мин {lag[0]}, медиана {lag[len(lag) // 2]}, макс {lag[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
