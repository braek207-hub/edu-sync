# -*- coding: utf-8 -*-
"""
sync/agent/ideas/consolidate.py — генератор идей: вынос доказанных связок
в отдельную кампанию.

Второй из пяти генераторов Ф13. Павел сформулировал задачу прямо:
«понадёргать с разных кампаний кабинета лучшие связки и потестировать
отдельной РК».

**Класс идеи определяет НАРЯД.** Рычаг выноса — наряд билдеру (Ф14), а не
запись в API Директа: новой кампании ещё нет, значит нет и объекта, которому
можно что-то отправить. Наряд собрался целиком — идея ставка (класс 2) и
несёт его колонкой action. Не собрался (у доноров не прочитаны цель и
счётчик, доноры разошлись в цели) — предложение человеку, класс 3, с
названной причиной в detail.launch_refusal. И в том, и в другом случае идея
обязана нести всё, чем проверяется: цену теста, срок, машинно проверяемый
критерий и доказательства.

**Адрес и доказательства — разные вещи.** subject здесь предельно скупой:
кабинет задан отдельным полем, а адресом выноса служит НАПРАВЛЕНИЕ. Состав
связок-доноров плавает от прогона к прогону, и войди он в subject — idea_id
менялся бы каждый день: пустая история, снятый отказ человека, тот же по сути
вынос под новым идентификатором. Поэтому список доноров, их кампании, суммы и
план кросс-минусовки едут колонкой detail, которая обновляется каждой
находкой, но идентичности не задаёт (registry._check_detail).

**Кросс-минусовка обязательна.** «Отдельная РК» без минусовки фраз у доноров
— это не тест, а гарантированное удорожание: две наши кампании начинают
торговаться друг с другом на одном аукционе и поднимают себе цену клика.
Поэтому связка, фразу которой у донора заминусовать НЕЛЬЗЯ (операторы языка
запросов, слишком длинная фраза — negatives.phrase_is_valid), из выноса
исключается с названной причиной, а не уезжает молча.

**Направления не смешиваются.** Кампания собирается под одно направление:
у ВПО и СПО разные посадочные, разная аудитория и разная экономика, и
кампания, склеенная из двух, не даёт вердикта ни по одному.

**Пороги.** Ни один не заведён заново:

  * ОБЪЁМ — power.MIN_EXPECTED_PAYMENTS = 25: столько ожидаемых оплат нужно
    накопить, чтобы решение на объекте вообще имело силу (MDE ≈ 30 % при базе
    1.4 %). Кампания, которой не хватит событий на вердикт, — не эксперимент,
    а трата.
  * СРОК НАКОПЛЕНИЯ — сколько дней при нынешнем темпе доноров нужно, чтобы
    этот объём набрался, но не меньше experiments.HORIZON_DAYS: горизонт
    замера ставки объявлен там и там же читается сторожем.
  * СОЗРЕВАНИЕ — db.CRM_MATURITY_WINDOW_DAYS = 14 сверху: оплата приходит
    позже лида, и горизонт, кончающийся в день последнего клика, судил бы
    кампанию по недозревшей когорте.
  * ЗАПАС ПО λ — portfolio.GROWTH_LAMBDA_MARGIN, тот же, которым генератор
    proven отделяет доказанное от «ровно на пороге».

**Что модуль не делает.** Не ходит в базу и не знает дат: на вход подают уже
собранные связки и контекст такта (сборку описывает задача 16а). Не решает,
применять ли идею. И не молчит об отбракованных: scan() возвращает их
списком с причиной, потому что «связок не нашлось» и «связки были, но все
отсеяны» — разные новости.
"""

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sync.agent import build_order
from sync.agent import ladder as ladder_mod
from sync.agent import power as power_mod
from sync.agent.db import CRM_MATURITY_WINDOW_DAYS
from sync.agent.experiments import HORIZON_DAYS as STAKE_HORIZON_DAYS
from sync.agent.ideas import limits, registry
from sync.agent.portfolio import GROWTH_LAMBDA_MARGIN
from sync.agent.writer import lanes as lanes_mod
from sync.agent.writer import launch
from sync.agent.writer import negatives as negatives_mod
from sync.agent.writer import tier as tier_mod

# Имя источника в реестре. Входит в idea_id, поэтому меняться не может: смена
# завела бы все идеи генератора заново, с пустой историей и снятым отказом.
SOURCE = "consolidate"

