# -*- coding: utf-8 -*-
"""
sync/agent/writer/expectation.py — что рычаг ОБЕЩАЕТ, применяя действие.

До 26.08.2026 ожидание заявляли два вида действий из девяти: budget.set и
budget.set_daily получали его готовым от солвера портфеля
(portfolio.computed_rows → budget._expectation_payload). Остальные семь —
корректировки, расписание, цель CPA, минус-фразы, площадки, выключение —
применялись, не обещая ничего. Следствий два, и оба ломают бету:

  * ОТБИРАТЬ НЕЧЕМ. Отбор лучшего на объекте (задача 7) ранжирует действия по
    ценности на рубль риска; у действия без ожидания ценности нет, и порядок
    снова оказывается порядком сборки плана — тем самым дефектом d36d1c3,
    из-за которого сотни корректировок вытесняли единицы сдвигов бюджета.
  * СУДИТЬ НЕЧЕМ. Замер такта кладёт рядом с ожиданием факт
    (agent_e1_watchdog.observed_leads_delta); без ожидания наблюдение не
    закрывается, в track record полосы действие не попадает, и лестница
    автономии по этой полосе не двигается никогда.

Ожидание — не «сколько хотелось бы», а проверяемое утверждение из трёх чисел
и одной фразы:

    {"leads_delta": +0.79, "rub_delta": 0.0,
     "basis": "сегмент 25.0% объекта × сдвиг −30%: …",
     "measure_days": 7}

Горизонт берётся из полос (lanes.MEASURE_DAYS), а не заводится здесь своей
константой: лимит полосы, срок наблюдения и обещание обязаны мерить ОДНО окно,
иначе факт сравнивается с прогнозом на другой срок.

Модели по рычагам — и почему они такие:

  * КОРРЕКТИРОВКА СТАВКИ И РАСПИСАНИЕ («те же деньги, больше лидов»). Правка
    коэффициента не трогает лимит: деньги переносятся ВНУТРИ объекта — из
    сегмента с конверсией r = 1 + p/100 базовой в остальной объект (r = 1) или
    наоборот. Отсюда rub_delta = 0, а прирост лидов = перенесённые рубли ×
    |p|/100 ÷ цена лида. Знак корректировки — направление переноса, а не знак
    обещания: и доливка в сильный сегмент, и урезание слабого обещают ПРИРОСТ.
    Оценка сегмента r восстанавливается из самого коэффициента, потому что он
    из неё и посчитан (computed.bid_modifier_percent: p = clip(r − 1) × 100);
    на упоре в потолок ±50 % это нижняя граница отклонения, то есть обещание
    занижено — в консервативную сторону.
  * ОТСЕЧЕНИЕ (минус-фразы, площадки) — «меньше расход, лидов не теряем».
    Вырезаемый расход известен точно: по нему кандидат и выбран
    (objects.minus_word_candidates). Лиды: у кандидата с признаком
    zero_conversions их нет по построению, у кандидата cpa_above_limit они
    есть и теряются — их число едет в контексте отдельно. Обещать ноль там,
    где режется конверсионный трафик, значило бы сделать наблюдение заведомо
    провальным.
  * ЦЕЛЬ CPA — «дороже лид, больше объём» (и наоборот). Расход следует за
    целью пропорционально, а прирост расхода покупает лиды по НОВОЙ цели:
    цель и есть та цена, которую мы объявили допустимой. Обе стороны берутся
    из самого действия (payload.TargetCpa против previous_state.TargetCpa),
    а не из плана: в кабинет уезжает дожатое капом число (tcpa.clamp_step),
    и обещать надо про него.
  * ВЫКЛЮЧЕНИЕ — единственный рычаг, обещающий МИНУС по лидам. Его оправдание
    не в том, что кампания что-то принесёт, а в том, что её деньги работают
    лучше в соседней (switch.plan_switch_offs: предельная окупаемость на полу
    не дотягивает до λ кабинета). Честное ожидание здесь — потеря лидов
    кампании и экономия её расхода; выдавать за обещание чужой прирост,
    которого этот рычаг не производит, нельзя.
  * БЮДЖЕТ — число солвера КАК ЕСТЬ. Пересчитать его здесь значило бы завести
    вторую копию кривой насыщения: две модели разъезжаются на первой же правке
    одной из них, а расхождение прогноза с исходом — ровно та величина, ради
    которой всё и меряется.

Чего модуль не делает. Не выдумывает курс «рубли → лиды»: без цены лида
объекта корректировка ожидания не заявляет вовсе. Ноль здесь был бы не
осторожностью, а прогнозом «эффекта не будет», и петля обучения зачла бы его
сбывшимся (тот же довод, что у budget._expected_leads_delta). Цена риска в
такой ситуации ведёт себя ЗЕРКАЛЬНО — неизвестная доля означает «под ударом
весь объект» (exposure.py): и там, и здесь неизвестность толкает число в
сторону меньшего обещания и большей осторожности.
"""

