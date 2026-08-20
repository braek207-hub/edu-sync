# -*- coding: utf-8 -*-
"""Зонд W33: варианты linearAll против ручного отчёта — где сидят −6% paid и −48% CRM.

Гипотеза: агентство (TW UI) не даёт кредита касаниям своих доменов (наш «Internal»,
44 заказа/нед) и, возможно, Non-attributed — эти веса в UI перераспределяются на
остальные касания пути. Считаем три варианта на ОДНИХ данных:
  V0 — как в дашборде: все касания linearAll, 1/N.
  V1 — касания Internal (organic_and_social + свой домен) выкинуты из знаменателя.
  V2 — V1 + выкинуты Non-attributed/пустые source.
Если у заказа все касания выкинуты — оставляем полный набор (V0-поведение).

Web/app сплит — Shopify (нужен API_LIME_SHOPIFY → запуск в CI).
Запуск: python scripts/probe_gcc_w33_orders_variants.py
"""
import io
import os
import sys
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sync.fx import to_rub  # noqa: E402
from sync.gcc_channels import _OWN_DOMAINS, map_tw_source  # noqa: E402
from sync.gcc_shopify import fetch_order_meta  # noqa: E402
from sync.gcc_triplewhale import fetch_tw_orders, order_touchpoint  # noqa: E402

FRM, TO = "2026-08-10", "2026-08-16"

MANUAL = {
    "web": {"Meta": (40, 487617), "Google": (121, 1802783), "CRM": (27, 321783),
            "DSS": (107, 1806316), "Others": (2, 20959)},
    "app": (42, 610310),
}


def touch_class(tp: dict) -> tuple[str, str, str]:
    src = tp.get("source") or None
    ref = (tp.get("campaignId") or "").strip() or None
    return map_tw_source(src, ref)


def is_internal(tp: dict) -> bool:
    if (tp.get("source") or "").lower() != "organic_and_social":
        return False
    ref = (tp.get("campaignId") or "").strip().lower()
    return any(own in ref for own in _OWN_DOMAINS)


def is_nonattr(tp: dict) -> bool:
    s = (tp.get("source") or "").strip().lower()
    return not s or s == "non-attributed"


def bucket(channel: str, subchannel: str, tt: str) -> str:
    if tt == "Платный":
        if subchannel == "Meta Ads":
            return "Meta"
        if subchannel == "Google.Adwords":
            return "Google"
        return "PaidOther"
    if channel == "CRM":
        return "CRM"
    if channel in ("Direct", "SEO", "SMM (organic)", "Internal"):
        return "DSS"
    return "Others"  # Others + Referrals + прочее


def main() -> None:
    key, shop = os.environ["GCC_TRIPLEWHALE_API_KEY"], os.environ["GCC_TW_SHOP_DOMAIN"]
    orders = fetch_tw_orders(key, shop, FRM, TO)
    _country, app_ids = fetch_order_meta(os.environ["API_LIME_SHOPIFY"], FRM, TO)
    print(f"W33 заказов TW: {len(orders)}, app-канал Shopify: {len(app_ids)}")

    variants = {
        "V0 все касания": lambda tps: tps,
        "V1 без Internal": lambda tps: [t for t in tps if not is_internal(t)] or tps,
        "V2 без Internal и Non-attr": lambda tps: [t for t in tps
                                                   if not is_internal(t) and not is_nonattr(t)] or tps,
    }

    for vname, keep in variants.items():
        web = defaultdict(lambda: [0.0, 0.0])
        app = [0.0, 0.0]
        for o in orders:
            oid = str(o.get("order_id") or "")
            iso = str(o.get("created_at") or TO)[:10]
            rate = to_rub("AED", iso if FRM <= iso <= TO else TO)
            rev = float(o.get("total_price") or 0) * rate
            tps = (o.get("attribution") or {}).get("linearAll") or []
            if not tps:
                tps = [order_touchpoint(o)]
            tps = keep(tps)
            w = 1.0 / len(tps)
            if oid in app_ids:
                app[0] += 1
                app[1] += rev
                continue
            for tp in tps:
                b = bucket(*touch_class(tp))
                web[b][0] += w
                web[b][1] += rev * w
        paid_o = sum(web[b][0] for b in ("Meta", "Google", "PaidOther"))
        paid_r = sum(web[b][1] for b in ("Meta", "Google", "PaidOther"))
        print(f"\n=== {vname} ===")
        print(f"  {'корзина':<10} {'заказы':>8} {'ручной':>7} | {'выручка ₽':>10} {'ручной':>9}")
        for b in ("Meta", "Google", "PaidOther", "CRM", "DSS", "Others"):
            mo, mr = MANUAL["web"].get(b, ("—", "—"))
            print(f"  {b:<10} {web[b][0]:>8.1f} {mo!s:>7} | {web[b][1]:>10.0f} {mr!s:>9}")
        print(f"  {'PAID web':<10} {paid_o:>8.1f} {161:>7} | {paid_r:>10.0f} {2290400:>9}")
        print(f"  {'APP':<10} {app[0]:>8.0f} {42:>7} | {app[1]:>10.0f} {610310:>9}")


if __name__ == "__main__":
    main()
