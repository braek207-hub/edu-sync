# -*- coding: utf-8 -*-
"""
sync/agent/writer/risk.py — риск-бюджет движка записи (слой 4 защиты).

Идея из спеки: апрув — слабый предохранитель, потому что человек, которому
двадцать раз в неделю показывают «подтвердить?», через месяц штампует не глядя.
Вместо него — потолок денег под непроверенными изменениями.

Цена ошибки = деньги, которые ЭТО изменение ставит под удар, × дни до замера.
Обоими множителями управляем инженерно: первый считает exposure.py по каждому
рычагу отдельно, дни до замера — наша настройка. Худший случай посчитан заранее:
даже если КАЖДОЕ активное изменение окажется вредным, потери ограничены
недельным лимитом.

Прежде первым множителем был весь дневной расход кампании, одинаковый для
любого действия. Это завышало риск в разы и стоило темпа: одна корректировка
сегмента съедала 38 876 ₽ из 50 000 недельного лимита (кампания 114057545,
неделя 2026-08-17), агент делал одно-два действия в неделю, за всё время
применил девять. Разбор дефекта и правильная арифметика — в exposure.py.

Гарантия при этом не ослаблена, потому что рядом стоит ПОТОЛОК ОБЪЕКТА: сумма
дельт по кампании за окно не может превысить её расход за горизонт замера.
Больше, чем кампания тратит, на ней не потерять, сколько бы правок в неё ни
внесли; а внутри этого потолка каждое действие платит ровно свою дельту, а не
цену всей кампании.

Второй счёт — по ТАКТУ, а не по действию (net_risk). Перенос денег внутри
кабинета платил обеими сторонами сразу: полоса перераспределения заявила
921 690 ₽ риска на 48 действиях — 16 % недельного расхода кабинета за неделю
переносов, при том что сумма кабинета не изменилась ни на рубль. Под ударом
при переносе разрыв окупаемостей сторон, а не перенесённая сумма и тем более
не удвоенная; поштучный action_risk этого не видит по построению — он знает
одно действие, а встречное движение денег видно только на наборе.

Расход известен не для всех кампаний (лаг синка, новая кампания, пробел в
источнике). Молчаливый ноль для таких случаев — дыра в гарантии: нулевой риск
означает «бюджет не нужен», а на деле расход просто неизвестен. Различаем:
кампания есть в справочнике со значением 0 — риск честно 0 (тратить нечего);
кампании нет в справочнике — расход оценивается консервативно, по медиане
известных дневных расходов; справочник пуст целиком — оценить неоткуда,
риск = +inf, что гарантированно не проходит fit_into_budget и уходит в
отложенные, а не пропускается бесплатно.
"""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sync.agent.writer import exposure as exposure_mod
from sync.agent.writer import expectation

DEFAULT_DAYS_TO_MEASURE = 7

# Абсолютный недельный потолок риска — тот, что стоял в коде до перехода на
# долю. Остаётся ровно одним: аварийным полом на случай, когда недельный
# расход кабинета неизвестен (см. weekly_limit).
DEFAULT_WEEKLY_RISK_RUB = 50_000.0

# Доля недельного расхода кабинета, которую агенту позволено держать под
# непроверенными изменениями. 1 % — примерно то, чем константа 50 000 ₽ была
# при недельном расходе кабинета 5,7 млн ₽ (замер 26.08.2026): переход на
# долю поведения не меняет, но перестаёт зависеть от размера кабинета.
DEFAULT_RISK_SHARE_WEEK = 0.01

# Где у действия лежит его предельная окупаемость. Имя — то же, что у солвера
# портфеля (portfolio.computed_rows: marginal_roi), потому что число ровно то
# же: второй ключ означал бы второй источник правды о том, чем кампания
# окупается.
ROI_KEY = "marginal_roi"


