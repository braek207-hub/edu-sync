"""Прогноз выручки: ожидаемая выручка на лид + агрегация по сегментам с интервалом."""

_INTERVAL = 0.30  # грубый интервал ±30% (уточним в Ф2)


def expected_revenue(p_pay: float, exp_amount: float, maturation_remaining: float) -> float:
    return float(p_pay) * float(exp_amount) * float(maturation_remaining)


def aggregate_forecast(items) -> list[dict]:
    buckets: dict[str, dict] = {}

    def _add(seg, exp_rev, p_pay):
        b = buckets.setdefault(seg, {"segment": seg, "pending_leads": 0,
                                     "exp_payments": 0.0, "exp_revenue": 0.0})
        b["pending_leads"] += 1
        b["exp_payments"] += float(p_pay)
        b["exp_revenue"] += float(exp_rev)

    for it in items:
        _add(it["segment"], it["exp_rev"], it["p_pay"])
        _add("all", it["exp_rev"], it["p_pay"])

    out = []
    for b in buckets.values():
        rev = b["exp_revenue"]
        b["revenue_lo"] = max(0.0, rev * (1.0 - _INTERVAL))
        b["revenue_hi"] = rev * (1.0 + _INTERVAL)
        out.append(b)
    return out
