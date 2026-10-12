# -*- coding: utf-8 -*-
"""probe_crm_pay_reconcile.py — сверка «что в таблице CRM» ↔ «что в витрине».

Повод (12.10.2026): оплаты и лиды в дашборде EDU расходятся с Google-таблицей —
оплата от 10.10 по провузу есть в листе, но Павел её не видит на экране.

Проба отвечает на три вопроса и ничего не пишет:
  1. Сколько строк листа «Оплаты» синк отбрасывает и по какой причине
     (нет id лида, orders≠1, нет даты оплаты, нет кампании).
  2. Какие оплаты листа не доехали до crm_lead_details (и почему: нет лида
     в листах «Лиды» — тогда приписывать оплату не к чему).
  3. Сходятся ли лиды и оплаты по дням за последние 45 дней.

ENV: GOOGLE_SHEETS_ID + ключ сервис-аккаунта + DATABASE_URL.
"""
import collections
import os
from datetime import date, timedelta

import psycopg2.extras

from sync.crm import crm_leads_sheets, crm_payments_sheets, find_orders_index, find_revenue_index
from sync.db import get_connection
from sync.sheets import get_sheets_service, read_sheet
from sync.utils import normalize_campaign_id, pick_index_loose, to_iso_date, to_num

WINDOW_DAYS = 45


def _cell(row, i):
    return "" if i == -1 or i >= len(row) else str(row[i] or "").strip()


def read_payments(svc, sid):
    """Строки листов оплат + причины, по которым синк их не возьмёт."""
    rows, reasons = [], collections.Counter()
    for sheet in crm_payments_sheets():
        values = read_sheet(svc, sid, sheet)
        if len(values) < 2:
            continue
        h = [str(x) for x in values[0]]
        i_lead = pick_index_loose(h, ["id лида в scrm", "lead id", "id лида"])
        i_pay = pick_index_loose(h, ["date pay", "дата оплаты"])
        i_camp = pick_index_loose(h, ["кампания", "utm campaign", "utm_campaign"])
        i_ord, i_rev = find_orders_index(h), find_revenue_index(h)
        print(f"[{sheet}] строк {len(values) - 1}; "
              f"lead={i_lead} pay_date={i_pay} campaign={i_camp} orders={i_ord} revenue={i_rev}")
        for r in values[1:]:
            lead_id = normalize_campaign_id(_cell(r, i_lead))
            pay_iso = to_iso_date(_cell(r, i_pay)) if i_pay != -1 else ""
            cid = normalize_campaign_id(_cell(r, i_camp)) if i_camp != -1 else ""
            orders = round(to_num(_cell(r, i_ord))) if i_ord != -1 else 1
            rev = to_num(_cell(r, i_rev)) if i_rev != -1 else 0.0
            if not any((lead_id, pay_iso, cid)):
                continue  # пустая строка листа
            if not lead_id:
                reasons["нет id лида → оплата не приписывается ни к какому лиду"] += 1
            if orders != 1:
                reasons[f"orders={orders} (не 1) → строка не считается оплатой"] += 1
            if not pay_iso:
                reasons["нет даты оплаты → не попадёт в crm_payments"] += 1
            if not cid:
                reasons["нет кампании → не попадёт в crm_payments"] += 1
            rows.append({"sheet": sheet, "lead_id": lead_id, "pay": pay_iso,
                         "cid": cid, "orders": orders, "revenue": rev})
    print("\n-- причины отбрасывания строк листа «Оплаты» --")
    for k, v in reasons.most_common():
        print(f"   {v:6d}  {k}")
    return rows


def read_lead_ids(svc, sid):
    """id лидов и их даты из листов «Лиды»."""
    ids = {}
    for sheet in crm_leads_sheets():
        values = read_sheet(svc, sid, sheet)
        if len(values) < 2:
            continue
        h = [str(x) for x in values[0]]
        i_id = pick_index_loose(h, ["id"])
        i_date = pick_index_loose(h, ["дата создания", "date created", "дата", "date"])
        n = 0
        for r in values[1:]:
            lid = normalize_campaign_id(_cell(r, i_id))
            if not lid:
                continue
            ids.setdefault(lid, to_iso_date(_cell(r, i_date)))
            n += 1
        print(f"[{sheet}] строк с id: {n}")
    print(f"уникальных id лидов в листах: {len(ids)}")
    return ids


def main() -> int:
    svc = get_sheets_service()
    sid = os.environ["GOOGLE_SHEETS_ID"]
    pay_rows = read_payments(svc, sid)
    lead_ids = read_lead_ids(svc, sid)

    since = (date.today() - timedelta(days=WINDOW_DAYS)).isoformat()
    paid_sheet = {}
    for r in pay_rows:
        if r["orders"] != 1 or not r["lead_id"]:
            continue
        agg = paid_sheet.setdefault(r["lead_id"], {"pay": r["pay"], "rev": 0.0})
        agg["rev"] += r["revenue"]
        if r["pay"] and (not agg["pay"] or r["pay"] < agg["pay"]):
            agg["pay"] = r["pay"]

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT lead_id, payment_date::text pay, amount FROM crm_lead_details WHERE is_paid")
        paid_db = {r["lead_id"]: r for r in cur.fetchall()}

        print(f"\n-- оплаты --\nлист (orders=1, есть id): {len(paid_sheet)} лидов; "
              f"база (is_paid): {len(paid_db)} лидов")

        miss = [lid for lid in paid_sheet if lid not in paid_db]
        extra = [lid for lid in paid_db if lid not in paid_sheet]
        no_lead_row = [lid for lid in miss if lid not in lead_ids]
        print(f"в листе есть, в базе нет: {len(miss)} "
              f"(из них {len(no_lead_row)} — нет строки лида в листах «Лиды»)")
        for lid in sorted(miss, key=lambda x: paid_sheet[x]["pay"] or "", reverse=True)[:25]:
            s = paid_sheet[lid]
            print(f"   lead {lid}: оплата {s['pay'] or '—'}, выручка {s['rev']:.0f}, "
                  f"лид в листе: {'нет' if lid not in lead_ids else lead_ids[lid]}")
        print(f"в базе есть, в листе нет: {len(extra)}")
        for lid in extra[:10]:
            print(f"   lead {lid}: база payment_date={paid_db[lid]['pay']}")

        # Расхождение дат оплаты
        diff_dates = [(lid, paid_sheet[lid]["pay"], paid_db[lid]["pay"])
                      for lid in paid_sheet if lid in paid_db
                      and paid_sheet[lid]["pay"] and paid_db[lid]["pay"]
                      and paid_sheet[lid]["pay"] != paid_db[lid]["pay"]]
        print(f"дата оплаты расходится: {len(diff_dates)}")
        for lid, a, b in diff_dates[:10]:
            print(f"   lead {lid}: лист {a} ≠ база {b}")

        # Лиды по дням
        cur.execute(
            "SELECT created_date::text d, count(*) n FROM crm_lead_details "
            "WHERE created_date >= %s GROUP BY 1 ORDER BY 1", (since,))
        db_by_day = {r["d"]: r["n"] for r in cur.fetchall()}

    sheet_by_day = collections.Counter(
        d for lid, d in lead_ids.items() if d and d >= since)
    print(f"\n-- лиды по дням с {since} (лист ↔ база) --")
    for d in sorted(set(sheet_by_day) | set(db_by_day)):
        s, b = sheet_by_day.get(d, 0), db_by_day.get(d, 0)
        mark = "" if s == b else "   <-- расхождение"
        print(f"   {d}  лист {s:5d}  база {b:5d}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
