# -*- coding: utf-8 -*-
"""probe_crm_pay_reconcile2.py — вторая часть сверки: суммы, а не только наличие.

Первая проба (probe_crm_pay_reconcile.py) показала, что состав оплаченных лидов
сходится (не доехали 60 исторических). Остался класс дефектов, который сверка по
составу не видит: ВЫРУЧКА. У лида в листе может быть несколько строк оплаты
(доплаты, рассрочка) — если синк берёт одну, сумма на дашборде ниже правды.

Проба отвечает:
  1. Сколько лидов имеют больше одной строки оплаты и как это влияет на сумму.
  2. Где расходится amount: лист ↔ crm_lead_details, по лидам и в сумме.
  3. Выручка по месяцам платежа: лист ↔ база (витрина дашборда).
  4. Сколько выручки потеряно на 60 неприехавших оплатах, по месяцам.

Только чтение. ENV: GOOGLE_SHEETS_ID + ключ сервис-аккаунта + DATABASE_URL.
"""
import collections
import os

import psycopg2.extras

from sync.crm import crm_leads_sheets, crm_payments_sheets, find_orders_index, find_revenue_index
from sync.db import get_connection
from sync.sheets import get_sheets_service, read_sheet
from sync.utils import normalize_campaign_id, pick_index_loose, to_iso_date, to_num


def _cell(row, i):
    return "" if i == -1 or i >= len(row) else str(row[i] or "").strip()


def main() -> int:
    svc = get_sheets_service()
    sid = os.environ["GOOGLE_SHEETS_ID"]

    # --- лист: все строки оплат (orders=1, есть id лида) ---
    rows_by_lead = collections.defaultdict(list)
    for sheet in crm_payments_sheets():
        values = read_sheet(svc, sid, sheet)
        if len(values) < 2:
            continue
        h = [str(x) for x in values[0]]
        i_lead = pick_index_loose(h, ["id лида в scrm", "lead id", "id лида"])
        i_pay = pick_index_loose(h, ["date pay", "дата оплаты"])
        i_ord, i_rev = find_orders_index(h), find_revenue_index(h)
        for r in values[1:]:
            lid = normalize_campaign_id(_cell(r, i_lead))
            if not lid or (i_ord != -1 and round(to_num(_cell(r, i_ord))) != 1):
                continue
            rows_by_lead[lid].append({
                "pay": to_iso_date(_cell(r, i_pay)) if i_pay != -1 else "",
                "rev": to_num(_cell(r, i_rev)) if i_rev != -1 else 0.0,
            })

    multi = {lid: rs for lid, rs in rows_by_lead.items() if len(rs) > 1}
    print(f"лидов с оплатами в листе: {len(rows_by_lead)}; из них с >1 строкой оплаты: {len(multi)}")
    extra_money = sum(sum(r["rev"] for r in rs[1:]) for rs in multi.values())
    print(f"выручка во «вторых и далее» строках таких лидов: {extra_money:.0f}")
    for lid, rs in list(multi.items())[:10]:
        print(f"   lead {lid}: " + ", ".join(f"{r['pay'] or '—'}/{r['rev']:.0f}" for r in rs))

    # --- лист: агрегат на лида (как ждёт витрина) ---
    sheet_agg = {}
    for lid, rs in rows_by_lead.items():
        pays = sorted(r["pay"] for r in rs if r["pay"])
        sheet_agg[lid] = {"pay": pays[0] if pays else "", "rev": sum(r["rev"] for r in rs)}

    # --- есть ли строка лида в листах «Лиды» ---
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
        cur.execute("SELECT lead_id, payment_date::text pay, amount FROM crm_lead_details WHERE is_paid")
        db = {r["lead_id"]: {"pay": r["pay"], "rev": float(r["amount"] or 0)} for r in cur.fetchall()}

    # --- расхождение сумм по лидам, которые доехали ---
    diff = [(lid, sheet_agg[lid]["rev"], db[lid]["rev"], sheet_agg[lid]["pay"])
            for lid in sheet_agg if lid in db and abs(sheet_agg[lid]["rev"] - db[lid]["rev"]) > 0.5]
    print(f"\n-- выручка расходится у доехавших лидов: {len(diff)} --")
    delta = sum(s - d for _, s, d, _ in diff)
    print(f"суммарная разница лист−база: {delta:.0f}")
    for lid, s, d, p in sorted(diff, key=lambda x: abs(x[1] - x[2]), reverse=True)[:15]:
        print(f"   lead {lid} ({p or '—'}): лист {s:.0f} ≠ база {d:.0f}")

    # --- выручка по месяцам платежа ---
    sheet_m = collections.Counter()
    sheet_n = collections.Counter()
    for lid, a in sheet_agg.items():
        if a["pay"]:
            sheet_m[a["pay"][:7]] += a["rev"]
            sheet_n[a["pay"][:7]] += 1
    db_m = collections.Counter()
    db_n = collections.Counter()
    for lid, a in db.items():
        if a["pay"]:
            db_m[a["pay"][:7]] += a["rev"]
            db_n[a["pay"][:7]] += 1
    print("\n-- по месяцу платежа: оплаты и выручка (лист ↔ база) --")
    for m in sorted(set(sheet_m) | set(db_m)):
        mark = "" if abs(sheet_m[m] - db_m[m]) < 1 and sheet_n[m] == db_n[m] else "   <--"
        print(f"   {m}  лист {sheet_n[m]:4d} / {sheet_m[m]:12.0f}   база {db_n[m]:4d} / {db_m[m]:12.0f}{mark}")

    # --- потерянные оплаты по месяцам ---
    miss = [lid for lid in sheet_agg if lid not in db]
    lost = collections.Counter()
    lost_n = collections.Counter()
    for lid in miss:
        m = sheet_agg[lid]["pay"][:7] or "без даты"
        lost[m] += sheet_agg[lid]["rev"]
        lost_n[m] += 1
    print(f"\n-- не доехало оплат: {len(miss)}, выручки {sum(lost.values()):.0f} --")
    print(f"   из них без строки лида в листах «Лиды»: {sum(1 for l in miss if l not in lead_ids)}")
    for m in sorted(lost):
        print(f"   {m}  {lost_n[m]:3d} оплат / {lost[m]:12.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
