# -*- coding: utf-8 -*-
"""sync/google_ads_fx.py — расход Google Ads → рубли (колонка cost_rub).

Google Ads отдаёт расход в валюте аккаунта (KZ-аккаунт LIME = USD). Дашборд считает в рублях,
поэтому конвертируем ЗАРАНЕЕ здесь (курс ЦБ, sync/fx.py), а не в рантайме дашборда:
хендлер читает готовый cost_rub и джойнит его к строкам lime_stats по (date, campaign_id) —
так же, как расход кабинета Директа для РФ.

RUB остаётся как есть; валюты из CBR_IDS (USD/AED) — по курсу на дату; прочие не гадаем (NULL).
ENV: DATABASE_URL, GOOGLE_ADS_FX_DAYS_BACK (default 30). Запуск: python -m sync.google_ads_fx
"""
import os
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

from sync.fx import CBR_IDS, to_rub as fx_to_rub

DAYS_BACK = int(os.environ.get("GOOGLE_ADS_FX_DAYS_BACK") or "30")
BACKFILL = (os.environ.get("GOOGLE_ADS_FX_BACKFILL") or "").strip() == "1"

_SELECT_BASE = (
    "SELECT DISTINCT date::text AS date, COALESCE(currency, 'USD') AS currency "
    "FROM lime_google_ads_stats"
)


def build_pairs_query(backfill: bool, frm: str, to: str) -> tuple:
    """Пары (дата, валюта) для конвертации.

    backfill=True — вся история, только незаполненные строки. Нужен потому, что шаг
    конвертации появился в workflow позже самих данных: с окном 30 дней история
    с июня 2025 навсегда осталась бы без cost_rub, а дашборд читает именно его.
    Условие cost_rub IS NULL заодно делает шаг самозалечивающимся — пропущенный
    день подхватится следующим прогоном, а не потеряется молча.
    """
    if backfill:
        return f"{_SELECT_BASE} WHERE cost_rub IS NULL", ()
    return f"{_SELECT_BASE} WHERE date >= %s AND date <= %s", (frm, to)

UPDATE_SQL = """
UPDATE lime_google_ads_stats
SET cost_rub = ROUND((cost * %s)::numeric, 2)
WHERE date = %s::date AND COALESCE(currency, 'USD') = %s
"""

UPDATE_RUB_SQL = """
UPDATE lime_google_ads_stats
SET cost_rub = ROUND(cost::numeric, 2)
WHERE date = %s::date AND COALESCE(currency, 'USD') = %s
"""


def sync_google_ads_fx() -> int:
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL не задан")
    to = date.today()
    frm = to - timedelta(days=DAYS_BACK)
    frm_s, to_s = frm.isoformat(), to.isoformat()

    conn = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    updated = 0
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sql, params = build_pairs_query(BACKFILL, frm_s, to_s)
            cur.execute(sql, params)
            pairs = cur.fetchall()

        failed = 0
        with conn.cursor() as cur:
            for p in pairs:
                cur_code = (p["currency"] or "").upper()
                if cur_code in ("RUB", "RUR", ""):
                    cur.execute(UPDATE_RUB_SQL, (p["date"], p["currency"]))
                elif cur_code in CBR_IDS:
                    # Недоступность ЦБ на ОДНУ дату не должна ронять прогон: раньше
                    # ConnectTimeout здесь убивал весь workflow вместе с шагами после
                    # него. Пропущенная дата подхватится следующим прогоном — строка
                    # остаётся с cost_rub IS NULL, а режим backfill её и выбирает.
                    try:
                        rate = fx_to_rub(cur_code, p["date"])
                    except Exception as e:
                        failed += 1
                        print(f"google_ads_fx: WARN курс {cur_code} на {p['date']} не получен: {e}")
                        continue
                    cur.execute(UPDATE_SQL, (rate, p["date"], p["currency"]))
                else:
                    print(f"google_ads_fx: WARN валюта {cur_code!r} ({p['date']}) — cost_rub не заполнен")
                    continue
                updated += cur.rowcount
        conn.commit()
        if failed:
            print(f"google_ads_fx: WARN {failed} дат без курса — подхватятся следующим прогоном")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"google_ads_fx: {frm_s}..{to_s} → cost_rub обновлён в {updated} строках ({len(pairs)} пар дата/валюта)")
    return updated


if __name__ == "__main__":
    sync_google_ads_fx()
