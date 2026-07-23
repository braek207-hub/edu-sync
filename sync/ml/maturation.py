"""Эмпирическая кривая созревания оплат (доля оплат, наступивших к возрасту когорты).
Корректирует прогноз выручки молодых когорт: у свежих лидов часть оплат ещё впереди."""


def maturation_table(paid_days_to_pay: list[int], horizon: int = 120) -> list[tuple[int, float]]:
    clean = [d for d in paid_days_to_pay if d is not None and d >= 0]
    total = len(clean)
    out: list[tuple[int, float]] = []
    for age in range(horizon + 1):
        if total == 0:
            out.append((age, 0.0))
            continue
        frac = sum(1 for d in clean if d <= age) / total
        out.append((age, frac))
    return out
