# -*- coding: utf-8 -*-
"""probe_crm_pay_orphans.py — кто такие 57 оплат без строки лида.

Вопрос, от которого зависит вердикт: это реклама (тогда дашборд недосчитывает выручку
и чинить надо выгрузку CRM) или лиды не из Директа (тогда им в дашборде не место).
Смотрим у каждой такой оплаты кампанию и ленд из листа «Оплаты» и сверяем кампанию
с direct_stats.

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

    pay = {}
    for sheet in crm_payments_sheets():
        values = read_sheet(svc, sid, sheet)
        if len(values) < 2:
            continue
        h = [str(x) for x in values[0]]
        i_lead = pick_index_loose(h, ["id лида в scrm", "lead id", "id лида"])
        i_pay = pick_index_loose(h, ["date pay", "дата оплаты"])
        i_camp = pick_index_loose(h, ["кампания", "utm campaign", "utm_campaign"])
        i_land = pick_index_loose(h, ["ленд", "land"])
        i_src = pick_index_loose(h, ["источник", "utm source", "utm_source"])
        i_ord, i_rev = find_orders_index(h), find_revenue_index(h)
        if i_land == -1 or i_src == -1:
            print(f"[{sheet}] колонки: {' | '.join(h)}")
        for r in values[1:]:
            lid = normalize_campaign_id(_cell(r, i_lead))
            if not lid or (i_ord != -1 and round(to_num(_cell(r, i_ord))) != 1):
                continue
            pay[lid] = {
                "pay": to_iso_date(_cell(r, i_pay)) if i_pay != -1 else "",
                "cid": normalize_campaign_id(_cell(r, i_camp)) if i_camp != -1 else "",
                "land": _cell(r, i_land).lower() if i_land != -1 else "",
                "src": _cell(r, i_src).lower() if i_src != -1 else "",
                "rev": to_num(_cell(r, i_rev)) if i_rev != -1 else 0.0,
            }

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
        cur.execute("SELECT lead_id FROM crm_lead_details WHERE is_paid")
        db = {r["lead_id"] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT campaign_id FROM direct_stats")
        direct = {r["campaign_id"] for r in cur.fetchall()}

    orphans = {lid: p for lid, p in pay.items() if lid not in db and lid not in lead_ids}
    print(f"оплат без строки лида: {len(orphans)}, выручка {sum(p['rev'] for p in orphans.values()):.0f}")

    by_src = collections.Counter()
    in_direct = out_direct = no_cid = 0
    money_direct = 0.0
    for lid, p in sorted(orphans.items(), key=lambda kv: kv[1]["pay"] or "", reverse=True):
        where = "нет кампании"
        if p["cid"]:
            if p["cid"] in direct:
                where, in_direct = "кампания Директа", in_direct + 1
                money_direct += p["rev"]
            else:
                where, out_direct = "кампания НЕ в direct_stats", out_direct + 1
        else:
            no_cid += 1
        by_src[(p["src"] or "—", p["land"] or "—", where)] += 1
        print(f"   {p['pay'] or '—'}  lead {lid}  {p['rev']:9.0f}  cid={p['cid'] or '—':>12}  "
              f"src={p['src'] or '—':<12} land={p['land'] or '—':<10} {where}")

    print(f"\nитого: кампания Директа {in_direct} (выручка {money_direct:.0f}), "
          f"кампания вне direct_stats {out_direct}, без кампании {no_cid}")
    print("\n-- срезы (источник / ленд / кампания) --")
    for (src, land, where), n in by_src.most_common():
        print(f"   {n:3d}  src={src:<14} land={land:<12} {where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
