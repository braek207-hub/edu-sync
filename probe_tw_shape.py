# -*- coding: utf-8 -*-
"""Форма заказа TW: какие поля у заказа и у тачпоинта (для окна атрибуции)."""
import io, json, os, sys
from dotenv import load_dotenv
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
load_dotenv()
from sync.gcc_triplewhale import fetch_tw_orders
orders = fetch_tw_orders(os.environ["GCC_TRIPLEWHALE_API_KEY"], os.environ.get("GCC_TW_SHOP_DOMAIN") or "", sys.argv[1], sys.argv[1])
o = max(orders, key=lambda x: len((x.get("attribution") or {}).get("linearAll") or []))
print("поля заказа:", sorted(o.keys()))
print("\nзаказ:", {k: v for k, v in o.items() if k not in ("attribution", "journey")})
tps = (o.get("attribution") or {}).get("linearAll") or []
print(f"\nтачпоинтов linearAll: {len(tps)}")
for t in tps[:6]:
    print("  ", json.dumps(t, ensure_ascii=False)[:300])
j = o.get("journey") or []
print(f"\njourney: {len(j)} событий")
for e in j[:4]:
    print("  ", json.dumps(e, ensure_ascii=False)[:300])
