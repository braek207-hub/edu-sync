# -*- coding: utf-8 -*-
"""probe_lead_lookup.py — все строки листов CRM по конкретным лидам, сырые и разобранные.

Нужна, когда «в таблице одна дата, в дашборде другая»: показывает КАЖДУЮ строку оплат
по лиду (у лида их может быть несколько — синк берёт самую раннюю дату), строку лида и
то, что лежит в витрине.

ENV: LEAD_IDS (через запятую) + GOOGLE_SHEETS_ID + ключ сервис-аккаунта + DATABASE_URL.
Только чтение.
"""
import os

import psycopg2.extras

from sync.crm import crm_leads_sheets, crm_payments_sheets, find_orders_index, find_revenue_index
from sync.db import get_connection
from sync.sheets import get_sheets_service, read_sheet
from sync.utils import normalize_campaign_id, pick_index_loose, to_iso_date, to_num


def _cell(row, i):
    return "" if i == -1 or i >= len(row) else str(row[i] or "").strip()


def main() -> int:
    ids = {x.strip() for x in os.environ.get("LEAD_IDS", "").split(",") if x.strip()}
    if not ids:
        print("LEAD_IDS пуст")
        return 1
    svc = get_sheets_service()
    sid = os.environ["GOOGLE_SHEETS_ID"]

    for sheet in crm_payments_sheets():
        values = read_sheet(svc, sid, sheet)
        if len(values) < 2:
            continue
        h = [str(x) for x in values[0]]
        i_lead = pick_index_loose(h, ["id лида в scrm", "lead id", "id лида"])
        i_pay = pick_index_loose(h, ["date pay", "дата оплаты"])
        i_camp = pick_index_loose(h, ["кампания", "utm campaign", "utm_campaign"])
        i_ord, i_rev = find_orders_index(h), find_revenue_index(h)
        i_stage = pick_index_loose(h, ["этап", "stage"])
        i_created = pick_index_loose(h, ["дата создания", "date created"])
        for n, r in enumerate(values[1:], start=2):
            if normalize_campaign_id(_cell(r, i_lead)) not in ids:
                continue
            raw = _cell(r, i_pay)
            print(f"[{sheet}] строка {n}: lead={_cell(r, i_lead)} "
                  f"date pay сырое={raw!r} → {to_iso_date(raw)!r}; "
                  f"создан={_cell(r, i_created)}; orders={_cell(r, i_ord)} "
                  f"(={round(to_num(_cell(r, i_ord)))}); выручка={_cell(r, i_rev)}; "
                  f"кампания={_cell(r, i_camp)}; этап={_cell(r, i_stage)}")

    for sheet in crm_leads_sheets():
        values = read_sheet(svc, sid, sheet)
        if len(values) < 2:
            continue
        h = [str(x) for x in values[0]]
        i_id = pick_index_loose(h, ["id"])
        i_date = pick_index_loose(h, ["дата создания", "date created", "дата", "date"])
        i_camp = pick_index_loose(h, ["utm campaign", "utm_campaign", "кампания", "campaign"])
        for n, r in enumerate(values[1:], start=2):
            if normalize_campaign_id(_cell(r, i_id)) not in ids:
                continue
            print(f"[{sheet}] строка {n}: id={_cell(r, i_id)} "
                  f"дата сырое={_cell(r, i_date)!r} → {to_iso_date(_cell(r, i_date))!r}; "
                  f"кампания={_cell(r, i_camp)}")

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT lead_id, project, campaign_id, created_date::text cd, payment_date::text pd,"
            " is_paid, amount, stage FROM crm_lead_details WHERE lead_id = ANY(%s)", (list(ids),))
        rows = cur.fetchall()
    print(f"\n-- crm_lead_details: {len(rows)} строк --")
    for r in rows:
        print("   " + "; ".join(f"{k}={v}" for k, v in r.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
