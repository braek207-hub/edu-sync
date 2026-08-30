# -*- coding: utf-8 -*-
"""
sync/agent/value.py — сколько агент принёс и сэкономил, в рублях.

Замеры у агента уже есть, и оба — в долях, а не в деньгах:

  * ЭФФЕКТ ТАКТА против заповедника (tact_effect.measure): did — относительное
    изменение цены эффективного лида обработанных кампаний за вычетом того же
    изменения у контроля. did = −0,10 значит «лид подешевел на 10 % сверх
    того, что случилось бы само».
  * ИСХОД ДЕЙСТВИЯ против ожидания (журнал, writer/db.closed_actions):
    observed_leads_delta — сколько лидов действие принесло сверх базового
    темпа за окно наблюдения.

Владелец судит о бете не долями. Перевод стоит ровно один множитель — расход
обработанных за окно наблюдения для тактов и цена эффективного лида кампании
для действий, — и весь модуль про то, чтобы этот множитель не появился там,
где мерить было нечем.

ГЛАВНОЕ ПРАВИЛО: НЕИЗМЕРЕННОЕ НЕ РАВНО НУЛЮ. Такт с вердиктом inconclusive —
это «весь интервал накрывает ноль, утверждать нечего», а не «выгоды не было».
Ноль рублей рядом с измеренными тактами читался бы как провал, поэтому у
каждой суммы едет счётчик измеренного и доля неизмеренного, а у тактов —
интервал в рублях. Интервал считается и для inconclusive: он включает ноль, и
это и есть честная граница знания, а не дефект замера.

Чего модуль не делает. Не ходит в БД (вход собирает вызывающий: Э0 читает
чёрный ящик и журнал), не выдумывает цену лида там, где лестница её не
посчитала, и не выдаёт обещание рычага за факт: у вырезанных гигиеной рублей
стоит basis='planned', потому что фактического расхода до/после сторож в
журнал действий не кладёт (mark_observation_closed пишет только вердикт и
дельту лидов).
"""

from typing import Any, Dict, Iterable, List, Optional, Tuple

from sync.agent.tact_effect import NO_ACTIONS_REASON
from sync.agent.writer.lanes import LANE_HYGIENE, LANE_OF_KIND

# Вердикты замера такта, при которых об эффекте можно что-то утверждать: весь
# доверительный интервал лежит по одну сторону нуля (tact_effect._verdict).
MEASURED_VERDICTS = ("improved", "worsened")

# Полоса действия, вид которого карта полос не знает. Не «гигиена» и не пустая
# строка: неизвестная полоса обязана быть видна в разбивке отдельной строкой,
# иначе новый вид действия тихо приписался бы чужим деньгам.
UNKNOWN_LANE = "unknown"

# Ожидание рычага в рублях за горизонт замера (writer/expectation.RUB_KEY).
# Отрицательное — рычаг снимает деньги с кабинета.
EXPECTED_RUB_KEY = "expected_rub_delta"

# Чем посчитаны вырезанные рубли. Значение одно: факта расхода до/после в
# журнале действий нет, и появится оно только вместе с колонкой, которой
# сторож этот факт запишет. Второй константы здесь нет намеренно — она была бы
# обещанием ветки кода, которой не существует.
BASIS_PLANNED = "planned"


def _cost_after(tact: Dict[str, Any]) -> float:
    """Расход обработанных за окно наблюдения — из любой из двух форм такта.

    Плоская форма (cost_treated_after) приходит из плана и из тестов, вложенная
    (treated.cost) — прямо из tact_effect.measure, то есть из чёрного ящика.
    Нормализация здесь одна на оба входа: разведи её по вызывающим — и один из
    них однажды прочитал бы ноль при живом did, показав нулевую выгоду.
    """
    flat = tact.get("cost_treated_after")
    if flat is not None:
        return float(flat)
    return float((tact.get("treated") or {}).get("cost") or 0.0)


def _interval_rub(tact: Dict[str, Any], cost: float
                  ) -> Optional[Tuple[float, float]]:
    """Интервал эффекта в рублях. None — интервала у замера нет вовсе.

    Знак переворачивается вместе с did: рост цены лида — это потраченные
    деньги, падение — сэкономленные. Границы пересортировываются, потому что
    после смены знака верхняя граница доли становится нижней границей рублей.
    """
    ci = tact.get("ci")
    if not ci or len(ci) != 2 or ci[0] is None or ci[1] is None or cost <= 0:
        return None
    low, high = -float(ci[1]) * cost, -float(ci[0]) * cost
    if low > high:
        low, high = high, low
    return round(low, 2), round(high, 2)


