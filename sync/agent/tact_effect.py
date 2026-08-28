# -*- coding: utf-8 -*-
"""
sync/agent/tact_effect.py — эффект ТАКТА ЦЕЛИКОМ против заповедника (Ф16).

Чего не хватало. Сторож судит КАЖДОЕ действие отдельно: базовое окно
кампании против её же окна наблюдения, с поправкой на заповедник
(agent_e1_watchdog._did_effect). На единичных правках это работает, но такт
беты — это до четырёхсот одновременных изменений, и у каждого отдельного
действия слишком мало лидов, чтобы контроль что-то значил. Хуже того: все они
живут в одном кабинете и двигаются вместе с ним, поэтому четыреста «слегка
подорожало» складываются не в четыреста маленьких провалов, а в один общий
сдвиг рынка, которого агент не делал.

Такт — это ОДНО решение системы: набор действий, применённых в один день по
одному плану. Спрашивать с него надо тоже одним числом, и считать это число
там, где объёма хватает, — на всех обработанных кампаниях разом против
заповедника разом.

Что здесь считается. Разность разностей по цене эффективного лида:

    treated_delta = (CPA обработанных после — до) / до
    holdout_delta = (CPA заповедника    после — до) / до
    did           = treated_delta − holdout_delta

Заповедник (holdout.select_holdout) — кампании, к которым агент сознательно
не применяет ничего; их движение между теми же двумя окнами и есть сезон плюс
рынок. Вычитая его, получаем то, что сделал такт.

Три случая, в которых замер обязан МОЛЧАТЬ, и все три названы причиной:

  * такт не применил ни одного действия. Кабинет двигается сам по себе
    каждый день, и приписать это движение себе тем соблазнительнее, чем оно
    приятнее;
  * заповедника нет или он слишком мал (holdout.MIN_CONTROL_LEADS). Контроль
    на десятке лидов — шум, и вычтенный по нему «сезон» добавил бы к оценке
    случайное число вместо поправки;
  * у обработанных нет фактов за одно из окон. Ноль лидов означает «мерить
    нечем», а не «эффекта нет».

Вердикт выносится по ИНТЕРВАЛУ, а не по точечной оценке: на шумных данных
разность разностей всегда чем-нибудь да отличается от нуля. «improved» —
весь интервал ниже нуля (лид подешевел относительно контроля), «worsened» —
весь выше, иначе «inconclusive»: ответа не куплено.

Словарь исходов — тот же, что у сторожа (economic_outcome) и реестра ставок
(experiments.WINNING_VERDICTS). Второе слово для того же исхода означало бы,
что часть тактов не зачтётся никогда.
"""

import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sync.agent import experiments, holdout

# Горизонт замера такта — тот же, которым живёт ставка (experiments.py). Такт
# состоит из ставок, и мерить его другим сроком значило бы закрывать общий
# счёт раньше или позже, чем частные.
DEFAULT_HORIZON_DAYS = experiments.HORIZON_DAYS

# Словарь исходов, общий с agent_e1_watchdog.economic_outcome.
VERDICTS = ("improved", "worsened", "inconclusive", "unknown")

# Множитель нормального интервала: 95 %. Записан числом с именем, потому что
# «1.96» в формуле — это решение о том, какой долей ошибок мы согласны
# заплатить за скорость выводов, а не арифметическая подробность.
Z_95 = 1.96

