# -*- coding: utf-8 -*-
"""Все модели атрибуции TW за неделю: весь Залив и по странам. Заказы целые и дробные."""
import io, os, sys
from collections import defaultdict
from dotenv import load_dotenv
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
load_dotenv()
from sync.gcc_channels import map_tw_source
from sync.gcc_triplewhale import _is_internal_touch, fetch_tw_orders, order_country

FRM, TO = sys.argv[1], sys.argv[2]
orders = fetch_tw_orders(os.environ["GCC_TRIPLEWHALE_API_KEY"], os.environ.get("GCC_TW_SHOP_DOMAIN") or "", FRM, TO)
print(f"заказов {len(orders)}  {FRM}…{TO}")

keys = defaultdict(int)
for o in orders:
    for k, v in (o.get("attribution") or {}).items():
        keys[k] += 1 if v else 0
print("модели в ответе (сколько заказов непустых):", dict(keys))

def bucket(t):
    ch, sub, tt = map_tw_source(t.get("source"), (t.get("campaignId") or "").strip() or None)
    return "Google" if sub == "Google.Adwords" else ("FB" if tt == "Платный" else "organic")

def run(model, whole, drop_internal=True):
    agg = defaultdict(float)
    for o in orders:
        c = order_country(o) or "—"
        tps = (o.get("attribution") or {}).get(model) or []
        if not tps:
            continue
        if drop_internal:
            ext = [t for t in tps if not _is_internal_touch(t)]
            tps = ext or tps
        if whole:                      # заказ целиком первому касанию (как UI: 1 заказ = 1 канал)
            agg[(c, bucket(tps[0]))] += 1
        else:
            w = 1.0 / len(tps)
            for t in tps:
                agg[(c, bucket(t))] += w
    return agg

for model in [m for m in ("linear", "fullFirstClick") if keys.get(m)]:
    for whole in (False, True):
        a = run(model, whole)
        tag = "целые (1-й тачпоинт)" if whole else "дробные 1/N"
        g = sum(v for k, v in a.items() if k[1] == "Google")
        f = sum(v for k, v in a.items() if k[1] == "FB")
        print(f"\n{model} · {tag}:  ВЕСЬ ЗАЛИВ Google {g:.1f}  FB {f:.1f}")
        for c in sorted({k[0] for k in a}):
            print(f"   {c[:20]:22}Google {a.get((c,'Google'),0):7.1f}  FB {a.get((c,'FB'),0):6.1f}  org {a.get((c,'organic'),0):7.1f}")
