# -*- coding: utf-8 -*-
"""Зонд W33 (2026-08-10..16): откуда расхождения дашборда с ручным отчётом GCC.

[A] GA4 users на уровне ГРУПП КАНАЛОВ (как ручной файл: дедуп внутри группы) vs наша
    сумма по строкам source/medium/campaign — квантифицировать «гранулярность».
[B] Расход Meta: ads_table по кампаниям (имена! ищем app-кампании) vs summary fb_ads_spend,
    курс AED→RUB по дням. Ручной = 73 386 ₽.
[C] Расход Google: чей источник в lime_stats за W33 — гео-скрипт кабинета (свежий ли?)
    или ga_adCost из TW.

Read-only. Запуск: python -m scripts.probe_gcc_w33_discrepancy
"""
import io
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sync.fx import to_rub  # noqa: E402
from sync.gcc_ga4 import GA4_PROPERTY, run_report  # noqa: E402

FRM, TO = "2026-08-10", "2026-08-16"
GCC_PREFIX = {"ae", "sa", "qa", "kw", "om"}

MANUAL = {"paid": 15879, "meta": 5784, "google": 10095, "org_web": 11412, "total_web": 27291}

_PAID_GROUPS = {"Paid Search", "Paid Social", "Cross-network", "Display",
                "Paid Shopping", "Paid Other", "Paid Video", "Audio"}


def _is_gcc(host):
    return (host or "").strip().lower().split(".")[0] in GCC_PREFIX


def probe_ga4_channel_groups() -> None:
    print(f"===== [A] GA4 users: группы каналов (дедуп как в UI) vs наши строки ({FRM}..{TO}) =====")
    rows = run_report(GA4_PROPERTY, FRM, TO,
                      ["date", "hostName", "sessionDefaultChannelGroup"], ("totalUsers",))
    grp = defaultdict(int)
    for r in rows:
        if not _is_gcc(r["dims"][1]):
            continue
        grp[r["dims"][2]] += int(r["metrics"][0])
    paid = sum(v for g, v in grp.items() if g in _PAID_GROUPS)
    google = grp.get("Paid Search", 0) + grp.get("Cross-network", 0) + grp.get("Paid Shopping", 0)
    meta = grp.get("Paid Social", 0)
    total = sum(grp.values())
    print("группы (totalUsers, сумма дней):")
    for g, v in sorted(grp.items(), key=lambda x: -x[1]):
        print(f"  {g}: {v}")
    print(f"\nСВОДКА групп: total={total} paid={paid} google(PS+CN+Shop)={google} meta(PaidSocial)={meta} org={total - paid}")
    print(f"РУЧНОЙ:        total={MANUAL['total_web']} paid={MANUAL['paid']} google={MANUAL['google']} "
          f"meta={MANUAL['meta']} org={MANUAL['org_web']}")

    # Наш уровень: source/medium/campaign — сумма users по строкам (то, что в lime_stats)
    rows2 = run_report(GA4_PROPERTY, FRM, TO,
                       ["date", "hostName", "sessionSource", "sessionMedium", "sessionCampaignId"],
                       ("totalUsers",))
    from sync.gcc_channels import map_ga4_channel
    ours = defaultdict(int)
    for r in rows2:
        if not _is_gcc(r["dims"][1]):
            continue
        ch, sub, tt = map_ga4_channel(r["dims"][2], r["dims"][3])
        ours[(tt, ch, sub)] += int(r["metrics"][0])
    o_paid = sum(v for (tt, _c, _s), v in ours.items() if tt == "Платный")
    o_meta = ours.get(("Платный", "SMM paid", "Meta Ads"), 0)
    o_goog = ours.get(("Платный", "SEM", "Google.Adwords"), 0)
    print(f"\nНАШИ строки:   total={sum(ours.values())} paid={o_paid} google={o_goog} meta={o_meta}")
    print("→ дельта наши−группы: "
          f"paid {o_paid - paid:+}, meta {o_meta - meta:+}, google {o_goog - google:+} "
          "(это цена дробления на кампании/source при неаддитивных users)")

    # Где сидит гранулярность у Meta: users по кампаниям Paid Social за один день
    day = "2026-08-12"
    rows3 = run_report(GA4_PROPERTY, day, day,
                       ["hostName", "sessionSource", "sessionMedium", "sessionCampaignId"],
                       ("totalUsers",))
    metas = [(r["dims"][3], int(r["metrics"][0])) for r in rows3
             if _is_gcc(r["dims"][0]) and map_ga4_channel(r["dims"][1], r["dims"][2])[1] == "Meta Ads"]
    print(f"\nMeta {day}: строк-кампаний {len(metas)}, сумма users {sum(v for _, v in metas)}")
    grp_day = run_report(GA4_PROPERTY, day, day, ["hostName", "sessionDefaultChannelGroup"], ("totalUsers",))
    ps = sum(int(r["metrics"][0]) for r in grp_day if _is_gcc(r["dims"][0]) and r["dims"][1] == "Paid Social")
    print(f"Paid Social группой за {day}: {ps} (дельта = внутридневной пересчёт одних людей между кампаниями)")