NO_ACTIONS_REASON = (
    "такт не применил ни одного действия: кабинет двигается и сам по себе, и "
    "приписывать это движение агенту не на чем"
)
NO_HOLDOUT_REASON = (
    "заповедник пуст: вычитать сезон нечем, а «сезона не было» — утверждение, "
    "которого никто не проверял"
)
THIN_HOLDOUT_REASON = (
    "заповедник дал {leads} эффективных лидов в окне при пороге контроля "
    "{minimum}: на таком объёме цена — шум, и вычтенный по нему сезон был бы "
    "случайным числом"
)
NO_TREATED_FACTS_REASON = (
    "у обработанных кампаний нет фактов за оба окна: ноль лидов означает "
    "«мерить нечем», а не «эффекта нет»"
)


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def windows_around(tact_date: Any, horizon_days: int = DEFAULT_HORIZON_DAYS
                   ) -> Tuple[Tuple[date, date], Tuple[date, date]]:
    """Базовое окно и окно наблюдения вокруг дня такта.

    Окна равной длины и смежные: наблюдение начинается днём такта, база
    заканчивается накануне. Равная длина — не симметрия ради красоты: у
    образовательного спроса будни и выходные различаются кратно, и окна
    разной длины сравнивали бы разный состав дней недели.

    День самого такта отнесён к наблюдению: изменения уезжают в кабинет
    утренним прогоном, и открутка этого дня идёт уже по новым настройкам.
    """
    tact = _as_date(tact_date)
    if tact is None:
        raise ValueError(f"день такта не читается датой: {tact_date!r}")
    horizon = int(horizon_days)
    if horizon <= 0:
        raise ValueError(f"горизонт замера должен быть положителен: {horizon_days!r}")
    baseline = (tact - timedelta(days=horizon), tact - timedelta(days=1))
    observation = (tact, tact + timedelta(days=horizon - 1))
    return baseline, observation


def _cost_leads(facts: Iterable[Dict[str, Any]], ids: Sequence[str],
                window: Tuple[date, date]) -> Tuple[float, int]:
    """Расход и эффективные лиды названных кампаний за окно.

    Форма строки — витринная (db.load_daily_facts): campaign_id, fact_date,
    cost, eff_leads. Знаменатель — eff_leads, тот же, что у базового CPA
    красной линии: считать наблюдение и базу по разным знаменателям значит
    сравнивать разные величины.
    """
    wanted = set(ids)
    start, end = window
    cost = 0.0
    leads = 0
    for row in facts or ():
        if str(row.get("campaign_id")) not in wanted:
            continue
        day = _as_date(row.get("fact_date"))
        if day is None or day < start or day > end:
            continue
        cost += float(row.get("cost") or 0.0)
        leads += int(row.get("eff_leads") or 0)
    return cost, leads


def _side(facts: Iterable[Dict[str, Any]], ids: Sequence[str],
          baseline: Tuple[date, date], observation: Tuple[date, date]
          ) -> Dict[str, Any]:
    """Одна сторона сравнения: цена лида до и после, с объёмами."""
    base_cost, base_leads = _cost_leads(facts, ids, baseline)
    obs_cost, obs_leads = _cost_leads(facts, ids, observation)
    return {
        "campaigns": sorted(ids),
        "baseline_leads": base_leads,
        "leads": obs_leads,
        "baseline_cpa": round(base_cost / base_leads, 2) if base_leads > 0 else 0.0,
        "cpa": round(obs_cost / obs_leads, 2) if obs_leads > 0 else 0.0,
        "baseline_cost": round(base_cost, 2),
        "cost": round(obs_cost, 2),
    }


def _relative_delta(side: Dict[str, Any]) -> Optional[float]:
    """Относительное изменение цены лида стороны. None — считать не из чего."""
    base = float(side.get("baseline_cpa") or 0.0)
    now = float(side.get("cpa") or 0.0)
    if base <= 0 or now <= 0:
        return None
    return (now - base) / base


def _verdict(did: float, low: float, high: float) -> str:
    """Исход по ИНТЕРВАЛУ, а не по точечной оценке.

    Точечная оценка на шумных данных всегда отличается от нуля, и вердикт по
    ней означал бы «такт всегда что-то сделал». Утверждение законно ровно
    тогда, когда весь интервал лежит по одну сторону нуля.
    """
    if high < 0:
        return "improved"          # лид подешевел относительно контроля
    if low > 0:
        return "worsened"
    return "inconclusive"


