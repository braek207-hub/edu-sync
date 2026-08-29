#!/usr/bin/env python3
"""Сверка MySQL PROCONTEXT (lc_simple_view — источник Power BI) с нашей витриной lime_stats.

Зачем: числа в Power BI и в Panda-BI расходятся, хотя источник у «общих» метрик один.
Скрипт кладёт обе стороны рядом — по периоду, по дням и по группировкам — чтобы стало
видно, где именно расходится: на синке, в классификации каналов или уже на дашборде.

Только чтение: SELECT в MySQL и в Postgres, ни одной записи.

Окно: PROBE_FROM / PROBE_TO (по умолчанию последние 30 дней, заканчивая позавчера).
"""
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import psycopg2
import pymysql

TO = (os.environ.get("PROBE_TO") or "").strip() or (
    datetime.now(timezone.utc) - timedelta(days=2)
).date().isoformat()
FROM = (os.environ.get("PROBE_FROM") or "").strip() or (
    datetime.fromisoformat(TO) - timedelta(days=29)
).date().isoformat()

# Метрики, которые обе стороны обязаны показывать одинаково (расход/клики берутся из
# кабинетов и в сверку не идут).
METRICS = ("sessions", "users", "purchases_count", "purchases_revenue")


def mysql_conn():
    return pymysql.connect(
        host=os.environ["LIME_DB_HOST"],
        port=int(os.environ.get("LIME_DB_PORT") or "3306"),
        db=os.environ["LIME_DB_SCHEMA"],
        user=os.environ["LIME_DB_USER"],
        password=os.environ["LIME_DB_PASSWORD"],
        charset="utf8mb4",
        connect_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )


def fmt(v):
    return f"{round(float(v or 0)):,}".replace(",", " ")


def line(label, my, pg):
    d = float(pg or 0) - float(my or 0)
    base = float(my or 0)
    pct = f"{d / base * 100:+.2f} %" if base else "—"
    print(f"  {label:<22} MySQL {fmt(my):>14} | lime_stats {fmt(pg):>14} | Δ {fmt(d):>12} ({pct})")


