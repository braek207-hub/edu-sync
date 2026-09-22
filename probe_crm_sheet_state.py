# -*- coding: utf-8 -*-
"""probe_crm_sheet_state.py — насколько заполнен лист CRM «Лиды» прямо сейчас.

22.09.2026 лист опустел (130k пустых строк, макс. дата 12.01.2026), синк снёс
crm_leads/crm_lead_details за 2026 год. Проба показывает, идёт ли заполнение
обратно: только счётчики по месяцам, без содержимого ячеек.
Только чтение. ENV: GOOGLE_SHEETS_ID + ключ сервис-аккаунта.
"""
import collections
import os

from sync.crm import crm_leads_sheets
from sync.sheets import get_sheets_service, read_sheet


def main() -> int:
    svc = get_sheets_service()
    sid = os.environ["GOOGLE_SHEETS_ID"]
    meta = svc.spreadsheets().get(spreadsheetId=sid, fields="sheets.properties").execute()
    for s in meta["sheets"]:
        p = s["properties"]
        print("tab:", p["title"], p.get("gridProperties"))
    for name in crm_leads_sheets():
        vals = read_sheet(svc, sid, name)
        dated = [r for r in vals[1:] if len(r) > 1 and str(r[1]).strip()]
        print(f"[{name}] строк {len(vals) - 1}, с датой {len(dated)}, "
              f"макс. дата {max((str(r[1]) for r in dated), default=None)}")
        by_month = collections.Counter(str(r[1])[:7] for r in dated)
        for k in sorted(by_month):
            print(f"  {k}: {by_month[k]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