def _unknown(reason: str, tact_date: Any, windows: Dict[str, Any],
             treated: Optional[Dict[str, Any]] = None,
             control: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "tact_date": str(tact_date),
        "treated_delta": None,
        "holdout_delta": None,
        "did": None,
        "ci": None,
        "verdict": "unknown",
        "reason": reason,
        "treated": treated or {},
        "holdout": control or {},
        "windows": windows,
    }


def measure(tact_date: Any, facts: List[Dict[str, Any]],
            holdout_ids: Iterable[str], applied_object_ids: Iterable[str],
            horizon_days: int = DEFAULT_HORIZON_DAYS) -> Dict[str, Any]:
    """Эффект такта: разность разностей обработанных против заповедника.

    Аргумент applied_object_ids планом не предусматривался — там подпись
    заканчивалась заповедником. Без него, однако, нельзя отличить такт,
    который ничего не применил, от такта, который применил и не подействовал:
    в фактах обе картины выглядят одинаково, а вывод из них противоположный.
    Список применённых кампаний знает вызывающий (сторож — по журналу
    действий), и брать его оттуда честнее, чем угадывать по движению цифр.

    facts — дневные факты ОБЕИХ групп одним списком (форма
    db.load_daily_facts). Разделение по группам делает сам замер: у него уже
    есть и список заповедника, и список обработанных, а вызывающий, деля
    факты сам, однажды положил бы кампанию в обе стороны сразу.
    """
    baseline, observation = windows_around(tact_date, horizon_days)
    windows = {
        "baseline": (baseline[0].isoformat(), baseline[1].isoformat()),
        "observation": (observation[0].isoformat(), observation[1].isoformat()),
        "horizon_days": int(horizon_days),
    }

    control_ids = sorted({str(i) for i in holdout_ids or ()})
    # Кампания заповедника среди применённых — дефект отбора (agent_e1 отсекает
    # такие действия check_holdout). Замер её вычитает, а не мерит заповедник
    # против самого себя: иначе контроль перестал бы быть контролем ровно в
    # том прогоне, где это важнее всего заметить.
    treated_ids = sorted({str(i) for i in applied_object_ids or ()}
                         - set(control_ids))

    if not treated_ids:
        return _unknown(NO_ACTIONS_REASON, tact_date, windows)
    if not control_ids:
        return _unknown(NO_HOLDOUT_REASON, tact_date, windows)

    treated = _side(facts, treated_ids, baseline, observation)
    control = _side(facts, control_ids, baseline, observation)

    control_leads = min(int(control["leads"]), int(control["baseline_leads"]))
    if control_leads < holdout.MIN_CONTROL_LEADS:
        return _unknown(
            THIN_HOLDOUT_REASON.format(leads=control_leads,
                                       minimum=holdout.MIN_CONTROL_LEADS),
            tact_date, windows, treated, control)

    treated_delta = _relative_delta(treated)
    control_delta = _relative_delta(control)
    if treated_delta is None or control_delta is None:
        return _unknown(NO_TREATED_FACTS_REASON, tact_date, windows,
                        treated, control)

    did = treated_delta - control_delta
    # Ошибка оценки — из объёмов ОБЕИХ сторон окна наблюдения: разность
    # разностей не может быть точнее, чем самая шумная из них. Складываются
    # дисперсии, а не ошибки, поэтому под корнем сумма обратных счётчиков —
    # та же оценка, что у сторожа (rel = sqrt(1/leads)), только на две группы.
    standard_error = math.sqrt(1.0 / max(int(treated["leads"]), 1)
                               + 1.0 / max(int(control["leads"]), 1))
    half = Z_95 * standard_error
    low, high = did - half, did + half

    return {
        "tact_date": str(tact_date),
        "treated_delta": round(treated_delta, 4),
        "holdout_delta": round(control_delta, 4),
        "did": round(did, 4),
        "ci": (round(low, 4), round(high, 4)),
        "verdict": _verdict(did, low, high),
        "reason": "",
        "treated": treated,
        "holdout": control,
        "windows": windows,
    }