# Предел срока — общий с остальными генераторами (ideas/limits.py): смысл у
# него один и тот же везде, а две копии одной ручки разъехались бы при первой
# же правке одной из них. Имена оставлены здесь ссылками, чтобы читателю
# генератора не приходилось искать, чем ограничен его горизонт.
MAX_HORIZON_KEY = limits.MAX_HORIZON_KEY
DEFAULT_MAX_HORIZON_DAYS = limits.DEFAULT_MAX_HORIZON_DAYS

REASON_NO_ADDRESS = "у связки нет кабинета, кампании-донора или направления"
REASON_NO_PHRASE = "у связки нет фразы: выносить в новую кампанию нечего"
REASON_NOT_MINUSABLE = (
    "фразу нельзя заминусовать у донора, а без кросс-минусовки вынос — не "
    "тест, а гарантированное удорожание: две наши кампании торгуются друг с "
    "другом на одном аукционе")
REASON_NO_LAMBDA = "порог кабинета λ не посчитан — сравнивать окупаемость не с чем"
REASON_THIN_MARGIN = (
    f"окупаемость выше λ, но без запаса ×{GROWTH_LAMBDA_MARGIN}: связка «ровно "
    "на пороге» не окупит отдельной кампании")
REASON_NO_COST = "у связки нет расхода: стартовый бюджет новой кампании не от чего отмерить"

GROUP_REASON_THIN_POWER = (
    f"направлению не набрать {power_mod.MIN_EXPECTED_PAYMENTS:g} ожидаемых "
    "оплат за допустимый срок: это не эксперимент, а трата")
GROUP_REASON_NO_CPA = (
    "у доноров не посчитана цена конверсии: критерий успеха не от чего "
    "отмерить — новая кампания обязана побить именно донорскую цену")
GROUP_REASON_NO_PAYMENTS = (
    "у выноса нет ожидаемых оплат (p_pay): порог значимости power.py считает "
    "именно их, и подменить их конверсиями значило бы мерить другое")
GROUP_REASON_THIN_MARGIN = (
    f"окупаемость выноса выше λ, но без запаса ×{GROWTH_LAMBDA_MARGIN}: "
    "кампания «ровно на пороге» не окупит переезда")
GROUP_REASON_NO_REVENUE = (
    "ожидаемые оплаты выноса не во что перевести: у доноров не посчитан "
    "средний чек, и окупаемость кампании осталась бы дробью без числителя")


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None   # NaN — то же «неизвестно»


def _text(value: Any) -> str:
    return str(value or "").strip()


