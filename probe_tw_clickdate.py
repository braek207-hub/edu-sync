# -*- coding: utf-8 -*-
"""Заказы TW, отнесённые к неделе по ДАТЕ КЛИКА (как в отчётах кабинетов/TW UI), а не по дате заказа."""
import io, os, sys
from collections import defaultdict
from datetime import datetime, date
from dotenv import load_dotenv
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
load_dotenv()
from sync.gcc_channels import map_tw_source
from sync.gcc_triplewhale import fetch_tw_orders, order_country

WFRM, WTO = sys.argv[1], sys.argv[2]           # неделя
FFRM, FTO = sys.argv[3], sys.argv[4]           # окно выборки заказов (шире недели)
orders = fetch_tw_orders(os.environ["GCC_TRIPLEWHALE_API_KEY"], os.environ.get("GCC_TW_SHOP_DOMAIN") or "", FFRM, FTO)
print(f"заказов в выборке {FFRM}…{FTO}: {len(orders)}")

def dt(s):
    try: return datetime.fromisoformat(s)
    except Exception: return None

PAID = {"google-ads", "facebook-ads", "tiktok-ads", "snapchat-ads", "bing-ads"}
NAMES = {"23274908571": "Бренд AE #3", "21087796023": "Бренд AE", "21067876545": "PMAX Retarget AE",
         "23865229385": "Привлечение AE", "22939132072": "Бренд SA", "22938928792": "Бренд Qatar",
         "23147258308": "Бренд KW", "23147215099": "Бренд OM"}
w0, w1 = date.fromisoformat(WFRM), date.fromisoformat(WTO)

camp = defaultdict(float)
buck = defaultdict(float)
for o in orders:
    tps = (o.get("attribution") or {}).get("linearAll") or []
    paid = [t for t in tps if (t.get("source") or "").lower() in PAID]
    if not paid:
        continue
    last = max(paid, key=lambda t: (dt(t.get("clickDate") or "") or datetime.min.replace(tzinfo=None)).timestamp()
               if dt(t.get("clickDate") or "") else 0)
    d = dt(last.get("clickDate") or "")
    if not d or not (w0 <= d.date() <= w1):     # клик вне недели → заказ в неделю не идёт
        continue
    ch, sub, tt = map_tw_source(last.get("source"), (last.get("campaignId") or "").strip() or None)
    b = "Google" if sub == "Google.Adwords" else "FB"
    buck[b] += 1
    buck[(b, order_country(o) or "—")] += 1
    cid = str(last.get("campaignId") or "—")
    camp[(b, NAMES.get(cid, cid))] += 1

print(f"\nнеделя по дате клика {WFRM}…{WTO}:  Google {buck['Google']:.0f}   FB {buck['FB']:.0f}")
print("  (ручной UAE: Google 84 = Brand 79 + PMax 5;  FB 32 = CPO 20 + Catalog 12)")
for k, v in sorted(camp.items(), key=lambda x: -x[1])[:10]:
    print(f"    {k[0]:8}{str(k[1])[:22]:24}{v:6.0f}")
print("  по странам:")
for k, v in sorted(((k, v) for k, v in buck.items() if isinstance(k, tuple)), key=lambda x: -x[1])[:10]:
    print(f"    {k[0]:8}{k[1][:22]:24}{v:6.0f}")
