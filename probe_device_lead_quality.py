# -*- coding: utf-8 -*-
"""
Различается ли КАЧЕСТВО лида по устройствам — или только цена лида.

Вопрос Павла к Э2.2: сегментные корректировки считаются по конверсии клик→лид
из отчёта Директа, а сегмент может давать много лидов с плохим соединением —
тогда выводы противоположные. Прямых данных «устройство → воронка CRM» в срезах
нет: CRM не знает устройство клика. Но есть мост: crm_lead_details.client_id ×
edu_visit_behavior.device_category (Метрика, счётчик vuz).

Мост покрывает только лидов ленда vuz с client_id — это выборка, не генеральная
совокупность. Отчёт обязан показать покрытие ДО выводов: на 10 % лидов выводы
про кабинет не делаются, делаются про сам мост.

Запуск: python probe_device_lead_quality.py   (нужен DATABASE_URL)
"""

import json
from collections import Counter, defaultdict
from datetime import date, timedelta

import psycopg2.extras

from sync.db import get_connection

LOOKBACK_DAYS = 540
MATURITY_PERCENTILE = 0.90


def _fetch(sql, params):
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _as_date(value):
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(int(q * len(ordered)), len(ordered) - 1)]


def _ratio(a, b):
    return round(a / b, 4) if b else None


def _funnel(leads):
    n = len(leads)
    eff = sum(1 for r in leads if r["is_eff"])
    conn = sum(1 for r in leads if r["is_connected"])
    deals = sum(1 for r in leads if r["is_deal"])
    paid = sum(1 for r in leads if r["is_paid"])
    revenue = sum(float(r["amount"] or 0) for r in leads if r["is_paid"])
    return {
        "leads": n, "eff": eff, "connected": conn, "deals": deals, "paid": paid,
        "revenue": round(revenue),
        "p_eff": _ratio(eff, n),
        "p_conn": _ratio(conn, eff),
        "p_deal": _ratio(deals, conn),
        "p_pay": _ratio(paid, deals),
        "avg_check": round(revenue / paid) if paid else None,
        "revenue_per_lead": round(revenue / n) if n else None,
    }


def main() -> int:
    today = date.today()
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()

    leads = _fetch(
        """
        SELECT lead_id, client_id, created_date, payment_date,
               is_eff, is_connected, is_deal, is_paid, amount, direction
        FROM crm_lead_details
        WHERE created_date >= %s
        """,
        (since,),
    )
    # Устройство клиента: самое частое по его визитам (клиент мог ходить с двух).
    visit_rows = _fetch(
        """
        SELECT client_id, device_category, COUNT(*) AS n
        FROM edu_visit_behavior
        WHERE device_category IS NOT NULL
        GROUP BY client_id, device_category
        """,
        (),
    )

    device_votes: defaultdict = defaultdict(Counter)
    for r in visit_rows:
        device_votes[str(r["client_id"])][r["device_category"]] += int(r["n"])
    device_of = {cid: votes.most_common(1)[0][0]
                 for cid, votes in device_votes.items()}

    for r in leads:
        r["created_date"] = _as_date(r["created_date"])
        r["payment_date"] = _as_date(r["payment_date"])

    lags = [
        (r["payment_date"] - r["created_date"]).days
        for r in leads
        if r["is_paid"] and r["payment_date"] and r["created_date"]
    ]
    maturity_days = int(_pct(lags, MATURITY_PERCENTILE) or 0)
    mature_before = today - timedelta(days=maturity_days)
    mature = [r for r in leads if r["created_date"] <= mature_before]

    with_cid = [r for r in mature if r["client_id"]]
    bridged = [r for r in with_cid if str(r["client_id"]) in device_of]

    report = {
        "maturity_days": maturity_days,
        "mature_before": mature_before.isoformat(),
        "coverage": {
            "leads_mature": len(mature),
            "with_client_id": len(with_cid),
            "with_device": len(bridged),
            "bridge_share_of_all": _ratio(len(bridged), len(mature)),
        },
    }

    if len(bridged) < 200:
        report["verdict"] = "МОСТ СЛИШКОМ МАЛ — выводы не делаются"
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0

    # Смещение моста: воронка всех зрелых против воронки мостовых лидов.
    # Если мостовые лиды сами по себе другие, сравнение устройств внутри моста
    # всё ещё честно, а перенос уровней на кабинет — уже нет.
    report["воронка_все_зрелые"] = _funnel(mature)
    report["воронка_мост"] = _funnel(bridged)

    by_device = defaultdict(list)
    for r in bridged:
        by_device[device_of[str(r["client_id"])]].append(r)
    report["по_устройствам"] = {
        device: _funnel(rows) for device, rows in sorted(by_device.items())
    }

    # То же внутри крупнейших направлений — устройство может быть спутано
    # с направлением (разные аудитории ходят с разных устройств).
    dir_counts = Counter(r["direction"] or "?" for r in bridged)
    report["по_направлениям"] = {}
    for direction, _ in dir_counts.most_common(3):
        subset = [r for r in bridged if (r["direction"] or "?") == direction]
        report["по_направлениям"][direction] = {
            device: _funnel(rows)
            for device, rows in sorted(
                (d, [r for r in subset
                     if device_of[str(r["client_id"])] == d])
                for d in by_device
            ) if rows
        }

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