def tact_value(tact: Dict[str, Any]) -> Dict[str, Any]:
    """Такт в рублях: сколько сэкономлено относительно заповедника.

    saved_rub = −did × расход обработанных. Отрицательный did означает, что
    лид подешевел, — то есть тот же расход купил больше лидов, и экономия
    равна доле удешевления от потраченного. Положительный did (вердикт
    worsened) даёт отрицательную экономию: такт стоил денег, и печатать это
    минусом честнее, чем прятать модулем.

    measured=True только при вердикте improved/worsened. inconclusive и
    unknown дают saved_rub=0.0 при measured=False — это «не измерено», а не
    «выгоды ноль», и читать сумму без соседнего счётчика измеренного нельзя.

    Нулевой расход обработанных тоже снимает measured, хотя вердикт при этом
    может быть годным: доля без множителя в рубли не переводится, и ноль тут
    означал бы «такт не дал ничего», тогда как он означает «переводить нечем».
    Из самого measure такая пара не приходит (вердикт требует ненулевых окон),
    но плоская форма такта расход может и не нести.
    """
    verdict = str(tact.get("verdict") or "unknown")
    did = tact.get("did")
    cost = _cost_after(tact)
    measured = verdict in MEASURED_VERDICTS and did is not None and cost > 0
    return {
        "saved_rub": round(-float(did) * cost, 2) if measured else 0.0,
        "measured": measured,
        "interval_rub": _interval_rub(tact, cost),
        "verdict": verdict,
    }


def action_value(action: Dict[str, Any],
                 value_per_lead: Optional[float]) -> Dict[str, Any]:
    """Действие в рублях: заработано лидами и (для гигиены) вырезано расходом.

    earned_rub = observed_leads_delta × цена эффективного лида кампании. Оба
    множителя обязаны существовать: дельты нет у действия без базового темпа в
    красной линии, цены нет у кампании, которой лестница не посчитала ступень
    или чек направления. Ноль вместо любого из них был бы утверждением «эффекта
    не было» — тем же дефектом, от которого observed_leads_delta хранится
    NULL-ом, а не нулём.

    cut_rub считается ТОЛЬКО полосе гигиены: она одна снимает деньги с
    кабинета, а сдвиг лимита их переносит — засчитать перенос экономией значит
    посчитать одни и те же рубли дважды.

    basis='planned' и никогда 'fact', пока сторож не кладёт в журнал
    фактический расход до и после: закрытие наблюдения пишет
    observation_verdict и observed_leads_delta, и другого следа денег в
    edu_agent_actions нет. Обещание рычага — это то, что рычаг СОБИРАЛСЯ
    вырезать; печатать его без пометки значило бы выдать план за замер.
    """
    kind = str(action.get("action_kind") or "")
    lane = LANE_OF_KIND.get(kind) or UNKNOWN_LANE
    observed = action.get("observed_leads_delta")
    measured = observed is not None and value_per_lead is not None

    cut_rub, basis = 0.0, None
    if lane == LANE_HYGIENE:
        promised = action.get(EXPECTED_RUB_KEY)
        if promised is None:
            promised = (action.get("payload") or {}).get(EXPECTED_RUB_KEY)
        if promised is not None:
            cut_rub = round(max(0.0, -float(promised)), 2)
            basis = BASIS_PLANNED

    return {
        "earned_rub": (round(float(observed) * float(value_per_lead), 2)
                       if measured else 0.0),
        "cut_rub": cut_rub,
        "measured": measured,
        "lane": lane,
        "basis": basis,
    }


