# -*- coding: utf-8 -*-
"""
sync/agent/pacing.py — план освоения месяца вместо трейлинга-28.

Σ целевых бюджетов кабинета была прибита к факту прошедших 28 дней: сколько
кабинет потратил, столько же он и планировал, плюс шаг роста. Для сезонного
бизнеса — а образование сезонно — это систематическая ошибка: на подъёме
агент недоливает (план тянет назад, в низкий месяц), на спаде переливает.

Здесь план берётся у ЧЕЛОВЕКА: месячный потолок освоения из панели настроек
(`monthly_budget_cap_rub`). Пейсинг не решает, сколько тратить, — он
раскладывает НЕВЫБРАННЫЙ остаток потолка по оставшимся дням месяца. Отсюда
два свойства, которых у трейлинга не было:

* месяц, начатый вяло, догоняется: остаток делится на меньшее число дней;
* месяц, обогнавший план, тормозится сам, без отдельного сторожа.

Потолка нет — пейсинга нет: `target_rub is None`, и солвер ведёт себя как
раньше (рост считается и печатается числом, сумма кабинета не меняется).
Выдуманный план здесь означал бы, что агент назначил себе бюджет сам.

Режим спроса (`demand.demand_regime`) двигает ТЕМП, но не потолок: в подъём
деньги нужны сегодня, в спад они дешевле завтра. Потолок — деньги владельца,
и рынок его не двигает.
"""

import calendar
from datetime import date
from typing import Any, Dict, Optional

from sync.agent.demand import REGIME_FALL, REGIME_NORMAL, REGIME_RISE

# Откуда взялся план: потолок панели или его отсутствие. Строкой, а не
# булевым флагом: в отчёте «плана нет» и «план 0 ₽» — разные утверждения.
BASIS_CAP = "monthly_cap"
BASIS_NO_CAP = "no_cap"

# Ограничитель роста, названный пейсингом. Отдельно от "monthly_cap": ровный
# потолок и темп освоения лечатся по-разному — первый поднимают, второму
# хватает нескольких дней, чтобы отпустить самому.
CAPPED_BY_PACING = "pacing"

# Насколько режим спроса двигает дневную долю. Числа скромные намеренно:
# доля — это ТЕМП внутри уже названного потолка, и агрессивный множитель
# просто выбрал бы месяц за две недели, оставив спрос без денег на хвосте.
REGIME_MULTIPLIER: Dict[str, float] = {
    REGIME_RISE: 1.25,
    REGIME_FALL: 0.80,
}


def month_bounds(month: str) -> tuple:
    """Первый и последний день месяца 'ГГГГ-ММ'."""
    year, mon = (int(part) for part in str(month).split("-")[:2])
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def days_left(month: str, today: Optional[date] = None) -> int:
    """Сколько дней месяца ещё не прожито, СЕГОДНЯШНИЙ включительно.

    Сегодня входит в остаток: день ещё идёт, и деньги на него ещё не
    потрачены. Исключить его значило бы каждый день недодавать одну долю.
    """
    first, last = month_bounds(month)
    today = today or date.today()
    if today > last:
        return 0
    if today < first:
        return (last - first).days + 1
    return (last - today).days + 1


def month_plan(month: str, spent_to_date: float, cap_rub: Optional[float],
               demand_regime: Optional[str] = None,
               today: Optional[date] = None) -> Dict[str, Any]:
    """План освоения месяца: цель, остаток и дневная доля.

    spent_to_date — факт месяца ДО сегодняшнего дня по этому кабинету.
    cap_rub — потолок из панели; None означает «плана нет», а не «ноль».
    demand_regime — режим спроса кабинета (`dominant_regime`); незнакомое
    значение и «мало данных» темп не двигают.
    """
    left = days_left(month, today)
    plan: Dict[str, Any] = {
        "month": str(month),
        "target_rub": None,
        "spent_to_date": round(float(spent_to_date or 0.0), 2),
        "remaining_rub": None,
        "days_left": left,
        "daily_allowance": None,
        "even_allowance": None,
        "regime": demand_regime,
        "regime_multiplier": 1.0,
        "basis": BASIS_NO_CAP,
    }
    if cap_rub is None:
        return plan

    target = float(cap_rub)
    # Перерасход — это ноль остатка, а не отрицательные деньги: минус уехал бы
    # в потолок окна солвера и прочитался бы там как команда резать кабинет,
    # которую агент принимать не имеет права.
    remaining = max(0.0, target - float(spent_to_date or 0.0))
    even = (remaining / left) if left > 0 else 0.0
    multiplier = REGIME_MULTIPLIER.get(str(demand_regime), 1.0)
    plan.update({
        "target_rub": round(target, 2),
        "remaining_rub": round(remaining, 2),
        "even_allowance": round(even, 2),
        "regime_multiplier": multiplier,
        # Сдвиг темпа не имеет права вынести за потолок: остаток — это всё,
        # что осталось от плана месяца, и «сегодня можно больше остатка» было
        # бы разрешением его перебрать.
        "daily_allowance": round(min(even * multiplier, remaining), 2),
        "basis": BASIS_CAP,
    })
    return plan


def dominant_regime(regimes: Optional[Dict[str, Dict[str, Any]]]) -> str:
    """Режим спроса кабинета: за кем большинство направлений.

    Потолок один на кабинет, а режим меряется по направлениям, и темп нужен
    один. Считаются только направления с настоящим вердиктом: «мало данных»
    и «нет ряда» — это отсутствие замера, и приравнивать их к спаду значило бы
    тормозить новое направление за то, что у него нет истории.

    Ничья между подъёмом и спадом — не режим: темп по такому большинству был
    бы монеткой.
    """
    rise = fall = 0
    for row in (regimes or {}).values():
        regime = str((row or {}).get("regime"))
        rise += regime == REGIME_RISE
        fall += regime == REGIME_FALL
    if rise > fall:
        return REGIME_RISE
    if fall > rise:
        return REGIME_FALL
    return REGIME_NORMAL