def probe_meta_cost() -> None:
    print(f"\n===== [B] Расход Meta W33: ads_table по кампаниям vs summary vs ручной 73 386 ₽ =====")
    from sync.gcc_triplewhale import fetch_tw_spend
    from sync.gcc_tw_ads import tw_sql
    key, shop = os.environ["GCC_TRIPLEWHALE_API_KEY"], os.environ["GCC_TW_SHOP_DOMAIN"]

    rows = tw_sql(key, shop,
                  "SELECT channel, campaign_id, campaign_name, SUM(spend) AS spend "
                  "FROM ads_table WHERE event_date BETWEEN @startDate AND @endDate "
                  "GROUP BY channel, campaign_id, campaign_name", FRM, TO)
    fb = [r for r in rows if "facebook" in (r.get("channel") or "").lower()]
    fb_total_aed = sum(float(r.get("spend") or 0) for r in fb)
    print(f"facebook-ads кампаний: {len(fb)}, spend AED {fb_total_aed:.0f}")
    for r in sorted(fb, key=lambda x: -float(x.get("spend") or 0)):
        print(f"  {str(r.get('campaign_name'))[:58]:<58} {float(r.get('spend') or 0):>9.1f} AED")

    # дневной курс — как в синке
    day = date.fromisoformat(FRM)
    rub = 0.0
    day_rows = tw_sql(key, shop,
                      "SELECT event_date, SUM(spend) AS spend FROM ads_table "
                      "WHERE channel LIKE '%facebook%' AND event_date BETWEEN @startDate AND @endDate "
                      "GROUP BY event_date", FRM, TO)
    for r in day_rows:
        d = str(r.get("event_date"))[:10]
        rub += float(r.get("spend") or 0) * to_rub("AED", d)
    print(f"\nfb ads_table в ₽ по дневному курсу: {rub:.0f} (ручной 73 386; наш lime_stats 94 032)")

    # summary-page для сверки (там магазинный fb_ads_spend)
    s_total = 0.0
    d = date.fromisoformat(FRM)
    while d <= date.fromisoformat(TO):
        m = fetch_tw_spend(key, shop, d.isoformat())
        s_total += float(m.get("fb_ads_spend") or 0) * to_rub("AED", d.isoformat())
        d += timedelta(days=1)
    print(f"fb summary-page (fb_ads_spend) в ₽: {s_total:.0f}")


def probe_google_cost_source() -> None:
    print(f"\n===== [C] Google-расход W33: гео-скрипт кабинета vs TW =====")
    import psycopg2
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL нет локально — пропуск (смотрим только TW)")
    else:
        conn = psycopg2.connect(dsn.split("?")[0], connect_timeout=30)
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(date), SUM(cost_rub) FROM lime_google_ads_stats "
                        "WHERE region='gcc' AND date BETWEEN %s AND %s", (FRM, TO))
            print("lime_google_ads_stats gcc W33:", cur.fetchone())
        conn.close()
    from sync.gcc_tw_ads import tw_sql
    key, shop = os.environ["GCC_TRIPLEWHALE_API_KEY"], os.environ["GCC_TW_SHOP_DOMAIN"]
    g = tw_sql(key, shop,
               "SELECT SUM(spend) AS spend FROM ads_table WHERE channel LIKE '%google%' "
               "AND event_date BETWEEN @startDate AND @endDate", FRM, TO)
    aed = float(g[0].get("spend") or 0) if g else 0
    print(f"google ads_table W33: {aed:.0f} AED ≈ {aed * to_rub('AED', TO):.0f} ₽ (ручной 144 988; наш 141 914)")


def main() -> None:
    probe_ga4_channel_groups()
    probe_meta_cost()
    probe_google_cost_source()


if __name__ == "__main__":
    main()