def weekly_limit(
    weekly_spend_rub: float,
    share: float = DEFAULT_RISK_SHARE_WEEK,
    absolute_override: Optional[float] = None,
) -> float:
    """Недельный потолок риска: доля расхода кабинета, а не константа.

    Константа 50 000 ₽ была 0,9 % недельного расхода кабинета в 5,7 млн ₽
    (замер 26.08.2026) — и молча меняла смысл вместе с кабинетом: вырос
    расход вдвое, и агент вдвое зажат; упал вчетверо, и тот же лимит
    разрешает ставить под удар 3,5 % денег недели. Доля этой связи не теряет.

    absolute_override — значение, которое человек поставил руками
    (risk_budget_week в LOCKED_KEYS панели). Оно перебивает долю: смысл
    ручного потолка в том, чтобы не зависеть ни от какой арифметики.

    Расход неизвестен (0 или пусто — пробел в витрине, лаг синка) —
    возвращается DEFAULT_WEEKLY_RISK_RUB, а НЕ ноль. Ноль означал бы «агент
    не работает» при первом же пробеле: прогон отложил бы всё до единого
    действия, и отчёт выглядел бы точно так же, как у исправно остановленного
    агента. Заметить такое можно только сверкой с витриной, то есть никогда.
    Ноль от нулевой доли (share=0.0 в панели) — другое дело: это решение
    человека, и оно исполняется.
    """
    if absolute_override is not None:
        return float(absolute_override)
    spend = float(weekly_spend_rub or 0.0)
    if spend <= 0.0:
        return DEFAULT_WEEKLY_RISK_RUB
    return round(spend * float(share), 2)


def week_start(today_iso: str) -> str:
    d = datetime.fromisoformat(str(today_iso)).date() if not isinstance(today_iso, date) else today_iso
    return (d - timedelta(days=d.weekday())).isoformat()


DAYS_IN_WEEK = 7


def paced_allowance(remaining_rub: float, today_iso: str, week_start_iso: str,
                    days_in_week: int = DAYS_IN_WEEK) -> float:
    """Сколько недельного риска прогону позволено занять СЕГОДНЯ.

    Недельный лимит был потолком на неделю, но не на прогон: один прогон
    вправе был занять его целиком. С переходом на дельта-цены действия
    подешевели в разы, и лимит действий стал пропускать столько правок, что
    понедельничный прогон выбирал почти весь недельный риск, — а вторник и
    все дни после него оставались без бюджета. Это не безопасность, а
    случайность порядка сортировки: важное, замеченное в среду, ждало бы
    следующей недели за спиной у неважного, замеченного в понедельник.

    Деление на число ОСТАВШИХСЯ дней недели самокорректируется: неистраченное
    переходит вперёд (остаток тот же, делителей меньше), а в последний день
    недели доступен весь остаток. Потолок недели при этом не растёт — это
    по-прежнему он, просто выдаётся долями.
    """
    d = date.fromisoformat(str(today_iso))
    start = date.fromisoformat(str(week_start_iso))
    days_left = days_in_week - (d - start).days
    return float(remaining_rub) / max(1, min(days_in_week, days_left))


def median(values) -> Optional[float]:
    """Медиана списка чисел. None, если список пуст — оценивать не от чего.

    Общий приём для консервативной оценки там, где для конкретного объекта
    нет собственных данных: половина известных объектов ниже медианы,
    половина — выше, оценка не занижена систематически в сторону нуля.
    Используется и для дневного расхода (median_daily_cost ниже), и для
    базового CPA красной линии (sync/agent_e1.py).
    """
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return None
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def median_daily_cost(daily_cost_by_campaign: Dict[str, float]) -> Optional[float]:
    """Медиана дневного расхода по известным кампаниям.

    Консервативная оценка для кампании без данных: половина известных
    кампаний тратит меньше, половина — больше, оценка не занижена
    систематически в сторону нуля. None, если справочник пуст —
    оценивать не от чего.
    """
    return median(daily_cost_by_campaign.values())


