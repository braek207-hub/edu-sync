# -*- coding: utf-8 -*-
"""probe_orders_zero.py — что такое строки листа «Оплаты» с orders≠1.

Повод: за 1–11.10 в crm_payments выручки на 363 040 ₽ больше, чем в crm_lead_details,
при одинаковом числе оплат. Причина в коде: `payments` считается только при orders==1,
а `revenue` прибавляется у ЛЮБОЙ строки (sync/crm.py, _parse_payments_sheet). В
crm_lead_details строки с orders≠1 отбрасываются целиком вместе с их выручкой.

Вопрос, от которого зависит, где дефект: orders=0 — это доплата по уже оплаченной сделке
(тогда деньги реальные и их теряет дашборд) или отменённая/нулевая строка (тогда лишние
деньги у crm_payments). Печатаем все строки октября и сводку по всем orders≠1.

ENV: GOOGLE_SHEETS_ID + ключ сервис-аккаунта. Только чтение.
"""
import collections
import os

from sync.crm import crm_payments_sheets, find_orders_index, find_revenue_index
from sync.sheets import get_sheets_service, read_sheet
from sync.utils import normalize_campaign_id, pick_index_loose, to_iso_date, to_num

FROM, TO = "2026-10-01", "2026-10-11"


def _cell(row, i):
    return "" if i == -1 or i >= len(row) else str(row[i] or "").strip()


def main() -> int:
    svc = get_sheets_service()
    sid = os.environ["GOOGLE_SHEETS_ID"]

    by_lead = collections.defaultdict(list)
    stages = collections.Counter()
    tot_zero = tot_zero_money = 0

    for sheet in crm_payments_sheets():
        values = read_sheet(svc, sid, sheet)
        if len(values) < 2:
            continue
        h = [str(x) for x in values[0]]
        i_lead = pick_index_loose(h, ["id лида в scrm", "lead id", "id лида"])
        i_pay = pick_index_loose(h, ["date pay", "дата оплаты"])
        i_camp = pick_index_loose(h, ["кампания", "utm campaign", "utm_campaign"])
        i_stage = pick_index_loose(h, ["этап", "stage"])
        i_ord, i_rev = find_orders_index(h), find_revenue_index(h)
        i_prod = next((i for i, x in enumerate(h)
                       if "продукт" in str(x or "").lower() and "группа" not in str(x or "").lower()), -1)
        print(f"[{sheet}] orders={h[i_ord] if i_ord != -1 else '—'}, "
              f"revenue={h[i_rev] if i_rev != -1 else '—'}")
        for n, r in enumerate(values[1:], start=2):
            o = round(to_num(_cell(r, i_ord))) if i_ord != -1 else 1
            rev = to_num(_cell(r, i_rev)) if i_rev != -1 else 0.0
            pay = to_iso_date(_cell(r, i_pay)) if i_pay != -1 else ""
            if o != 1:
                tot_zero += 1
                tot_zero_money += rev
                stages[(o, _cell(r, i_stage))] += 1
            if FROM <= pay <= TO:
                by_lead[normalize_campaign_id(_cell(r, i_lead))].append({
                    "sheet": sheet, "row": n, "pay": pay, "orders": o, "rev": rev,
                    "cid": normalize_campaign_id(_cell(r, i_camp)) if i_camp != -1 else "",
                    "stage": _cell(r, i_stage), "prod": _cell(r, i_prod)[:60],
                })

    print(f"\n-- строки {FROM}..{TO} --")
    for lid, rs in sorted(by_lead.items(), key=lambda kv: kv[1][0]["pay"]):
        flag = "  <-- есть строка с orders≠1" if any(x["orders"] != 1 for x in rs) else ""
        print(f"lead {lid}{flag}")
        for x in rs:
            print(f"   {x['pay']}  orders={x['orders']}  выручка={x['rev']:9.0f}  "
                  f"cid={x['cid']}  этап={x['stage']}  продукт={x['prod']}")

    print(f"\n-- всего строк с orders≠1 во всех листах: {tot_zero}, выручка {tot_zero_money:.0f} --")
    for (o, stage), n in stages.most_common(20):
        print(f"   orders={o:3d}  этап={stage or '—':<30} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
