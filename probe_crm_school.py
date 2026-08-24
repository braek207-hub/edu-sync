"""Разовый разбор: как ленд `eduschool` (school.edunetwork.ru) лежит в книге CRM.

Отвечает на два вопроса перед правкой синка:
  1) какие вообще листы есть в книге (не добавили ли отдельные под школу);
  2) есть ли строки школы в «Оплаты» и склеиваются ли они с лидами по ID лида.
Только чтение.
"""

from __future__ import annotations

import os
from collections import Counter

from sync.sheets import get_sheets_service, read_sheet
from sync.utils import pick_index_loose, normalize_campaign_id, to_iso_date, to_num
from sync.crm import crm_leads_sheets, crm_payments_sheets, find_revenue_index, find_orders_index

LAND = os.environ.get("PROBE_LAND", "eduschool").strip().lower()


def _cell(row, idx):
    if idx < 0 or idx >= len(row):
        return ""
    return row[idx]


def main() -> None:
    if not os.environ.get("GOOGLE_SHEETS_ID") and os.environ.get("SHEET_ID_EDU"):
        os.environ["GOOGLE_SHEETS_ID"] = os.environ["SHEET_ID_EDU"]
    sid = os.environ["GOOGLE_SHEETS_ID"]
    service = get_sheets_service()

    meta = service.spreadsheets().get(spreadsheetId=sid).execute(num_retries=4)
    print("=== ЛИСТЫ КНИГИ ===")
    for s in meta.get("sheets", []):
        p = s["properties"]
        grid = p.get("gridProperties", {})
        print(f"  {p['title']!r}: {grid.get('rowCount')}×{grid.get('columnCount')}")

    lead_ids_of_land: set[str] = set()

    for sheet in crm_leads_sheets():
        values = read_sheet(service, sid, sheet)
        if len(values) < 2:
            print(f"\n=== ЛИДЫ [{sheet}] — пусто")
            continue
        headers = [str(x) for x in values[0]]
        print(f"\n=== ЛИДЫ [{sheet}] — строк {len(values) - 1}")
        print("  колонки:", headers)
        i_land = pick_index_loose(headers, ["ленд", "land"])
        i_lead = pick_index_loose(headers, ["id лида в scrm", "lead id", "id лида"])
        i_camp = pick_index_loose(headers, ["кампания", "utm campaign", "utm_campaign"])
        i_date = pick_index_loose(headers, ["дата", "date"])
        lands = Counter()
        for row in values[1:]:
            land = str(_cell(row, i_land)).strip().lower() if i_land != -1 else ""
            lands[land] += 1
            if land == LAND:
                lid = normalize_campaign_id(_cell(row, i_lead)) if i_lead != -1 else ""
                if lid:
                    lead_ids_of_land.add(lid)
        print("  ленды:", dict(lands.most_common(20)))
        rows = [r for r in values[1:] if str(_cell(r, i_land)).strip().lower() == LAND]
        print(f"  строк ленда {LAND}: {len(rows)}, из них с ID лида: {len(lead_ids_of_land)}")
        if rows:
            camps = Counter(normalize_campaign_id(_cell(r, i_camp)) for r in rows)
            dates = sorted({to_iso_date(_cell(r, i_date)) for r in rows if to_iso_date(_cell(r, i_date))})
            print("  кампании:", dict(camps.most_common(10)))
            print("  период:", dates[:1], "…", dates[-1:])
            print("  пример строки:", rows[0])

    for sheet in crm_payments_sheets():
        values = read_sheet(service, sid, sheet)
        if len(values) < 2:
            print(f"\n=== ОПЛАТЫ [{sheet}] — пусто")
            continue
        headers = [str(x) for x in values[0]]
        print(f"\n=== ОПЛАТЫ [{sheet}] — строк {len(values) - 1}")
        print("  колонки:", headers)
        i_land = pick_index_loose(headers, ["ленд", "land"])
        i_lead = pick_index_loose(headers, ["id лида в scrm", "lead id", "id лида"])
        i_camp = pick_index_loose(headers, ["кампания", "utm campaign", "utm_campaign"])
        i_pay = pick_index_loose(headers, ["date pay", "дата оплаты"])
        i_rev = find_revenue_index(headers)
        i_ord = find_orders_index(headers)
        lands = Counter()
        for row in values[1:]:
            lands[str(_cell(row, i_land)).strip().lower() if i_land != -1 else ""] += 1
        print("  ленды:", dict(lands.most_common(20)))

        rows = [r for r in values[1:] if str(_cell(r, i_land)).strip().lower() == LAND]
        print(f"  строк ленда {LAND}: {len(rows)}")
        if rows:
            print("  пример:", rows[0])
            camps = Counter(normalize_campaign_id(_cell(r, i_camp)) for r in rows)
            print("  кампании:", dict(camps.most_common(10)))
            no_camp = sum(1 for r in rows if not normalize_campaign_id(_cell(r, i_camp)))
            no_lead = sum(1 for r in rows if not normalize_campaign_id(_cell(r, i_lead)))
            paid_rev = sum(to_num(_cell(r, i_rev)) for r in rows) if i_rev != -1 else 0.0
            ord1 = sum(1 for r in rows if i_ord != -1 and round(to_num(_cell(r, i_ord))) == 1)
            days = sorted({to_iso_date(_cell(r, i_pay)) for r in rows if to_iso_date(_cell(r, i_pay))})
            print(f"  без кампании: {no_camp}, без ID лида: {no_lead}, orders==1: {ord1}, выручка: {paid_rev:.0f}")
            print("  период оплат:", days[:1], "…", days[-1:])

        # Склейка в обратную сторону: оплаты, чей ID лида есть среди лидов школы,
        # но сама строка оплаты помечена другим лендом (или пустым).
        if lead_ids_of_land and i_lead != -1:
            cross = [
                r for r in values[1:]
                if normalize_campaign_id(_cell(r, i_lead)) in lead_ids_of_land
            ]
            print(f"  строк с ID лида школы (любой ленд в оплате): {len(cross)}")
            for r in cross[:5]:
                print("    ", r)


if __name__ == "__main__":
    main()