def object_daily_cost(
    action: Dict[str, Any],
    daily_cost_by_campaign: Dict[str, float],
) -> float:
    """Дневной расход объекта действия. +inf, если оценить не от чего.

    Кампания есть в справочнике — берём её расход как есть (в т.ч. честный
    0.0, если расхода нет). Кампании нет в справочнике — расход неизвестен,
    а не нулевой: берём консервативную оценку median_daily_cost по известным
    кампаниям. Если справочник пуст целиком — возвращаем +inf: действие с
    такой ценой fit_into_budget никогда не пропустит внутрь бюджета, оно
    уйдёт в отложенные вместо того, чтобы молча стоить 0.
    """
    object_id = str(action.get("object_id"))
    if object_id in daily_cost_by_campaign:
        return float(daily_cost_by_campaign[object_id])
    fallback = median_daily_cost(daily_cost_by_campaign)
    if fallback is None:
        return float("inf")
    return float(fallback)


def action_risk(
    action: Dict[str, Any],
    daily_cost_by_campaign: Dict[str, float],
    days_to_measure: int = DEFAULT_DAYS_TO_MEASURE,
) -> float:
    """Сколько денег максимум уйдёт неоптимально до момента замера.

    Дельта действия (exposure.daily_rub) × горизонт замера, но не больше
    расхода самого объекта за тот же горизонт: потерять на кампании больше,
    чем она тратит, нельзя — а рычаг бюджета «вверх» ограничен своим капом
    ±20 % от расхода, так что этот потолок ему не жмёт.
    """
    daily = object_daily_cost(action, daily_cost_by_campaign)
    if daily == float("inf"):
        return float("inf")
    at_risk, _ = exposure_mod.daily_rub(action.get("exposure"), daily)
    return round(min(at_risk, daily) * days_to_measure, 2)



def net_risk(
    actions: List[Dict[str, Any]],
    daily_cost_by_campaign: Dict[str, float],
    days: int = DEFAULT_DAYS_TO_MEASURE,
) -> Dict[str, float]:
    """Цена НАБОРА действий одного такта с учётом взаимной компенсации.

    Дефект 8б плана беты: перенос платил дважды. Сдвиг 100 000 ₽ с кампании A
    на кампанию B стоил риском обе дельты — и −100 000 у A, и +100 000 у B, —
    хотя сумма кабинета не изменилась ни на рубль. Замер Б: полоса
    перераспределения заявила 921 690 ₽ на 48 действиях, то есть 16 %
    недельного расхода кабинета за одну неделю переносов.

    Под ударом при переносе не 200 000 ₽ и даже не 100 000, а РАЗРЫВ
    ОКУПАЕМОСТЕЙ сторон: ошибись мы, и перенесённые деньги зарабатывают по
    ставке донора вместо ставки получателя. Отсюда цена компенсированной части
    = перенесённая сумма × относительный разрыв, и платится она ОДИН раз на
    пару (поровну обеими сторонами), а не каждой стороной целиком.

    Компенсация действует только на ту часть, которая реально скомпенсирована:
    если такт даёт кабинету больше, чем забирает, разница — новые деньги под
    ударом, и они платят как доливка. Разрыв неизвестен хотя бы у одной
    стороны — компенсации нет вовсе: та же дисциплина, что у
    object_daily_cost, неизвестное не ноль.

    Компенсируют друг друга ТОЛЬКО действия риск-платящих полос. Гигиена
    вырезает расход, риском не платит и скидки не даёт: иначе сломанный
    источник данных, нагенерив минус-фраз, делал бы любую доливку бесплатной.

    Неопределённая цена (+inf) остаётся неопределённой: компенсация — скидка
    за встречное движение денег, а не способ превратить «расход неизвестен» в
    конечное число.
    """
    gross = {a["idempotency_key"]: action_risk(a, daily_cost_by_campaign, days)
             for a in actions}
    moved = {a["idempotency_key"]: _moved_rub(a, days) for a in actions}

    given = [a for a in actions if (moved[a["idempotency_key"]] or 0.0) < 0.0]
    taken = [a for a in actions if (moved[a["idempotency_key"]] or 0.0) > 0.0]
    given_rub = sum(-(moved[a["idempotency_key"]] or 0.0) for a in given)
    taken_rub = sum((moved[a["idempotency_key"]] or 0.0) for a in taken)
    compensated = min(given_rub, taken_rub)

    if compensated <= 0.0:
        return {key: round(value, 2) for key, value in gross.items()}

    gap = _efficiency_gap(given, taken, moved)
    prices: Dict[str, float] = {}
    for action in actions:
        key = action["idempotency_key"]
        price = gross[key]
        if price == float("inf"):
            prices[key] = price
            continue
        delta = moved[key] or 0.0
        side_rub = taken_rub if delta > 0 else given_rub
        share = 0.0 if (delta == 0.0 or side_rub <= 0.0) else compensated / side_rub
        # Половина — потому что компенсированную часть платит ПАРА, а не
        # каждая сторона: доли донора и получателя в сумме дают два переноса,
        # умножение на 0.5 возвращает их к одному.
        prices[key] = round(price * (1.0 - share) + price * share * gap / 2.0, 2)
    return prices