def _skip(row: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Отбракованная связка с причиной.

    Причина обязательна: связка, исчезнувшая молча, неотличима от связки,
    которой не было, — и первый же вопрос «почему генератор ничего не
    предложил» превращается в археологию по коду.
    """
    return {
        "campaign_id": _text(row.get("campaign_id")),
        "phrase": _text(row.get("phrase") or row.get("query")),
        "direction": _text(row.get("direction")),
        "reason": reason,
    }




def _one(row: Dict[str, Any], ctx: Dict[str, Any],
         ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Связка → (донор для выноса, отбраковка). Ровно одно из двух непусто."""
    campaign_id = _text(row.get("campaign_id"))
    direction = _text(row.get("direction"))
    if not campaign_id or not direction:
        return None, _skip(row, REASON_NO_ADDRESS)

    phrase = negatives_mod.normalize_phrase(row.get("phrase") or row.get("query"))
    if not phrase:
        return None, _skip(row, REASON_NO_PHRASE)
    # Проверка кросс-минусовки идёт ДО экономики намеренно: связку, которую
    # нельзя защитить от самоконкуренции, не спасает никакая окупаемость.
    valid, why = negatives_mod.phrase_is_valid(phrase)
    if not valid:
        return None, _skip(row, f"{REASON_NOT_MINUSABLE} ({why})")

    lam = _number(ctx.get("lambda"))
    if lam is None or lam <= 0:
        return None, _skip(row, REASON_NO_LAMBDA)
    # Окупаемость связки ТРЁХЗНАЧНА, и это главное отличие от прежней версии.
    # Посчитана и ниже порога — отказ здесь: связка, которая не окупается,
    # не окупится и в новой кампании. НЕ посчитана — не отказ: лестница
    # отказалась судить связку «кампания × фраза», а не признала её плохой.
    #
    # Замер 28.08 показал, во что превращался прежний отказ по непосчитанной
    # окупаемости: 15 доноров из 17 отпадали на нём, до группировки не доживал
    # ни один, и отчёт прогона не мог сказать, молчит генератор или сломан.
    # Причина механическая: порог лестницы — 25 событий за окно, а связку
    # «кампания × фраза» он проходит у 61 строки из 37 318 (0,16 %), потому
    # что фраза столько кликов в одиночку не набирает. Экономика фразы при
    # этом УЖЕ проверена выше по конвейеру: в доноры попадают только кандидаты
    # расширения (objects.expansion_candidates) — с конверсиями и с ценой не
    # выше медианного CPA кабинета. Второй, непроходимый экономический гейт
    # рядом с проходимым был не строгостью, а выключателем.
    #
    # Поэтому вердикт об окупаемости переезжает на ГРУППУ (_group): кампания
    # создаётся под направление целиком, события её доноров складываются, и
    # порог применяется там, где статистика существует.
    romi = _number(row.get("romi"))
    if romi is not None and romi < lam * GROWTH_LAMBDA_MARGIN:
        return None, _skip(row, REASON_THIN_MARGIN)

    cost = _number(row.get("cost_rub"))
    if cost is None or cost <= 0:
        return None, _skip(row, REASON_NO_COST)

    donor = {
        "phrase": phrase,
        "donor_campaign_id": campaign_id,
        "direction": direction,
        "cost_rub": round(cost, 2),
        "conversions": _number(row.get("conversions")) or 0.0,
        "window_days": _number(row.get("window_days")),
        "romi": romi,
        # Чем лестница объяснила отказ судить связку. Нужна не здесь, а в
        # отказе ГРУППЫ: если объёма не набралось и вместе, человек обязан
        # прочитать причину лестницы, а не догадку генератора.
        "romi_reason": _text(row.get("romi_reason")) or None,
        # События и входы лестницы кампании-донора: из них группа считает свой
        # объём, когда сама связка его не набрала (bundles.query_donors).
        "counts": row.get("counts") or {},
        "pools": row.get("pools") or (),
        "avg_check": _number(row.get("avg_check")),
        # Настройки донора едут с ним: счётчик и цель новой кампании берутся
        # у них, а не из панели (launch.campaign_from_donors). Их отсутствие
        # не отбраковывает связку — оно оставляет идею предложением.
        "settings": row.get("settings"),
    }
    payments = _number(row.get("p_pay_sum"))
    if payments is not None:
        donor["p_pay_sum"] = round(payments, 4)
    return donor, None


def _cannibalization(donors: List[Dict[str, Any]]) -> Dict[str, Any]:
    """План кросс-минусовки: у какого донора какие фразы гасим.

    Фразы уже нормализованы и проверены (negatives.phrase_is_valid) на входе:
    план, который донор не примет, — это не план, а обещание разобраться
    потом, когда обе кампании уже торгуются друг с другом.
    """
    by_campaign: Dict[str, List[str]] = {}
    for donor in donors:
        phrases = by_campaign.setdefault(donor["donor_campaign_id"], [])
        if donor["phrase"] not in phrases:
            phrases.append(donor["phrase"])
    return {
        "donor_negatives": [
            {"campaign_id": campaign_id, "phrases": sorted(phrases)}
            for campaign_id, phrases in sorted(by_campaign.items())
        ],
        # Вид рычага назван явно: минусовка у доноров поедет тем же
        # negative.add, что и обычная гигиена, — второго механизма для неё
        # заводить не нужно, и наряд билдеру обязан это знать.
        "lever": negatives_mod.NEGATIVE_KIND,
    }


def _economics(donors: List[Dict[str, Any]],
               ) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """Доноры направления → (ожидаемые оплаты, выручка, причина отказа).

    Считается в два приёма, и это не оптимизация, а разная природа слагаемых.
    Связка, которой лестница вердикт ДАЛА, приносит своё число как есть:
    пересчитывать его здесь значило бы завести второго судью рядом с
    лестницей. Связка, которой не дала, вносит не ноль и не догадку, а СВОИ
    СОБЫТИЯ — они складываются с событиями таких же связок ЕЁ КАМПАНИИ, и
    лестница спрашивается один раз про сумму.

    Складывать по кампании, а не по направлению целиком, обязательно:
    коэффициенты перехода и средний чек лестница берёт у кампании, и общий
    прогон по направлению применил бы к событиям одной кампании воронку
    другой. Направление же остаётся адресом кампании, а не единицей расчёта.

    Ноль оплат в ответе законен и означает «объёма нет»; None означает
    «посчитать нечем» — и это разные новости, поэтому третьим значением едет
    причина, а не пустая сумма.
    """
    payments = 0.0
    revenue = 0.0
    unresolved: Dict[str, List[Dict[str, Any]]] = {}
    for donor in donors:
        own = _number(donor.get("p_pay_sum"))
        if own is None:
            unresolved.setdefault(donor["donor_campaign_id"], []).append(donor)
            continue
        payments += own
        romi = _number(donor.get("romi"))
        if romi is None:
            return None, None, (donor.get("romi_reason")
                                or GROUP_REASON_NO_REVENUE)
        revenue += romi * donor["cost_rub"]

    for campaign_id in sorted(unresolved):
        rows = unresolved[campaign_id]
        counts: Dict[str, float] = {}
        for donor in rows:
            for step, value in (donor.get("counts") or {}).items():
                counts[step] = counts.get(step, 0.0) + (_number(value) or 0.0)
        if not any(counts.values()):
            # Ни оплат, ни событий: складывать нечего. Не «мало» — нечем.
            return None, None, GROUP_REASON_NO_PAYMENTS
        result = ladder_mod.ladder(counts, rows[0].get("pools") or (),
                                   avg_check=rows[0].get("avg_check"))
        got = _number(result.get("expected_payments"))
        if got is None:
            return None, None, (_text(result.get("reason"))
                                or ladder_mod.NO_STEP_REASON)
        payments += got
        earned = _number(result.get("expected_revenue"))
        if earned is None:
            return None, None, (rows[0].get("romi_reason")
                                or GROUP_REASON_NO_REVENUE)
        revenue += earned
    return payments, revenue, None


def _group(direction: str, donors: List[Dict[str, Any]], ctx: Dict[str, Any],
           ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Доноры одного направления → (идея выноса, отбраковка группы)."""
    account = _text(ctx.get("account"))
    windows = [d["window_days"] for d in donors if d["window_days"]]
    window = max(windows) if windows else None
    cost = sum(d["cost_rub"] for d in donors)
    conversions = sum(d["conversions"] for d in donors)

    payments, revenue, refusal = _economics(donors)
    if refusal is not None:
        return None, {"direction": direction, "reason": refusal}

    # Порог окупаемости — тот же, что был у связки, и применён к тому объекту,
    # который на самом деле создаётся: к кампании направления целиком.
    lam = _number(ctx.get("lambda"))
    if lam is not None and lam > 0 and cost > 0 and revenue is not None:
        if revenue / cost < lam * GROWTH_LAMBDA_MARGIN:
            return None, {"direction": direction,
                          "reason": GROUP_REASON_THIN_MARGIN}

    if not window or window <= 0 or not payments or payments <= 0:
        return None, {"direction": direction, "reason": GROUP_REASON_THIN_POWER}

    # Сколько дней при нынешнем темпе доноров копится порог значимости. Темп
    # берётся у доноров, а не выдумывается для новой кампании: она собирается
    # ИЗ ИХ ЖЕ трафика, и другого основания для прогноза объёма нет.
    daily_payments = payments / float(window)
    days_to_power = int(math.ceil(power_mod.MIN_EXPECTED_PAYMENTS / daily_payments))
    horizon = max(days_to_power, STAKE_HORIZON_DAYS) + CRM_MATURITY_WINDOW_DAYS
    if horizon > limits.max_horizon(ctx):
        return None, {"direction": direction, "reason": GROUP_REASON_THIN_POWER}

    if conversions <= 0:
        return None, {"direction": direction, "reason": GROUP_REASON_NO_CPA}
    donor_cpa = cost / conversions

    daily_cost = cost / float(window)
    value_per_payment = _number(ctx.get("value_per_payment_rub"))
    expected_payments = daily_payments * horizon

    idea = {
        "source": SOURCE,
        "account": account,
        # Адрес выноса — направление кабинета, и только оно. Состав доноров
        # плавает, и войди он сюда, idea_id менялся бы каждым прогоном.
        "subject": {"kind": SOURCE, "direction": direction},
        # Класс идеи назначается ниже: с нарядом это ставка (рычаг есть —
        # билдер), без наряда — предложение человеку.
        "tier": tier_mod.TIER_PROPOSAL,
        "lane": lanes_mod.LANE_PROPOSAL,
        "expected_rub": (round(expected_payments * value_per_payment, 2)
                         if value_per_payment is not None else None),
        # Цена теста — деньги, которые новая кампания проживёт за горизонт по
        # нынешнему темпу доноров. Это не «новые» деньги: тот же трафик просто
        # переезжает, — но под ударом они, и очередь реестра сравнивает идеи
        # по ценности НА РУБЛЬ проверки.
        "test_cost_rub": round(daily_cost * horizon, 2),
        "horizon_days": int(horizon),
        # Критерий — побить донорскую цену конверсии. Порог не выдуман: это та
        # самая цена, по которой связки покупаются СЕЙЧАС, и вынос, который её
        # не улучшил, не окупил переезда. База сравнения названа явно.
        "success_rule": {
            "metric": "cpa_rub",
            "op": "<=",
            "value": round(donor_cpa, 2),
            "comparison": "vs_donors",
        },
        "detail": {
            "queries": [
                {"phrase": d["phrase"],
                 "donor_campaign_id": d["donor_campaign_id"],
                 "cost_rub": d["cost_rub"],
                 "conversions": d["conversions"],
                 # Ожидаемые оплаты фразы — ТОЛЬКО когда лестница их дала.
                 # Ноль вместо «неизвестно» читался бы на экране как «фраза
                 # не приносит оплат», хотя лестница отказалась судить её по
                 # объёму, а не признала пустой.
                 "p_pay_sum": d.get("p_pay_sum")}
                for d in sorted(donors, key=lambda d: (-d["cost_rub"],
                                                       d["phrase"]))
            ],
            "donor_cost_rub": round(cost, 2),
            "donor_conversions": round(conversions, 4),
            "donor_cpa_rub": round(donor_cpa, 2),
            "expected_payments": round(expected_payments, 2),
            "window_days": int(window),
            "cannibalization": _cannibalization(donors),
        },
    }
    return _with_order(idea, donors, donor_cpa=donor_cpa, window=int(window)), None


def _with_order(idea: Dict[str, Any], donors: List[Dict[str, Any]], *,
                donor_cpa: float, window: int) -> Dict[str, Any]:
    """Идея выноса + наряд билдеру, если наряд собирается.

    До Ф14 вынос был предложением по построению: рычага у него не
    существовало. Теперь рычаг есть — наряд, — и класс идеи определяется тем,
    собрался ли наряд ЦЕЛИКОМ. Собрался: это ставка, у неё есть чем
    распорядиться. Не собрался (у доноров не прочитаны цель и счётчик, они
    разошлись в цели): предложение с названной причиной, и человек видит на
    экране не «генератор молчит», а чего именно не хватило.

    Наряд кладётся в колонку action, потому что такт записи — ДРУГОЙ прогон и
    читает идеи из базы: всё, чего нет в колонке, для него не существует.
    Кросс-минусовка доноров туда не кладётся: её нельзя посчитать заранее —
    она строится поверх СВЕЖЕГО чтения списков кабинета, и такт записи
    разворачивает связку сам (launch.build_all).

    idea_id считается здесь тем же реестровым правилом, которым его назначит
    upsert: наряд без него — сирота, вердикт которой некуда вернуть, а
    реестр сверит два числа и упадёт, если они разойдутся.
    """
    campaign, refusal = launch.campaign_from_donors(
        donors, donor_cpa=donor_cpa, window_days=window)
    if campaign is None:
        idea["detail"]["launch_refusal"] = refusal
        return idea

    known = dict(idea)
    known["idea_id"] = registry.idea_id(SOURCE, idea["subject"], idea["account"])
    try:
        order = build_order.from_idea(known, campaign=campaign,
                                      account=idea["account"])
        action = launch.build(order)
    except ValueError as error:
        # Наряд не прошёл собственный валидатор — дефект генератора, но не
        # повод потерять находку: идея едет предложением, причина видна.
        idea["detail"]["launch_refusal"] = str(error)
        return idea

    idea["idea_id"] = known["idea_id"]
    idea["tier"] = tier_mod.TIER_BET
    idea["lane"] = lanes_mod.LANE_LAUNCH
    idea["action"] = action
    return idea


def scan(rows: Sequence[Dict[str, Any]],
         ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Связки кабинета → {"ideas": [...], "skipped": [...]}.

    Отбракованные возвращаются рядом с принятыми и с причиной — и связки, и
    целые направления: «поводов не было» и «поводы были, но направлению не
    набрать объёма» ведут к разным следующим шагам.
    """
    ctx = ctx or {}
    skipped: List[Dict[str, Any]] = []
    by_direction: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        donor, refusal = _one(row, ctx)
        if donor is None:
            skipped.append(refusal)
            continue
        by_direction.setdefault(donor["direction"], []).append(donor)

    ideas: List[Dict[str, Any]] = []
    for direction in sorted(by_direction):
        idea, refusal = _group(direction, by_direction[direction], ctx)
        if idea is not None:
            ideas.append(idea)
        else:
            skipped.append(refusal)
    return {"ideas": ideas, "skipped": skipped}


def candidates(rows: Sequence[Dict[str, Any]],
               ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Только идеи — форма вызова для реестра (registry.upsert)."""
    return scan(rows, ctx)["ideas"]
