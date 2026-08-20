# -*- coding: utf-8 -*-
"""Зонд перед переводом GCC-дашборда на GA4-трафик + linearAll-атрибуцию.

Три вопроса:
  [A] GA4: работает ли прод-набор измерений date×hostName×source×medium×campaign
      (sessions, totalUsers, newUsers) и какова потеря к грандтоталу без измерений.
  [B] GA4: есть ли события воронки (add_to_cart / begin_checkout) и bounce/depth.
  [C] TW linearAll: доля заказов без linearAll (нужен fallback), распределение числа
      касаний, какие source встречаются в касаниях, есть ли campaignId у касаний.

Read-only, печатает только агрегаты. Запуск: python -m scripts.probe_gcc_dashboard_switch [дней]
"""
import io
import os
import sys
from collections import Counter
from datetime import date, timedelta

from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sync.gcc_ga4 import GA4_PROPERTY, HOST_COUNTRY, run_report  # noqa: E402
from sync.gcc_triplewhale import TW_ORDERS_URL, _tw_post  # noqa: E402


def probe_ga4(frm: str, to: str) -> None:
    print(f"\n===== [A] GA4 прод-набор измерений ({frm}..{to}) =====")
    grand = run_report(GA4_PROPERTY, frm, to, ["date"], ("sessions", "totalUsers", "newUsers"))
    g_ses = sum(int(r["metrics"][0]) for r in grand)
    g_usr = sum(int(r["metrics"][1]) for r in grand)
    g_new = sum(int(r["metrics"][2]) for r in grand)
    print(f"грандтотал (только date): sessions={g_ses} totalUsers(сумма дней)={g_usr} newUsers={g_new}")

    dims = ["date", "hostName", "sessionSource", "sessionMedium", "sessionCampaignName"]
    rows = run_report(GA4_PROPERTY, frm, to, dims, ("sessions", "totalUsers", "newUsers"))
    ses = sum(int(r["metrics"][0]) for r in rows)
    usr = sum(int(r["metrics"][1]) for r in rows)
    hosts_in = sum(int(r["metrics"][0]) for r in rows if (r["dims"][1] or "").lower() in HOST_COUNTRY)
    print(f"с 5 измерениями: строк={len(rows)} sessions={ses} (потеря {100 * (g_ses - ses) / g_ses:.2f}%) "
          f"users_sum={usr}")
    print(f"sessions на GCC-хостах: {hosts_in} ({100 * hosts_in / ses:.1f}% всех)")

    # source/medium платного — что маппить
    paid_sm = Counter()
    camp_examples = Counter()
    for r in rows:
        s, m, c = r["dims"][2], r["dims"][3], r["dims"][4]
        if (m or "").lower() in ("cpc", "cpm", "paid", "paid_social", "ppc", "display", "cpa"):
            paid_sm[f"{s}/{m}"] += int(r["metrics"][0])
            if c and c != "(not set)":
                camp_examples[c] += int(r["metrics"][0])
    print("\nплатные source/medium (sessions, топ 15):")
    for k, v in paid_sm.most_common(15):
        print(f"  {k}: {v}")
    print("\nкампании платного (sessionCampaignName, топ 10):")
    for k, v in camp_examples.most_common(10):
        print(f"  {k[:60]!r}: {v}")

    # campaignId — числовой id есть?
    rows_id = run_report(GA4_PROPERTY, frm, to, ["sessionCampaignId"], ("sessions",))
    ids = Counter()
    for r in rows_id:
        ids[r["dims"][0] or "(пусто)"] += int(r["metrics"][0])
    print("\nsessionCampaignId (топ 12):")
    for k, v in ids.most_common(12):
        print(f"  {k[:60]!r}: {v}")

    print(f"\n===== [B] GA4 события воронки + bounce/depth =====")
    ev = run_report(GA4_PROPERTY, frm, to, ["eventName"], ("eventCount", "sessions"))
    ev_map = {r["dims"][0]: (int(r["metrics"][0]), int(r["metrics"][1])) for r in ev}
    for name in ("add_to_cart", "begin_checkout", "purchase", "view_item", "session_start"):
        cnt, ses_n = ev_map.get(name, (0, 0))
        print(f"  {name}: eventCount={cnt} sessions={ses_n}")
    beh = run_report(GA4_PROPERTY, frm, to, ["date"],
                     ("bounceRate", "screenPageViewsPerSession"))
    if beh:
        br = sum(float(r["metrics"][0]) for r in beh) / len(beh)
        pv = sum(float(r["metrics"][1]) for r in beh) / len(beh)
        print(f"  bounceRate(ср.дн.)={br:.4f} pageViewsPerSession(ср.дн.)={pv:.2f}")


def probe_linear(frm: str, to: str) -> None:
    print(f"\n===== [C] TW linearAll покрытие ({frm}..{to}) =====")
    key = os.environ["GCC_TRIPLEWHALE_API_KEY"]
    shop = os.environ["GCC_TW_SHOP_DOMAIN"]
    data = _tw_post(
        TW_ORDERS_URL,
        {"x-api-key": key, "content-type": "application/json"},
        {"shop": shop, "startDate": frm, "endDate": to, "excludeJourneyData": True},
        timeout=180,
    )
    orders = data.get("ordersWithJourneys") or []
    print(f"заказов: {len(orders)} (totalForRange={data.get('totalForRange')})")
    empty = 0
    n_touch = Counter()
    sources = Counter()
    camp_missing = 0
    camp_total = 0
    for o in orders:
        la = (o.get("attribution") or {}).get("linearAll") or []
        if not la:
            empty += 1
            continue
        n_touch[min(len(la), 10)] += 1
        for tp in la:
            src = (tp.get("source") or "∅")
            sources[src] += 1
            if src not in ("organic_and_social", "Direct", "∅"):
                camp_total += 1
                if not (tp.get("campaignId") or "").strip():
                    camp_missing += 1
    print(f"без linearAll (нужен fallback): {empty} ({100 * empty / len(orders):.1f}%)")
    print(f"число касаний (обрезано на 10): {sorted(n_touch.items())}")
    print("\nsource в касаниях linearAll:")
    for s, n in sources.most_common(15):
        print(f"  {s!r}: {n}")
    print(f"\nплатформенные касания без campaignId: {camp_missing} из {camp_total}")


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    to = date.today() - timedelta(days=1)
    frm = to - timedelta(days=days - 1)
    probe_ga4(frm.isoformat(), to.isoformat())
    probe_linear(frm.isoformat(), to.isoformat())


if __name__ == "__main__":
    main()
