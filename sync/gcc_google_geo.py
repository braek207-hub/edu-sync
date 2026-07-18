# -*- coding: utf-8 -*-
"""sync/gcc_google_geo.py — расход Google Ads кабинета GCC по странам Залива.

Источник — `lime_google_ads_stats` (region='gcc'), куда пишет Google Ads Script
(`docs/integrations/google-ads-ingest-script-gcc.js` в репо EDU v2) через ingest-роут.
Строки там — непересекающиеся срезы: по строке на страну + строка-остаток (country='')
с показами без гео-привязки, поэтому сумма по кампании = полный расход кабинета.

Валюту конвертируем ЗДЕСЬ, а не читаем готовый `cost_rub`: cost_rub заполняет
`sync/google_ads_fx.py` в 07:00 UTC, а GCC-синк идёт в 05:30 — иначе свежий день
приходил бы без расхода и «чинился» только на следующие сутки.
"""
from sync.fx import to_rub as fx_to_rub

CHANNEL = "SEM"
SUBCHANNEL = "Google.Adwords"

SELECT_SQL = """
SELECT COALESCE(country, '')       AS country,
       COALESCE(campaign_id, '')   AS campaign_id,
       COALESCE(campaign_name, '') AS campaign_name,
       COALESCE(cost, 0)           AS cost,
       COALESCE(currency, '')      AS currency
FROM lime_google_ads_stats
WHERE region = 'gcc' AND date = %s::date
"""


def aggregate_geo_spend(db_rows, date_s: str, rate_for) -> list[dict]:
    """Свернуть строки кабинета в расход по стране (в рублях).

    Args:
        db_rows: записи {country, campaign_id, campaign_name, cost, currency} за один день.
        date_s: дата строк (YYYY-MM-DD).
        rate_for: (currency) -> курс к рублю; бросает при неизвестной валюте.

    Returns:
        [{date, country, campaign_id, campaign_name, channel, subchannel, traffic_type, cost}]
        — cost в ₽. country=None для строки-остатка (пустая страна) — так же, как у прочих
        источников без гео-разбивки; такие строки идут только в GCC-тотал.
        campaign_id совпадает с utm_campaign в Метрике (Google Ads пишет туда id через
        ValueTrack), поэтому расход кабинета садится на ту же кампанию, что и её трафик.
    """
    totals: dict[tuple[str | None, str, str], float] = {}
    for row in db_rows:
        cost = float(row.get("cost") or 0)
        if not cost:
            continue
        currency = (row.get("currency") or "").upper()
        try:
            rate = rate_for(currency)
        except Exception:
            # Курса нет — молча считать 1:1 нельзя: это тихая ложь в рублях.
            print(f"gcc_google_geo: WARN валюта {currency!r} ({date_s}) — строка пропущена")
            continue
        country = (row.get("country") or "").strip() or None
        key = (country, (row.get("campaign_id") or "").strip(),
               (row.get("campaign_name") or "").strip())
        totals[key] = totals.get(key, 0.0) + cost * rate

    return [
        {
            "date": date_s,
            "country": country,
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "channel": CHANNEL,
            "subchannel": SUBCHANNEL,
            "traffic_type": "Платный",
            "cost": round(cost_rub, 2),
        }
        for (country, campaign_id, campaign_name), cost_rub in totals.items()
    ]


def fetch_geo_spend(conn, date_s: str) -> list[dict]:
    """Прочитать расход кабинета GCC за день и свернуть по странам (в рублях).

    Пустой список = Script в кабинете GCC ещё не поставлен (или за день нет расхода) →
    вызывающий оставляет расход Google из Triple Whale, без гео-разбивки.
    """
    if conn is None:
        return []
    import psycopg2.extras

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(SELECT_SQL, (date_s,))
        rows = cur.fetchall()
    return aggregate_geo_spend(rows, date_s, lambda cur_code: fx_to_rub(cur_code, date_s))
