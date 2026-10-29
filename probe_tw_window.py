# -*- coding: utf-8 -*-
"""Окно атрибуции TW: какое даёт числа ручного отчёта (ОАЭ Google 84 / FB 32)."""
import io, os, sys
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
load_dotenv()
from sync.gcc_channels import map_tw_source
from sync.gcc_triplewhale import _is_internal_touch, fetch_tw_orders, order_country

FRM, TO = sys.argv[1], sys.argv[2]
orders = fetch_tw_orders(os.environ["GCC_TRIPLEWHALE_API_KEY"], os.environ.get("GCC_TW_SHOP_DOMAIN") or "", FRM, TO)

def dt(s):
    try: return datetime.fromisoformat(s)
    except Exception: return None

def bucket(t):
    ch, sub, tt = map_tw_source(t.get("source"), (t.get("campaignId") or "").strip() or None)
    return "Google" if sub == "Google.Adwords" else ("FB" if tt == "Платный" else "organic")

def run(days, model="linearAll"):
    agg = defaultdict(float)
    for o in orders:
        c = order_country(o) or "—"
        od = dt(o.get("created_at") or "")
        tps = (o.get("attribution") or {}).get(model) or []
        if days and od:
            keep = []
            for t in tps:
                td = dt(t.get("clickDate") or "")
                if td is None or (od - td).days <= days:
                    keep.append(t)
            tps = keep or tps          # пустое окно → заказ остаётся как есть
        if not tps: continue
        ext = [t for t in tps if not _is_internal_touch(t)]
        tps = ext or tps
        w = 1.0 / len(tps)
        for t in tps:
            agg[(c, bucket(t))] += w
    return agg

print(f"заказов {len(orders)}  {FRM}…{TO}   (ручной W34: ОАЭ Google 84 / FB 32; Залив Google ~105 / FB ~33)")
print(f"{'окно':>8}{'ОАЭ Google':>12}{'ОАЭ FB':>9}{'ОАЭ org':>9}   {'Залив Google':>13}{'Залив FB':>10}{'Залив org':>11}")
for days in (None, 90, 30, 28, 14, 7, 3, 1):
    a = run(days)
    g = sum(v for k, v in a.items() if k[1] == "Google")
    f = sum(v for k, v in a.items() if k[1] == "FB")
    o_ = sum(v for k, v in a.items() if k[1] == "organic")
    lbl = "все" if days is None else f"{days}д"
    print(f"{lbl:>8}{a.get(('ОАЭ','Google'),0):12.1f}{a.get(('ОАЭ','FB'),0):9.1f}{a.get(('ОАЭ','organic'),0):9.1f}   {g:13.1f}{f:10.1f}{o_:11.1f}")
