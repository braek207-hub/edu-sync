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

Ширина интервала берётся из ПЛАЦЕБО, а не из пуассоновского счёта лидов.
Замер docs/AGENT-TICK-POWER.md (29.08.2026) прогнал этот самый оценщик по 168
дням истории, где агент не применял ничего и истинный эффект равен нулю по
построению: настоящий разброс — 13,2 %, а формула по счётчикам лидов давала
6,7 %, то есть занижала вдвое. Ценой этого занижения уверенный вердикт
«improved»/«worsened» выносился на пустом месте в 20,8 % случаев вместо
заявленных 5 %, а experiments.WINNING_VERDICTS зачитывает такие вердикты в
ставки — агент повышал бы ставки по шуму. Механизм пола ошибки взят готовым у
квазиэкспериментов (mining.placebo_sigma → mine_quasi_experiments error_floor),
только поднят с уровня одной кампании на уровень групп «обработанные против
заповедника».

Словарь исходов — тот же, что у сторожа (economic_outcome) и реестра ставок
(experiments.WINNING_VERDICTS). Второе слово для того же исхода означало бы,
что часть тактов не зачтётся никогда.
"""

import math
from datetime import date, datetime, timedelta
from statistics import pstdev
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sync.agent import experiments, holdout, mining

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

# Пол ошибки замера, снятый плацебо-прогоном на прод-данных кабинета
# (docs/AGENT-TICK-POWER.md, 29.08.2026): 168 точек истории, в каждой оба окна
# целиком в витрине и ни одного применённого действия, σ разброса DiD = 0,1322
# при медианной пуассоновской оценке 0,0665.
#
# Почему константа, если правильнее замерять. Замерять и надо — это делает
# placebo_sigma ниже, и его результат всегда в дело. Но прогон сторожа держит в
# памяти лишь два горизонта фактов (agent_e1_watchdog.load_facts: 2 × 14 дней),
# ровно те самые два окна, и плацебо-точек в них ноль. Пока история короче,
# ошибка не может быть меньше уже замеренной на этом кабинете: пустой замер —
# не повод вернуться к вдвое заниженному интервалу. Число пересматривается
# ПОВТОРНЫМ прогоном той же процедуры, а не понижается по короткому куску
# истории.
MEASURED_PLACEBO_SIGMA = 0.1322

# Шаг между плацебо-точками и минимум точек — общие с квазиэкспериментами
# (mining): смысл тот же, и вторая пара чисел означала бы, что «пол ошибки»
# в двух местах агента считается по-разному.
PLACEBO_STEP_DAYS = mining.PLACEBO_STEP_DAYS
MIN_PLACEBO_POINTS = mining.MIN_PLACEBO_POINTS

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
THIN_CONTROL_NEFF_REASON = (
    "эффективный размер заповедника {n_eff} кампании при пороге "
    "{minimum}: лиды собраны одной-двумя кампаниями, и вычитается из такта "
    "не сезон, а их собственная история"
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


def _control_n_eff(facts: Iterable[Dict[str, Any]], ids: Sequence[str],
                   baseline: Tuple[date, date],
                   observation: Tuple[date, date]) -> float:
    """Эффективный размер контроля (Kish) — худший из двух окон.

    Сумма лидов о годности контроля не говорит: 69 лидов в одной кампании и 69
    в пяти — разные контроли, а метрика заповедника взвешена расходом и
    лидами. Берётся МЕНЬШИЙ из двух окон по той же причине, по которой ниже
    берётся меньший объём: контроль не крепче своей слабой половины —
    сравниваются оба окна, а не лучшее из них.
    """
    per_campaign: Dict[str, Dict[str, int]] = {}
    wanted = {str(i) for i in ids}
    for row in facts or ():
        campaign = str(row.get("campaign_id"))
        if campaign not in wanted:
            continue
        day = _as_date(row.get("fact_date"))
        if day is None:
            continue
        for name, (start, end) in (("baseline", baseline),
                                   ("observation", observation)):
            if start <= day <= end:
                cell = per_campaign.setdefault(campaign, {"baseline": 0,
                                                          "observation": 0})
                cell[name] += int(row.get("eff_leads") or 0)
    if not per_campaign:
        return 0.0
    return min(holdout.kish_n_eff(c[name] for c in per_campaign.values())
               for name in ("baseline", "observation"))


def _relative_delta(side: Dict[str, Any]) -> Optional[float]:
    """Относительное изменение цены лида стороны. None — считать не из чего."""
    base = float(side.get("baseline_cpa") or 0.0)
    now = float(side.get("cpa") or 0.0)
    if base <= 0 or now <= 0:
        return None
    return (now - base) / base


def _daily_cost_leads(facts: Iterable[Dict[str, Any]],
                      ids: Sequence[str]) -> Dict[date, Tuple[float, int]]:
    """Расход и эффективные лиды группы по дням — один проход по фактам.

    Плацебо-прогон повторяет замер в десятках точек истории, и наивный
    пересчёт «просуммируй все факты за окно» стоил бы сотни проходов по всей
    выборке. День — минимальная единица витрины, поэтому свернуть к нему можно
    один раз.
    """
    wanted = {str(i) for i in ids}
    out: Dict[date, Tuple[float, int]] = {}
    for row in facts or ():
        if str(row.get("campaign_id")) not in wanted:
            continue
        day = _as_date(row.get("fact_date"))
        if day is None:
            continue
        cost, leads = out.get(day, (0.0, 0))
        out[day] = (cost + float(row.get("cost") or 0.0),
                    leads + int(row.get("eff_leads") or 0))
    return out


def _window_cost_leads(daily: Dict[date, Tuple[float, int]],
                       window: Tuple[date, date]) -> Tuple[float, int]:
    start, end = window
    cost, leads = 0.0, 0
    day = start
    while day <= end:
        add_cost, add_leads = daily.get(day, (0.0, 0))
        cost += add_cost
        leads += add_leads
        day += timedelta(days=1)
    return cost, leads


def _group_delta(daily: Dict[date, Tuple[float, int]],
                 baseline: Tuple[date, date], observation: Tuple[date, date]
                 ) -> Optional[Tuple[float, int, int]]:
    """Относительное изменение цены лида группы и объёмы обоих окон."""
    base_cost, base_leads = _window_cost_leads(daily, baseline)
    obs_cost, obs_leads = _window_cost_leads(daily, observation)
    if base_leads <= 0 or obs_leads <= 0 or base_cost <= 0 or obs_cost <= 0:
        return None
    base_cpa = base_cost / base_leads
    obs_cpa = obs_cost / obs_leads
    return (obs_cpa - base_cpa) / base_cpa, base_leads, obs_leads


def placebo_sigma(facts: Iterable[Dict[str, Any]], treated_ids: Sequence[str],
                  control_ids: Sequence[str], before: date,
                  horizon_days: int = DEFAULT_HORIZON_DAYS,
                  step: int = PLACEBO_STEP_DAYS) -> Optional[float]:
    """Разброс ЭТОГО ЖЕ оценщика там, где такта не было.

    Приём взят у квазиэкспериментов (mining.placebo_sigma) и поднят с уровня
    одной кампании на уровень групп: в точке истории, где агент ничего не
    применял, истинный эффект такта равен нулю по построению, и всё, что
    разность разностей там показывает, — шум. Сезон, аукцион, качество
    трафика, состав дней недели, случайные колебания конверсии — пуассоновский
    счёт лидов не знает ни об одном из них и потому систематически занижает
    неопределённость. Замер docs/AGENT-TICK-POWER.md: 6,7 % против настоящих
    13,2 %, ровно вдвое.

    Точки берутся строго ПОЗАДИ дня такта (окно наблюдения кончается раньше
    before), иначе измеряемый эффект попал бы в собственный пол ошибки.
    Плацебо-точки не обязаны быть чистыми от прежних тактов агента: чужой
    эффект внутри точки разброс только расширяет, а расширение — безопасная
    сторона. Порог контроля тот же, что у самого замера
    (holdout.MIN_CONTROL_LEADS): пол должен быть посчитан по тем конфигурациям,
    в которых замер вообще выносит суждение.

    Точек меньше MIN_PLACEBO_POINTS — None, а не выдуманный ноль: оценка
    разброса по трём наблюдениям сама шум. Что делать с None, решает
    вызывающий (measure берёт замеренный на проде пол).
    """
    treated_daily = _daily_cost_leads(facts, treated_ids)
    control_daily = _daily_cost_leads(facts, control_ids)
    days = sorted(set(treated_daily) | set(control_daily))
    if not days:
        return None

    horizon = int(horizon_days)
    first_tact = days[0] + timedelta(days=horizon)
    last_tact = min(days[-1] - timedelta(days=horizon - 1),
                    before - timedelta(days=horizon))

    effects: List[float] = []
    point = last_tact
    while point >= first_tact:
        baseline, observation = windows_around(point, horizon)
        treated = _group_delta(treated_daily, baseline, observation)
        control = _group_delta(control_daily, baseline, observation)
        point -= timedelta(days=max(int(step), 1))
        if treated is None or control is None:
            continue
        if min(control[1], control[2]) < holdout.MIN_CONTROL_LEADS:
            continue
        effects.append(treated[0] - control[0])

    if len(effects) < MIN_PLACEBO_POINTS:
        return None
    return pstdev(effects)


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
            horizon_days: int = DEFAULT_HORIZON_DAYS,
            error_floor: Optional[float] = None) -> Dict[str, Any]:
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

    error_floor — уже посчитанный плацебо-разброс, если вызывающий считает его
    сам (разбор по истории считает пол один раз на десятки тактов, а проход
    дорогой). None — замер считает пол сам по переданным фактам и в любом
    случае не опускается ниже MEASURED_PLACEBO_SIGMA: подпись без пола не
    должна молча возвращать вдвое заниженный интервал, ради которого правка и
    делалась.
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

    # Второй порог годности контроля — на ЭФФЕКТИВНОМ размере группы, а не на
    # сумме её лидов. Когорта заповедника, прослеженная замером вперёд
    # (docs/AGENT-TICK-POWER.md), давала 453 лида при Kish n_eff = 1,47: порог
    # в 20 лидов она проходит в двадцать раз, будучи фактически одной
    # кампанией, и «сезон» вычитался бы по истории этой одной кампании.
    control["n_eff"] = round(_control_n_eff(facts, control_ids, baseline,
                                            observation), 2)
    if control["n_eff"] < holdout.MIN_CONTROL_NEFF:
        return _unknown(
            THIN_CONTROL_NEFF_REASON.format(n_eff=control["n_eff"],
                                            minimum=holdout.MIN_CONTROL_NEFF),
            tact_date, windows, treated, control)

    treated_delta = _relative_delta(treated)
    control_delta = _relative_delta(control)
    if treated_delta is None or control_delta is None:
        return _unknown(NO_TREATED_FACTS_REASON, tact_date, windows,
                        treated, control)

    did = treated_delta - control_delta

    # Пуассоновская ошибка — из объёмов ОБЕИХ сторон окна наблюдения: разность
    # разностей не может быть точнее, чем самая шумная из них. Складываются
    # дисперсии, а не ошибки, поэтому под корнем сумма обратных счётчиков —
    # та же оценка, что у сторожа (rel = sqrt(1/leads)), только на две группы.
    # Это НИЖНЯЯ граница неопределённости, и одна она врёт вдвое: счётчик
    # лидов не знает ни про сезон, ни про состав дней недели, ни про то, что
    # цена лида самой кампании гуляет на десятки процентов сама по себе.
    poisson = math.sqrt(1.0 / max(int(treated["leads"]), 1)
                        + 1.0 / max(int(control["leads"]), 1))
    measured_floor = (float(error_floor) if error_floor is not None
                      else placebo_sigma(facts, treated_ids, control_ids,
                                         before=baseline[1] + timedelta(days=1),
                                         horizon_days=horizon_days))
    # Из трёх оценок берётся САМАЯ ШИРОКАЯ. Замеренный на этой истории пол
    # выигрывает у пуассоновского счёта почти всегда, а замеренный на проде
    # (MEASURED_PLACEBO_SIGMA) страхует случай, когда истории под плацебо не
    # хватило: «не смогли посчитать разброс» — не то же самое, что «разброса
    # нет».
    standard_error = max(poisson, MEASURED_PLACEBO_SIGMA, measured_floor or 0.0)
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
        # Из чего сложился интервал — в отчёт: вердикт «inconclusive» без этих
        # трёх чисел неотличим от «замер сломался», и первый же разбор такта
        # начался бы с раскопок вместо чтения.
        "error": {
            "standard_error": round(standard_error, 4),
            "poisson": round(poisson, 4),
            "placebo": round(measured_floor, 4) if measured_floor else None,
            "measured_floor": MEASURED_PLACEBO_SIGMA,
        },
        "treated": treated,
        "holdout": control,
        "windows": windows,
    }
