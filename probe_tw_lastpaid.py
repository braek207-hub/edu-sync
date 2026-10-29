# -*- coding: utf-8 -*-
"""Заказ целиком последнему ПЛАТНОМУ клику (как показывает интерфейс TW) — по кампаниям ОАЭ."""
import io, os, sys
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
load_dotenv()
from sync.gcc_channels import map_tw_source
from sync.gcc_triplewhale import fetch_tw_orders, order_country

FRM, TO = sys.argv[1], sys.argv[2]
WIN = int(sys.argv[3]) if len(sys.argv) > 3 else 0        # 0 = без окна
orders = fetch_tw_orders(os.environ["GCC_TRIPLEWHALE_API_KEY"], os.environ.get("GCC_TW_SHOP_DOMAIN") or "", FRM, TO)

def dt(s):
    try: return datetime.fromisoformat(s)
    except Exception: return None

PAID = {"google-ads", "facebook-ads", "tiktok-ads", "snapchat-ads", "bing-ads"}
by_camp = defaultdict(float)
by_bucket = defaultdict(float)
for o in orders:
    if (order_country(o) or "") != "ОАЭ":
        continue
    od = dt(o.get("created_at") or "")
    tps = (o.get("attribution") or {}).get("linearAll") or []
    paid = [t for t in tps if (t.get("source") or "").lower() in PAID]
    if WIN and od:
        paid = [t for t in paid if (d := dt(t.get("clickDate") or "")) and (od - d).days <= WIN]
    if not paid:
        by_bucket["organic"] += 1
        continue
    last = max(paid, key=lambda t: dt(t.get("clickDate") or "") or datetime.min.replace(tzinfo=od.tzinfo))
    ch, sub, tt = map_tw_source(last.get("source"), (last.get("campaignId") or "").strip() or None)
    b = "Google" if sub == "Google.Adwords" else "FB"
    by_bucket[b] += 1
    by_camp[(b, str(last.get("campaignId") or "—"))] += 1

win = f"окно {WIN}д" if WIN else "без окна"
print(f"ОАЭ, {FRM}…{TO}, заказ целиком последнему платному клику ({win})")
print(f"  Google {by_bucket['Google']:.0f}   FB {by_bucket['FB']:.0f}   органика {by_bucket['organic']:.0f}")
print("  (ручной: Google 84, FB 32, органика web 109)\n  топ кампаний:")
for k, v in sorted(by_camp.items(), key=lambda x: -x[1])[:10]:
    print(f"    {k[0]:8}{k[1]:>16}  {v:5.0f}")