def tacts_from_reports(reports: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Замеры тактов из отчётов сторожа, по одному на кабинет и день.

    Замер лежит НЕ в корне отчёта, а внутри кабинета:
    report['accounts'][i]['tact_effect'] (agent_e1_watchdog.main собирает
    accounts из watch() по кабинетам). Чтение из корня вернуло бы пусто молча,
    и месячная выгода печаталась бы нулями при живых замерах.

    Ключ схлопывания — ПАРА «кабинет + день такта», а не один день: у каждого
    кабинета свои обработанные кампании и свой расход, и это разные замеры
    одного дня. Дублем считается повторный прогон сторожа — он меряет ровно
    ту же пару, и побеждает последний: у него шире хвост фактов. Порядок
    reports поэтому обязан быть по возрастанию времени прогона (так их и
    отдаёт agent_db.load_stage_reports).
    """
    latest: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for report in reports or ():
        for account in (report or {}).get("accounts") or []:
            effect = account.get("tact_effect") or {}
            if not effect:
                continue
            # Сторож ходит каждый день, а такт с применёнными действиями —
            # не каждый: день без действий он возвращает как «unknown» с этой
            # причиной. Это не неизмеренный такт, а его отсутствие; считать
            # такие дни в n_tacts значило бы за месяц показать «измерен 1 из
            # 30» там, где тактов было три.
            if effect.get("reason") == NO_ACTIONS_REASON:
                continue
            login = str(account.get("account") or "")
            latest[(login, str(effect.get("tact_date")))] = {
                "account": login, **effect}
    return [latest[key] for key in sorted(latest)]


def period_value(tacts: List[Dict[str, Any]],
                 actions: List[Dict[str, Any]],
                 value_per_lead_by_campaign: Dict[str, float]
                 ) -> Dict[str, Any]:
    """Выгода агента за период — суммой, с интервалом и с долей неизмеренного.

    Такты и действия НЕ складываются в одно число намеренно: такт меряет цену
    лида относительно заповедника, действие — прирост лидов относительно
    базового темпа, и сложить их значило бы засчитать одни и те же деньги
    дважды. Они стоят рядом, и рядом же стоит доля неизмеренного — без неё
    сумма выглядит полным ответом, будучи ответом по измеренной части.

    Интервал складывается ПОКОМПОНЕНТНО (нижние к нижним, верхние к верхним).
    Это консервативная граница: она предполагает, что такты ошибаются в одну
    сторону, — а сумма независимых интервалов по корню была бы утверждением о
    независимости, которого замер не даёт.
    """
    prices = {str(k): v for k, v in (value_per_lead_by_campaign or {}).items()}

    saved = 0.0
    interval_low, interval_high = 0.0, 0.0
    has_interval = False
    n_tacts_measured = 0
    for tact in tacts or ():
        row = tact_value(tact)
        saved += row["saved_rub"]
        n_tacts_measured += 1 if row["measured"] else 0
        if row["interval_rub"] is not None:
            has_interval = True
            interval_low += row["interval_rub"][0]
            interval_high += row["interval_rub"][1]

    earned, cut = 0.0, 0.0
    n_actions_measured = 0
    by_lane: Dict[str, Dict[str, float]] = {}
    for action in actions or ():
        row = action_value(action, prices.get(str(action.get("object_id"))))
        earned += row["earned_rub"]
        cut += row["cut_rub"]
        n_actions_measured += 1 if row["measured"] else 0
        slot = by_lane.setdefault(row["lane"], {"earned_rub": 0.0,
                                                "cut_rub": 0.0, "n": 0})
        slot["earned_rub"] += row["earned_rub"]
        slot["cut_rub"] += row["cut_rub"]
        slot["n"] += 1

    n_tacts, n_actions = len(tacts or ()), len(actions or ())
    observations = n_tacts + n_actions
    unmeasured = observations - n_tacts_measured - n_actions_measured
    return {
        "saved_rub": round(saved, 2),
        "did_interval_rub": ([round(interval_low, 2), round(interval_high, 2)]
                             if has_interval else None),
        "n_tacts": n_tacts,
        "n_tacts_measured": n_tacts_measured,
        "earned_rub": round(earned, 2),
        "cut_rub": round(cut, 2),
        "n_actions": n_actions,
        "n_actions_measured": n_actions_measured,
        # Доля округляется до ЧЕТЫРЁХ знаков, а не до двух, как деньги: при
        # сотнях наблюдений два знака превратили бы «измерено не всё» в ровный
        # ноль, то есть ровно в то утверждение, которого эта доля не делает.
        "unmeasured_share": (round(unmeasured / observations, 4)
                             if observations else 0.0),
        "by_lane": {lane: {"earned_rub": round(slot["earned_rub"], 2),
                           "cut_rub": round(slot["cut_rub"], 2),
                           "n": int(slot["n"])}
                    for lane, slot in sorted(by_lane.items())},
    }