def _moved_rub(action: Dict[str, Any], days: int) -> Optional[float]:
    """Сколько рублей действие добавляет кабинету (+) или снимает (−) за окно.

    Знак берётся из обещания рычага (writer/expectation.of), а не из цены
    риска: цена — модуль дельты, по ней донора от получателя не отличить.
    Обещание приведено к СВОЕМУ горизонту (у полосы перераспределения он
    вдвое длиннее недельного), поэтому здесь оно пересчитывается на горизонт
    такта — иначе компенсировались бы суммы, посчитанные на разных окнах.

    None — действие в зачёт не идёт: либо его полоса риском не платит, либо
    рычаг ничего не обещает, и сколько денег он двигает, неизвестно.
    """
    if not _nets(action):
        return None
    exp = expectation.of(action)
    if exp is None:
        return None
    horizon = float(exp.get("measure_days") or 0.0)
    rub = float(exp.get("rub_delta") or 0.0)
    if horizon <= 0.0 or rub == 0.0:
        return None
    return rub / horizon * float(days)


def _nets(action: Dict[str, Any]) -> bool:
    """Участвует ли действие во взаимозачёте такта.

    Импорт полос ВНУТРИ функции: кольцо lanes → switch → expectation →
    (лениво) lanes уже существует, и модульный импорт отсюда добавил бы ему
    третью сторону — порядок импортов начал бы решать, соберётся ли пакет.

    Вид без полосы (lane_of падает) во взаимозачёт не идёт: у него нет ни
    лимита, ни срока замера, и зачесть его дельту значило бы дать скидку за
    движение денег, о котором движок ничего не знает.
    """
    from sync.agent.writer import lanes

    try:
        return lanes.lane_of(action) in lanes.RISK_PAYING_LANES
    except ValueError:
        return False


def _efficiency_gap(given: List[Dict[str, Any]], taken: List[Dict[str, Any]],
                    moved: Dict[str, Optional[float]]) -> float:
    """Относительный разрыв окупаемостей сторон переноса: от 0 до 1.

    Ноль — стороны окупаются одинаково, и ошибка переноса не стоит ничего:
    деньги зарабатывают ту же ставку там, куда их перенесли. Единица — одна
    из сторон не окупается вовсе или её окупаемость неизвестна; тогда
    компенсации нет и перенос платит полную цену.

    Окупаемости сторон взвешены по перенесённым рублям: крупный сдвиг
    определяет разрыв сильнее мелкого, иначе одна копеечная правка с дикой
    окупаемостью задавала бы цену всему такту.
    """
    give_roi = _weighted_roi(given, moved)
    take_roi = _weighted_roi(taken, moved)
    if give_roi is None or take_roi is None:
        return 1.0
    top = max(give_roi, take_roi)
    if top <= 0.0:
        return 1.0
    return max(0.0, min(1.0, abs(take_roi - give_roi) / top))


