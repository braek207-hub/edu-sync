# -*- coding: utf-8 -*-
"""
Э2.0 — пригоден ли p_pay как суррогат оплаты ДЛЯ РЕШЕНИЙ.

Мерим не то, что мерили раньше. AP 0.05 говорит о плохом ранжировании отдельных
лидов; решения строятся на другом — насколько сумма p_pay ПО ГРУППЕ предсказывает
фактические оплаты этой группы. Модель может быть слабой поштучно и пригодной
в агрегате, и наоборот.

Главный вопрос — не «близко ли Σp_pay к факту», а «БЛИЖЕ ЛИ ОНА, ЧЕМ НАИВНАЯ
ОЦЕНКА n_лидов × базовая конверсия». Если не ближе, суррогат не различает группы
и вся его точность — это знание общей базы, которое у нас и так есть. Ровно этот
класс дефекта уже ловили в корректировках: оплаты, размазанные по доле кликов,
давали всем сегментам одинаковую конверсионность.

Зрелость: оплата приходит с лагом, поэтому свежие лиды недосчитаны по факту и
сравнивать их с прогнозом нельзя. Окно созревания берётся ИЗ ДАННЫХ (перцентиль
наблюдаемого лага), а не константой.

Запуск: python probe_surrogate_calibration.py   (нужен DATABASE_URL)
"""

import json
import os
from collections import defaultdict
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

LOOKBACK_DAYS = 540
MATURITY_PERCENTILE = 0.90
# Группы мельче этого по решениям и так не рассматриваются.
MIN_GROUP_LEADS = 30


def _fetch(sql, params):
    with psycopg2.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _as_date(value):
    """Колонка может приехать строкой — сравнение дат тогда молча соврёт."""
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(int(q * len(ordered)), len(ordered) - 1)
    return ordered[idx]


def _corr(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs) ** 0.5
    vy = sum((y - my) ** 2 for y in ys) ** 0.5
    return round(cov / (vx * vy), 4) if vx > 0 and vy > 0 else None


def _rank(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    for pos, i in enumerate(order):
        ranks[i] = float(pos)
    return ranks


def _auc(labels, scores):
    """ROC-AUC через ранги — поштучное качество, для контекста."""
    pos = sum(labels)
    neg = len(labels) - pos
    if pos == 0 or neg == 0:
        return None
    ranks = _rank(scores)
    rank_sum = sum(r for r, y in zip(ranks, labels) if y)
    return round((rank_sum - pos * (pos - 1) / 2) / (pos * neg), 4)


def main() -> int:
    today = date.today()
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()

    rows = _fetch(
        """
        SELECT d.lead_id, d.campaign_id, d.direction, d.created_date, d.payment_date,
               d.is_paid, d.is_eff, d.amount,
               s.p_pay
        FROM crm_lead_details d
        LEFT JOIN edu_lead_scores s
               ON s.lead_id = d.lead_id AND s.scoring_point = 'at_creation'
        WHERE d.created_date >= %s
        """,
        (since,),
    )

    for r in rows:
        r["created_date"] = _as_date(r["created_date"])
        r["payment_date"] = _as_date(r["payment_date"])

    # 1. Лаг оплаты — окно созревания из данных.
    lags = []
    for r in rows:
        if r["is_paid"] and r["payment_date"] and r["created_date"]:
            lags.append((r["payment_date"] - r["created_date"]).days)
    maturity_days = _pct(lags, MATURITY_PERCENTILE) or 0
    mature_before = today - timedelta(days=int(maturity_days))

    scored = [r for r in rows if r["p_pay"] is not None]
    mature = [r for r in scored if r["created_date"] <= mature_before]

    report = {
        "lookback_days": LOOKBACK_DAYS,
        "leads_total": len(rows),
        "leads_scored": len(scored),
        "score_coverage": round(len(scored) / len(rows), 4) if rows else 0,
        "payment_lag_days": {
            "p50": _pct(lags, 0.5), "p90": _pct(lags, 0.9), "p99": _pct(lags, 0.99),
            "n": len(lags),
        },
        "maturity_days": int(maturity_days),
        "leads_mature_scored": len(mature),
    }

    if len(mature) < 100:
        report["verdict"] = "НЕДОСТАТОЧНО ЗРЕЛЫХ ЛИДОВ СО СКОРОМ"
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0

    labels = [1 if r["is_paid"] else 0 for r in mature]
    scores = [float(r["p_pay"]) for r in mature]
    base_rate = sum(labels) / len(labels)

    report["поштучно"] = {
        "auc": _auc(labels, scores),
        "base_rate": round(base_rate, 5),
        "mean_p_pay": round(sum(scores) / len(scores), 5),
        # Смещение в целом: сумма прогноза против суммы факта.
        "total_bias": round((sum(scores) / sum(labels)) if sum(labels) else 0.0, 4),
    }

    # 2. ГЛАВНОЕ: агрегат по группам — Σp_pay против наивной оценки.
    def group_check(key_fn):
        groups = defaultdict(list)
        for r, y, s in zip(mature, labels, scores):
            k = key_fn(r)
            if k is not None:
                groups[k].append((y, s))
        big = {k: v for k, v in groups.items() if len(v) >= MIN_GROUP_LEADS}
        if len(big) < 3:
            return {"groups": len(big), "verdict": "групп мало"}

        err_model, err_naive, facts, preds = [], [], [], []
        for k, items in big.items():
            n = len(items)
            fact = sum(y for y, _ in items)
            pred = sum(s for _, s in items)
            naive = n * base_rate
            facts.append(fact)
            preds.append(pred)
            err_model.append(abs(pred - fact))
            err_naive.append(abs(naive - fact))

        mae_model = sum(err_model) / len(err_model)
        mae_naive = sum(err_naive) / len(err_naive)
        return {
            "groups": len(big),
            "mae_model": round(mae_model, 3),
            "mae_naive": round(mae_naive, 3),
            # < 1 значит модель точнее наивной оценки; ≥ 1 — суррогат
            # не несёт информации о группах сверх общей базы.
            "model_vs_naive": round(mae_model / mae_naive, 4) if mae_naive else None,
            "corr_pred_fact": _corr(preds, facts),
        }

    def month_key(r):
        return str(r["created_date"])[:7]

    report["агрегат"] = {
        "min_group_leads": MIN_GROUP_LEADS,
        "по_кампаниям": group_check(lambda r: str(r["campaign_id"] or "") or None),
        "по_направлениям": group_check(lambda r: r["direction"] or None),
        "по_месяцам": group_check(month_key),
        "по_кампания_месяц": group_check(
            lambda r: (str(r["campaign_id"] or ""), str(r["created_date"])[:7])
            if r["campaign_id"] else None),
    }

    # 3. Воронка: сколько событий на каждой ступени — вход для лестницы Э2.1.
    funnel = _fetch(
        """
        SELECT COUNT(*) AS leads,
               SUM(CASE WHEN is_eff THEN 1 ELSE 0 END) AS eff,
               SUM(CASE WHEN is_connected THEN 1 ELSE 0 END) AS connected,
               SUM(CASE WHEN is_deal THEN 1 ELSE 0 END) AS deals,
               SUM(CASE WHEN is_paid THEN 1 ELSE 0 END) AS paid,
               SUM(COALESCE(amount, 0)) AS revenue
        FROM crm_lead_details
        WHERE created_date >= %s AND created_date <= %s
        """,
        (since, mature_before.isoformat()),
    )[0]
    report["воронка_зрелая"] = {k: (float(v) if k == "revenue" else int(v or 0))
                               for k, v in funnel.items()}

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