def main():
    print(f"[probe] окно {FROM} .. {TO}\n")
    my = mysql_conn()
    pg = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)

    # ── 0. Форма источника ───────────────────────────────────────────────────
    with my.cursor() as c:
        c.execute("SHOW COLUMNS FROM lc_simple_view")
        cols = [r["Field"] for r in c.fetchall()]
    print("=== 0. Колонки lc_simple_view ===")
    print("  " + ", ".join(cols) + "\n")

    # ── 1. Итого за период ───────────────────────────────────────────────────
    agg = ", ".join(f"SUM({m}) AS {m}" for m in METRICS)
    with my.cursor() as c:
        c.execute(
            f"SELECT COUNT(*) AS rows_n, {agg} FROM lc_simple_view WHERE date >= %s AND date <= %s",
            (FROM, TO),
        )
        my_tot = c.fetchone()
    with pg.cursor() as c:
        # Регионы, которые в lime_stats пишут ДРУГИЕ синки (не из этого MySQL), из сверки
        # исключаем — иначе сравнивали бы разные множества.
        c.execute(
            f"""SELECT COUNT(*), {agg} FROM lime_stats
                 WHERE date BETWEEN %s AND %s
                   AND (region IS NULL OR region NOT IN ('gcc','kz_metrika','kz_roistat'))""",
            (FROM, TO),
        )
        pg_tot = c.fetchone()
    print("=== 1. Итого за период (одинаковые метрики) ===")
    print(f"  строк: MySQL {fmt(my_tot['rows_n'])} → lime_stats {fmt(pg_tot[0])} (агрегация синка)")
    for i, m in enumerate(METRICS):
        line(m, my_tot[m], pg_tot[i + 1])

    # ── 2. По дням ───────────────────────────────────────────────────────────
    with my.cursor() as c:
        c.execute(
            f"SELECT date, {agg} FROM lc_simple_view WHERE date >= %s AND date <= %s GROUP BY date",
            (FROM, TO),
        )
        my_days = {str(r["date"]): r for r in c.fetchall()}
    with pg.cursor() as c:
        c.execute(
            f"""SELECT date::text, {agg} FROM lime_stats
                 WHERE date BETWEEN %s AND %s
                   AND (region IS NULL OR region NOT IN ('gcc','kz_metrika','kz_roistat'))
                 GROUP BY date""",
            (FROM, TO),
        )
        pg_days = {r[0]: r for r in c.fetchall()}
    print("\n=== 2. По дням: заказы и выручка ===")
    print(f"  {'дата':<12} {'заказы MySQL':>14} {'заказы наши':>14} {'Δ':>8}  "
          f"{'выручка MySQL':>16} {'выручка наши':>16} {'Δ':>12}")
    for d in sorted(set(my_days) | set(pg_days)):
        m = my_days.get(d)
        p = pg_days.get(d)
        mo = float(m["purchases_count"] or 0) if m else 0
        po = float(p[3] or 0) if p else 0
        mr = float(m["purchases_revenue"] or 0) if m else 0
        pr = float(p[4] or 0) if p else 0
        flag = "" if abs(mo - po) < 0.5 and abs(mr - pr) < 1 else "  ←"
        print(
            f"  {d:<12} {fmt(mo):>14} {fmt(po):>14} {fmt(po - mo):>8}  "
            f"{fmt(mr):>16} {fmt(pr):>16} {fmt(pr - mr):>12}{flag}"
        )

    # ── 3. По регионам ───────────────────────────────────────────────────────
    with my.cursor() as c:
        c.execute(
            f"SELECT region, {agg} FROM lc_simple_view WHERE date >= %s AND date <= %s GROUP BY region",
            (FROM, TO),
        )
        my_reg = {(r["region"] or ""): r for r in c.fetchall()}
    with pg.cursor() as c:
        c.execute(
            f"""SELECT COALESCE(region,''), {agg} FROM lime_stats
                 WHERE date BETWEEN %s AND %s
                   AND (region IS NULL OR region NOT IN ('gcc','kz_metrika','kz_roistat'))
                 GROUP BY 1""",
            (FROM, TO),
        )
        pg_reg = {r[0]: r for r in c.fetchall()}
    print("\n=== 3. По регионам ===")
    for r in sorted(set(my_reg) | set(pg_reg)):
        print(f"  регион '{r or '(пусто)'}'")
        m = my_reg.get(r)
        p = pg_reg.get(r)
        for i, met in enumerate(METRICS):
            line(met, m[met] if m else 0, p[i + 1] if p else 0)

    # ── 4. Группировка каналов: наша classify против сырых source/medium ─────
    # Синк схлопывает (source, medium) в свои channel/subchannel. Если Power BI группирует
    # иначе, разрез разъедется при верном итоге — здесь видно, из чего собран каждый канал.
    with my.cursor() as c:
        c.execute(
            f"""SELECT source, medium, {agg} FROM lc_simple_view
                 WHERE date >= %s AND date <= %s GROUP BY source, medium""",
            (FROM, TO),
        )
        raw = c.fetchall()
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sync.lime import classify

    by_ch = defaultdict(lambda: defaultdict(float))
    for r in raw:
        ch, _sub = classify(r["source"] or "", r["medium"] or "")
        for m in METRICS:
            by_ch[ch][m] += float(r[m] or 0)
    with pg.cursor() as c:
        c.execute(
            f"""SELECT channel, {agg} FROM lime_stats
                 WHERE date BETWEEN %s AND %s
                   AND (region IS NULL OR region NOT IN ('gcc','kz_metrika','kz_roistat'))
                 GROUP BY channel""",
            (FROM, TO),
        )
        pg_ch = {r[0]: r for r in c.fetchall()}
    print("\n=== 4. Каналы: MySQL, разложенный нашей classify, против lime_stats ===")
    for ch in sorted(set(by_ch) | set(pg_ch)):
        p = pg_ch.get(ch)
        print(f"  канал '{ch}'")
        for i, met in enumerate(METRICS):
            line(met, by_ch[ch][met], p[i + 1] if p else 0)

    # ── 5. Топ (source, medium), попавших в «Others» — кандидаты на расхождение ─
    others = [
        r for r in raw if classify(r["source"] or "", r["medium"] or "")[0] == "Others"
    ]
    others.sort(key=lambda r: float(r["sessions"] or 0), reverse=True)
    print("\n=== 5. Топ source/medium, которые наша classify кладёт в «Others» ===")
    for r in others[:15]:
        print(
            f"  {str(r['source'])[:28]:<30} / {str(r['medium'])[:16]:<18} "
            f"визиты {fmt(r['sessions']):>10} заказы {fmt(r['purchases_count']):>8}"
        )

    # ── 6. Готовая группировка PROCONTEXT против нашей ───────────────────────
    # В lc_simple_view есть source_type — собственная группировка источника. Power BI
    # почти наверняка режет по ней, а мы её игнорируем и классифицируем сами. Матрица
    # показывает, где две таксономии расходятся.
    with my.cursor() as c:
        c.execute(
            f"""SELECT source_type, {agg} FROM lc_simple_view
                 WHERE date >= %s AND date <= %s GROUP BY source_type ORDER BY 2 DESC""",
            (FROM, TO),
        )
        st = c.fetchall()
    print("\n=== 6. source_type PROCONTEXT (готовая группировка источника) ===")
    for r in st:
        print(
            f"  {str(r['source_type'])[:30]:<32} визиты {fmt(r['sessions']):>12} "
            f"заказы {fmt(r['purchases_count']):>9} выручка {fmt(r['purchases_revenue']):>15}"
        )

    print("\n=== 7. Матрица source_type × наш channel (заказы) ===")
    with my.cursor() as c:
        c.execute(
            f"""SELECT source_type, source, medium, {agg} FROM lc_simple_view
                 WHERE date >= %s AND date <= %s GROUP BY source_type, source, medium""",
            (FROM, TO),
        )
        cross = c.fetchall()
    mat = defaultdict(lambda: defaultdict(float))
    for r in cross:
        ch, _ = classify(r["source"] or "", r["medium"] or "")
        mat[str(r["source_type"])][ch] += float(r["purchases_count"] or 0)
    for stype in sorted(mat, key=lambda k: -sum(mat[k].values())):
        parts = ", ".join(
            f"{ch} {fmt(v)}" for ch, v in sorted(mat[stype].items(), key=lambda kv: -kv[1]) if v
        )
        print(f"  {stype[:28]:<30} → {parts}")

    my.close()
    pg.close()


if __name__ == "__main__":
    main()