def _weighted_roi(actions: List[Dict[str, Any]],
                  moved: Dict[str, Optional[float]]) -> Optional[float]:
    """Средняя окупаемость стороны, взвешенная по её рублям. None — неизвестна.

    Хватает ОДНОГО действия без окупаемости, чтобы сторона стала неизвестной:
    средняя по остальным выдала бы догадку за измерение — ровно та подмена,
    против которой стоят +inf в object_daily_cost и «весь объект» при
    неизвестной доле сегмента.
    """
    total = 0.0
    weight = 0.0
    for action in actions:
        roi = action.get(ROI_KEY)
        if roi is None:
            roi = (action.get("payload") or {}).get(ROI_KEY)
        if roi is None:
            return None
        rub = abs(moved[action["idempotency_key"]] or 0.0)
        total += float(roi) * rub
        weight += rub
    if weight <= 0.0:
        return None
    return total / weight


def action_risk_basis(
    action: Dict[str, Any],
    daily_cost_by_campaign: Dict[str, float],
) -> str:
    """Почему цена действия такая — строкой для отчёта прогона.

    Без неё дельта-модель непроверяема: число в журнале не говорит, посчитана
    ли доля сегмента или молча взят весь объект.
    """
    daily = object_daily_cost(action, daily_cost_by_campaign)
    if daily == float("inf"):
        return "дневной расход неизвестен и оценить не от чего"
    _, basis = exposure_mod.daily_rub(action.get("exposure"), daily)
    return basis


def object_cap(
    action: Dict[str, Any],
    daily_cost_by_campaign: Dict[str, float],
    days_to_measure: int = DEFAULT_DAYS_TO_MEASURE,
) -> float:
    """Потолок риска объекта за окно: его расход за горизонт замера.

    Это то, что раньше платило КАЖДОЕ действие. Теперь это предел суммы всех
    действий по объекту: дельты складываются, пока не упрутся в цену объекта
    целиком, и дальше объект становится бесплатным — хуже, чем «вся кампания
    ошибочна», уже не будет.
    """
    daily = object_daily_cost(action, daily_cost_by_campaign)
    if daily == float("inf"):
        return float("inf")
    return round(daily * days_to_measure, 2)


def risk_object(action: Dict[str, Any]) -> str:
    """Единица риска — ОБЪЕКТ, на который действие влияет, а не само действие."""
    return f"{action.get('object_level')}:{action.get('object_id')}"


def fit_into_budget(
    actions: List[Dict[str, Any]],
    risks: Dict[str, float],
    remaining_rub: float,
    charged_by_object: Optional[Dict[str, float]] = None,
    caps: Optional[Dict[str, float]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Берёт действия по порядку, пока хватает бюджета. Остальное откладывает.

    Каждое действие платит свою дельту (risks), но сумма списаний по одному
    объекту ограничена его потолком (caps): дойдя до цены объекта целиком,
    следующие правки той же кампании проходят бесплатно. Прежде «бесплатным»
    было всё, кроме первого действия по объекту, а первое платило за всю
    кампанию — модель, при которой одно касание съедало три четверти
    недельного лимита.

    charged_by_object — сколько по объекту уже списано (журнал прошлых
    прогонов + этот прогон). Словарь ДОПОЛНЯЕТСЯ по ходу: вызывающий код
    передаёт один и тот же словарь на все кабинеты прогона, поэтому счёт по
    объекту сквозной, а не покабинетный.

    Списание происходит только при фактическом попадании в бюджет: действие,
    ушедшее в отложенные, счёт объекта не двигает — иначе следующее действие
    по тому же объекту прошло бы за счёт так и не применённого первого.

    Каждому действию в fits проставлен risk_rub — ровно та сумма, которую
    прогон за него заплатил. Она же уходит в журнал: по ней считается
    spent_risk следующего прогона.
    """
    charged: Dict[str, float] = charged_by_object if charged_by_object is not None else {}
    limits: Dict[str, float] = caps or {}
    fits: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    budget = float(remaining_rub)
    for action in actions:
        obj = risk_object(action)
        cap = float(limits.get(obj, float("inf")))
        already = float(charged.get(obj, 0.0))
        headroom = max(0.0, cap - already)
        cost = min(float(risks.get(action["idempotency_key"], 0.0)), headroom)
        if cost <= budget:
            fits.append({**action, "risk_rub": round(cost, 2)})
            budget -= cost
            charged[obj] = already + cost
        else:
            deferred.append(action)
    return fits, deferred