from typing import Any, Dict, Optional

from sync.agent.writer import schedule
from sync.agent.writer.budget import BUDGET_DAILY_KIND, BUDGET_KIND, VAT

MICROS = 1_000_000

# Ключи ожидания в payload. Payload — единственная часть действия, которая
# переживает прогон: журнал хранит его целиком (writer_db.insert_action), а
# верхний уровень действия — нет. Поэтому обещание едет именно здесь, и
# петля обучения читает его оттуда же
# (writer_db.CLOSED_ACTIONS_SQL: payload->>'expected_leads_delta').
LEADS_KEY = "expected_leads_delta"
RUB_KEY = "expected_rub_delta"
BASIS_KEY = "expectation_basis"
DAYS_KEY = "expectation_days"

# Сколько дней в окне, на котором солвер считает целевой бюджет и лиды.
WINDOW_DAYS = 28.0

HOURS = 24.0


def of(action: Dict[str, Any],
       context: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Ожидание действия: заявленное рычагом или посчитанное по модели.

    Сначала читается ЗАЯВЛЕННОЕ: рычаг считал его на своих данных (доля
    сегмента, конверсии вырезаемого трафика), которых в самом действии нет, и
    пересчёт по обеднённому контексту дал бы другое число — то есть отбор и
    замер судили бы не о том обещании, с которым действие уезжало в кабинет.

    None — ожидания нет и выдумать его не из чего. Отдельный случай: у
    бюджетных действий заявлено число солвера, но нет остальных полей — им
    считается только рублёвая сторона, а лиды берутся как есть.
    """
    declared = _declared(action)
    if declared is not None:
        return declared
    return _model(action, context or {})


def attach(action: Dict[str, Any],
           context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Действие с ожиданием в payload (и в поле expected — для отбора).

    Ожидания нет — действие возвращается КАК ЕСТЬ, без ключей-пустышек:
    отсутствие ключа отличимо от нуля, а None в payload уехал бы в журнал
    как записанный прогноз «эффекта не будет».

    Повторный вызов ничего не портит: of на уже заявленном действии вернёт
    заявленное, и в payload лягут те же числа.
    """
    exp = of(action, context)
    if exp is None:
        return action
    payload = {
        **(action.get("payload") or {}),
        LEADS_KEY: exp["leads_delta"],
        RUB_KEY: exp["rub_delta"],
        BASIS_KEY: exp["basis"],
        DAYS_KEY: exp["measure_days"],
    }
    return {**action, "payload": payload, "expected": exp}


def _declared(action: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payload = action.get("payload") or {}
    if payload.get(BASIS_KEY) is None:
        return None
    return {
        "leads_delta": float(payload.get(LEADS_KEY) or 0.0),
        "rub_delta": float(payload.get(RUB_KEY) or 0.0),
        "basis": str(payload[BASIS_KEY]),
        "measure_days": int(payload.get(DAYS_KEY) or 0),
    }


def _measure_days(action: Dict[str, Any]) -> Optional[int]:
    """Горизонт замера полосы этого действия. None — вида нет в карте полос.

    Импорт полос ВНУТРИ функции: writer/lanes.py сам берёт из writer/switch.py
    потолок выключений, а switch заявляет ожидание отсюда — при импорте на
    уровне модуля кольцо switch → expectation → lanes → switch рвалось бы на
    полуинициализированном switch, и порядок импортов решал бы, соберётся
    пакет или нет.
    """
    from sync.agent.writer import lanes

    try:
        return lanes.MEASURE_DAYS[lanes.lane_of(action)]
    except ValueError:
        return None


def _round(value: float) -> float:
    """Округление до копеек без минус-нуля.

    -0.0 в журнале и в отчёте читается как «отрицательное, но очень мало»;
    у отсечения без конверсий потеряно не «чуть меньше нуля», а ровно ноль.
    """
    return round(value, 2) + 0.0


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None   # NaN — то же «неизвестно»


def _cpa(context: Dict[str, Any]) -> Optional[float]:
    """Цена лида объекта — курс перевода рублей в лиды.

    Прямое число или частное расхода и лидов: у портфеля под рукой пара
    (расход окна, лиды окна), у прогона — база кабинета, и требовать от них
    одной формы значило бы гонять одно и то же деление по вызывающим.
    """
    direct = _number(context.get("cpa_rub"))
    if direct is not None and direct > 0:
        return direct
    cost = _number(context.get("daily_cost_rub"))
    leads = _number(context.get("leads_per_day"))
    if cost is None or leads is None or leads <= 0:
        return None
    return cost / leads


def _model(action: Dict[str, Any],
           context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    days = _measure_days(action)
    if days is None or days <= 0:
        return None
    kind = str(action.get("action_kind") or "")
    if kind in ("bidmodifier.add", "bidmodifier.set"):
        return _bid_modifier(action, context, days)
    if kind == "schedule.set":
        return _schedule(action, context, days)
    if kind in (BUDGET_KIND, BUDGET_DAILY_KIND):
        return _budget(action, days)
    if kind == "tcpa.set":
        return _tcpa(action, context, days)
    # Литерал, а не константа рычага: writer/goal.py сам заявляет отсюда
    # ожидание, и импорт на уровне модуля замкнул бы кольцо goal → expectation.
    if kind == "goal.set":
        return _goal(action, context, days)
    if kind == "campaign.suspend":
        return _suspend(action, context, days)
    if kind in ("negative.add", "placement.exclude"):
        return _cut(action, context, days)
    if kind == "negative.remove_added":
        return _restore(action, context, days)
    return None


def _restore(action: Dict[str, Any], context: Dict[str, Any],
             days: int) -> Optional[Dict[str, Any]]:
    """Снятие своей минус-фразы — зеркало отсечения, которое оно отменяет.

    Числа приходят контекстом от отменяемого действия, а не считаются заново:
    вырезанный поток в кабинете больше не измеряется (его же и вырезали), и
    любая свежая оценка была бы догадкой. Зеркало же проверяемо: замер
    положит рядом факт и увидит, вернулось ли ровно то, что уходило.

    Знак противоположен отсечению: расход растёт, лиды возвращаются. Ноль
    лидов — законный случай (отсекали нулевой по конверсиям трафик), и
    обещание тогда чисто рублёвое.
    """
    daily = _number(context.get("restored_daily_rub"))
    if daily is None or daily <= 0:
        return None
    leads_day = _number(context.get("restored_conversions_per_day")) or 0.0
    removed = (action.get("payload") or {}).get("RemovedPhrases") or []
    return {
        "leads_delta": _round(leads_day * days),
        "rub_delta": _round(daily * days),
        "basis": (f"возвращается {len(removed)} шт.: +{round(daily)} ₽/дн "
                  f"расхода и {round(leads_day * days, 2)} лида за {days} дн. "
                  "по числам отменяемого отсечения"),
        "measure_days": days,
    }


def _reallocation_leads(moved_rub: float, percent: float, cpa: float) -> float:
    """Лиды от переноса moved_rub между конверсиями (1 + p/100) и 1.

    Перенос из сегмента с оценкой r в базовый даёт moved × (1 − r) / cpa,
    обратный — moved × (r − 1) / cpa; по модулю это одно и то же число, и
    знак обещания в обоих случаях плюс.
    """
    return moved_rub * abs(percent) / 100.0 / cpa


def _bid_modifier(action: Dict[str, Any], context: Dict[str, Any],
                  days: int) -> Optional[Dict[str, Any]]:
    percent = _number((action.get("payload") or {}).get("BidModifier"))
    share = _number(context.get("segment_share"))
    cost = _number(context.get("daily_cost_rub"))
    cpa = _cpa(context)
    if percent is None or share is None or cost is None or cpa is None:
        return None
    if share <= 0 or cost <= 0 or cpa <= 0:
        return None
    move = abs(percent) / 100.0
    moved = cost * share * move
    leads = _reallocation_leads(moved, percent, cpa) * days
    return {
        "leads_delta": _round(leads),
        # Корректировка не трогает лимит кампании: расход объекта тот же,
        # меняется его РАСКЛАДКА по сегментам.
        "rub_delta": 0.0,
        "basis": (f"сегмент {round(share * 100, 1)}% объекта × сдвиг "
                  f"{int(percent)}%: {round(moved)} ₽/дн переносится внутри "
                  f"объекта при цене лида {round(cpa)} ₽, {days} дн."),
        "measure_days": days,
    }


def _schedule(action: Dict[str, Any], context: Dict[str, Any],
              days: int) -> Optional[Dict[str, Any]]:
    """Расписание — та же модель переноса, разложенная по часам.

    Час берётся как 1/24 расхода: распределения расхода по часам в действии
    нет, и равновесная раскладка — то же допущение, на котором стоит цена
    риска расписания (exposure.schedule_exposure). Часы без правки в сумму не
    входят вовсе, иначе профиль «одна ночная ставка −50 %» обещал бы как
    сплошное понижение.
    """
    items = (((action.get("payload") or {}).get("TimeTargeting") or {})
             .get("Schedule") or {}).get("Items") or []
    cost = _number(context.get("daily_cost_rub"))
    cpa = _cpa(context)
    if not items or cost is None or cpa is None or cost <= 0 or cpa <= 0:
        return None

    by_hour = schedule.percent_by_hour(list(items))
    hourly_cost = cost / HOURS
    leads = 0.0
    touched = 0
    for percent in by_hour.values():
        if not percent:
            continue
        touched += 1
        moved = hourly_cost * abs(percent) / 100.0
        leads += _reallocation_leads(moved, percent, cpa)
    if touched == 0:
        return None
    mean_abs = sum(abs(p) for p in by_hour.values()) / HOURS
    return {
        "leads_delta": _round(leads * days),
        "rub_delta": 0.0,
        "basis": (f"расписание: правится {touched} ч. из 24, средний сдвиг по "
                  f"суткам {round(mean_abs, 1)}% при цене лида {round(cpa)} ₽, "
                  f"{days} дн."),
        "measure_days": days,
    }


def _budget(action: Dict[str, Any], days: int) -> Optional[Dict[str, Any]]:
    """Бюджет: лиды — число солвера, рубли — сдвиг лимита против расхода.

    Лиды НЕ пересчитываются: кривая насыщения живёт в портфеле, и вторая её
    копия здесь разошлась бы с первой на первой же правке. Горизонт солвера —
    окно расчёта (28 дней), горизонт полосы — 14; числа приведены к разным
    окнам сознательно, потому что править чужое ожидание задним числом
    значило бы сломать калибровку, которая на нём построена.
    """
    payload = action.get("payload") or {}
    leads = _number(payload.get(LEADS_KEY))
    cost_28d_vat = _number(payload.get("Cost28dVat"))
    if leads is None or cost_28d_vat is None:
        return None
    if str(action.get("action_kind")) == BUDGET_KIND:
        limit = _number(payload.get("WeeklySpendLimit"))
        target_day = None if limit is None else limit / MICROS / 7.0
    else:
        amount = _number((payload.get("DailyBudget") or {}).get("Amount"))
        target_day = None if amount is None else amount / MICROS
    if target_day is None:
        return None
    # Факты расхода с НДС, лимиты кабинета без — сравнивать их можно только
    # в одних единицах (budget.VAT).
    cost_day = cost_28d_vat / WINDOW_DAYS / VAT
    return {
        "leads_delta": _round(leads),
        "rub_delta": _round((target_day - cost_day) * days),
        "basis": (f"ожидание солвера портфеля за окно {int(WINDOW_DAYS)} дн.; "
                  f"лимит {round(target_day)} ₽/дн против расхода "
                  f"{round(cost_day)} ₽/дн"),
        "measure_days": days,
    }


def _tcpa(action: Dict[str, Any], context: Dict[str, Any],
          days: int) -> Optional[Dict[str, Any]]:
    payload = action.get("payload") or {}
    target = _number(payload.get("TargetCpa"))
    previous = _number((action.get("previous_state") or {}).get("TargetCpa"))
    cost = _number(context.get("daily_cost_rub"))
    if target is None or previous is None or cost is None:
        return None
    if target <= 0 or previous <= 0 or cost <= 0:
        return None
    target_rub = target / MICROS
    spend_delta = cost * (target / previous - 1.0) * days
    return {
        "leads_delta": _round(spend_delta / target_rub),
        "rub_delta": _round(spend_delta),
        "basis": (f"цель {round(target_rub)} ₽ против {round(previous / MICROS)} ₽: "
                  f"расход следует за целью, прирост покупает лиды по новой "
                  f"цели, {days} дн."),
        "measure_days": days,
    }


def _goal(action: Dict[str, Any], context: Dict[str, Any],
          days: int) -> Optional[Dict[str, Any]]:
    """Смена цели оптимизации: те же деньги, другая доля заявок.

    Расход не трогается вовсе — ни лимит, ни цель CPA рычаг не двигает, —
    поэтому рублёвая сторона обещания ровно ноль, а не «неизвестно»: это
    утверждение, и замер вправе спросить с него.

    Лиды считаются разностью двух конверсий на одном и том же потоке кликов.
    Обе приходят контекстом от рычага: конверсия новой цели снята с другого
    объекта (у ЭТОЙ кампании её по построению нет), и это ровно то, что
    делает действие ставкой, а не измерением (writer/tier.py). Нет любой из
    двух — обещания нет: курс «клики → лиды» не выдумывается.
    """
    clicks = _number(context.get("clicks_per_day"))
    cr_new = _number(context.get("cr_new"))
    cr_current = _number(context.get("cr_current"))
    if None in (clicks, cr_new, cr_current):
        return None
    if clicks <= 0 or cr_new <= 0 or cr_current <= 0:
        return None
    return {
        "leads_delta": _round(clicks * days * (cr_new - cr_current)),
        "rub_delta": 0.0,
        "basis": (f"смена цели: {round(clicks)} кликов/дн те же, конверсия "
                  f"{round(cr_new * 100, 2)} % против {round(cr_current * 100, 2)} %, "
                  f"{days} дн. Конверсия новой цели снята с другого объекта"),
        "measure_days": days,
    }


def _suspend(action: Dict[str, Any], context: Dict[str, Any],
             days: int) -> Optional[Dict[str, Any]]:
    cost = _number(context.get("daily_cost_rub"))
    cpa = _cpa(context)
    if cost is None or cpa is None or cost <= 0 or cpa <= 0:
        return None
    return {
        "leads_delta": _round(-cost / cpa * days),
        "rub_delta": _round(-cost * days),
        "basis": (f"выключение: кампания перестаёт тратить {round(cost)} ₽/дн и "
                  f"приносить лиды по {round(cpa)} ₽, {days} дн."),
        "measure_days": days,
    }


def _cut(action: Dict[str, Any], context: Dict[str, Any],
         days: int) -> Optional[Dict[str, Any]]:
    """Отсечение: вырезанный расход — точно, потерянные лиды — по кандидатам.

    Вырезаемые рубли берутся из экспозиции этого же действия: там они уже
    приведены к дню по окну наблюдения кандидатов, и второй пересчёт по
    другому окну развёл бы цену и обещание.

    Ключ cut_daily_rub — СЫРОЙ вырезаемый расход, и читается он первым.
    daily_rub у отсечения означает другое: сколько денег под ударом, а это
    после скидки правила трёх меньше вырезаемого потока на порядок
    (exposure.cut_exposure). Обещание — про снятые с кабинета деньги, не про
    поставленные под удар. Запасной путь через daily_rub оставлен для
    экспозиций старого вида (traffic_cut_exposure), где скидки нет и оба
    числа совпадают.
    """
    own = action.get("exposure") or {}
    cut_daily = _number(own.get("cut_daily_rub"))
    if cut_daily is None:
        cut_daily = _number(own.get("daily_rub"))
    if cut_daily is None or cut_daily <= 0:
        return None
    lost_leads_day = _number(context.get("cut_conversions_per_day")) or 0.0
    added = ((action.get("payload") or {}).get("AddedPhrases")
             or (action.get("payload") or {}).get("AddedSites") or [])
    return {
        "leads_delta": _round(-lost_leads_day * days),
        "rub_delta": _round(-cut_daily * days),
        "basis": (f"отсекается {len(added)} шт.: −{round(cut_daily)} ₽/дн "
                  f"расхода и {round(lost_leads_day * days, 2)} лида за "
                  f"{days} дн. по конверсиям кандидатов"),
        "measure_days": days,
    }
