# -*- coding: utf-8 -*-
"""
sync/agent/ideas/abtests.py — генератор идей: A/B-тесты по каталогу типов.

Третий из пяти генераторов Ф13. Берёт кампанию и спрашивает у ЗАКРЫТОГО
каталога типов тестов, какие из них на ней сегодня осмысленны.

**Каталог закрытый и полный.** В нём перечислены все типы, включая те, чьи
рычаги ещё не написаны (Ф15, задачи 21–24) и тот, чей рычаг вообще не запись
в API, а наряд билдеру (Ф14). Выкинуть ненаписанные значило бы забыть о них:
рычаг появится, а каталог о нём не узнает. Поэтому они ЛЕЖАТ в каталоге, но
НЕ ПРЕДЛАГАЮТСЯ — и отказ виден строкой с причиной, а не отсутствием.

Что тип предлагается, решает allow-лист записи (guardrails.ALLOWED_ACTION_KINDS),
а не список в этом файле: он и есть правда о том, какие рычаги живы. Так
каталог расширяется сам собой в тот день, когда рычаг доезжает до allow-листа.

**Почему идеи класса 3, а не ставки.** Нагрузку теста строит ТОЛЬКО такт
записи и только от живого состояния кабинета: writer/budget.diff_budget
требует fetch_budget_state, лимит и цель CPA читаются из кабинета в момент
отправки. Расчётный такт собрать её не может, не соврав о текущем состоянии,
— а previous_state, описывающий вчерашнее состояние, это отправка вслепую.
Тот же довод, по которому генератор proven ставит только `bidmodifier.add`
и никогда `set`. Значит идея едет человеку на экран предложений и несёт всё,
чем предложение проверяется: рычаг, направление и шаг изменения, базу
сравнения, срок, смету и машинно проверяемый критерий.

**Сравнение «было/стало» запрещено.** Смену цели, стратегии или лимита нельзя
судить по одной кампании до и после: эффект неотличим от сезона и
переобучения стратегии. База сравнения — DiD против заповедника либо парная
кампания того же направления, и она названа в самой идее. Не названа заранее
— значит её выберут задним числом, ту, что даст нужный ответ. Нет ни
заповедника, ни пары — тест не предлагается вовсе.

**Пороги.** Ни один не заведён заново:

  * ОБЪЁМ — power.AB_MIN_EFF_LEADS = 400 эффективных лидов: столько нужно на
    сравнение двух плеч (Яндекс рекомендует 200 на плечо). Кампания, которой
    столько не набрать за допустимый срок, даёт не эксперимент, а трату.
  * СРОК ЗАМЕРА — не меньше experiments.HORIZON_DAYS: горизонт ставки
    объявлен там и там же читается сторожем.
  * ПЕРЕОБУЧЕНИЕ — writer/learning.LEARNING_COOLDOWN_DAYS сверху у тестов,
    сбрасывающих обучение: недели переобучения замеру не принадлежат, и
    считать их сроком теста значило бы судить его по чужому шуму.
  * ПРЕДЕЛ СРОКА — ideas/limits.py, общий с остальными генераторами.
  * КЛАСС ДЕЙСТВИЯ — writer/learning.learning_impact, а не своя таблица:
    что именно сбрасывает обучение, знает модуль рычагов.

**Что модуль не делает.** Не ходит в базу и не знает дат: на вход подают уже
собранные строки кампаний и контекст такта (сборку описывает задача 16а). Не
решает, применять ли идею. И не молчит об отбракованных: scan() возвращает их
списком с причиной, потому что «тестов не нашлось» и «тесты были, но все
отсеяны» — разные новости.
"""

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sync.agent import power as power_mod
from sync.agent.experiments import HORIZON_DAYS, METRIC
from sync.agent.ideas import limits
from sync.agent.writer import guardrails, lanes as lanes_mod, tier as tier_mod
from sync.agent.writer import learning as learning_mod
from sync.agent.writer.budget import BUDGET_KIND, MICROS

# Имя источника в реестре. Входит в idea_id, поэтому меняться не может: смена
# завела бы все идеи генератора заново, с пустой историей и снятым отказом.
SOURCE = "abtest"

# Шаг бюджетного теста — ровно тот, который стратегию не сбивает
# (writer/learning.BUDGET_SAFE_DELTA). Больше — и тест мерил бы переобучение
# вместо себя, а горизонт вырос бы на две недели кулдауна.
BUDGET_STEP = learning_mod.BUDGET_SAFE_DELTA

# Базы сравнения. «Было/стало» здесь нет и быть не может — см. шапку.
COMPARISON_DID_VS_HOLDOUT = "did_vs_holdout"
COMPARISON_PAIRED = "paired_campaign"
COMPARISON_BASES = (COMPARISON_DID_VS_HOLDOUT, COMPARISON_PAIRED)

# Каталог типов тестов: тип → рычаг и параметры изменения. lever=None значит
# «рычага записи у типа нет вовсе» — так устроен только тест креативов, он
# едет нарядом билдеру (Ф14, задача 17), а не в API Директа.
CATALOGUE: Dict[str, Dict[str, Any]] = {
    # Прибавка лимита предлагается ТОЛЬКО кампании, у которой лимит расход
    # связывает. Это не осторожность, а замер: из 62 кампаний кабинета лимит
    # связывал у 9 (август 2026), у остальных прибавка не меняет ничего —
    # кампания и так не выбирает того, что ей уже разрешено. Тот же довод
    # стоит рельсой в самом рычаге (budget.NOT_APPLICABLE_UP_REASON), и
    # предлагать человеку тест, который рычаг потом отвергнет, значит
    # обещать проверку, которой не будет.
    "budget_up": {"lever": BUDGET_KIND, "direction": "up",
                  "step": BUDGET_STEP, "needs_binding_limit": True},
    "budget_down": {"lever": BUDGET_KIND, "direction": "down",
                    "step": BUDGET_STEP},
    "tcpa": {"lever": "tcpa.set", "direction": "down", "step": BUDGET_STEP},
    "goal": {"lever": "goal.set", "direction": "switch"},          # задача 21
    "strategy": {"lever": "strategy.set", "direction": "switch"},  # задача 22
    "schedule": {"lever": "schedule.set", "direction": "switch"},
    "audience": {"lever": "audience.add", "direction": "add"},     # задача 23
    "geo": {"lever": "geo.set", "direction": "switch"},            # задача 24
    "placements": {"lever": "placement.exclude", "direction": "exclude"},
    "creatives": {"lever": None, "direction": "switch"},           # Ф14
}

REASON_NO_ADDRESS = "у строки нет кабинета или кампании"
REASON_HOLDOUT = (
    "кампания в заповеднике: он линейка, которой меряют весь кабинет, и "
    "тронутая линейка не меряет ничего")
REASON_NO_VOLUME = "у кампании не посчитаны эффективные лиды или окно"
REASON_NO_COST = "у кампании нет расхода: смету теста не от чего отмерить"
REASON_THIN_POWER = (
    f"кампании не набрать {power_mod.AB_MIN_EFF_LEADS} эффективных лидов на "
    "сравнение плеч за допустимый срок: вердикт вышел бы на шуме")
REASON_NO_COMPARISON = (
    "сравнивать не с чем: ни заповедника, ни парной кампании направления, а "
    "сравнение «было/стало» на одной кампании меряет сезон и переобучение, "
    "а не наше изменение")
REASON_LOST_BEFORE = (
    "этот тест на этой кампании уже проигран своей же ставкой: повтор "
    "опровергнутой гипотезы стоит денег и не покупает ответа")
REASON_NO_LEVER = "рычага записи у типа теста ещё нет — предлагать нечем"
REASON_LIMIT_NOT_BINDING = (
    "лимит расход не связывает: прибавка не изменит ничего — кампания не "
    "выбирает и того, что ей уже разрешено")
REASON_LIMIT_UNKNOWN = (
    "неизвестно, связывает ли лимит расход: «не знаем» — не то же самое, что "
    "«связывает», и тест был бы предложен вслепую")
REASON_LEARNING_COOLDOWN = (
    f"обучение сбрасывалось меньше {learning_mod.LEARNING_COOLDOWN_DAYS} дней "
    "назад: сбрасывающий тест померил бы переобучение, а не себя")
REASON_RESET_UNKNOWN = (
    "неизвестно, когда сбрасывалось обучение: «не знаем» — не то же самое, "
    "что «давно», и сбрасывающий тест мог бы лечь поверх свежего сброса")


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None   # NaN — то же «неизвестно»


def _text(value: Any) -> str:
    return str(value or "").strip()


def _skip(campaign_id: str, reason: str,
          test_kind: Optional[str] = None) -> Dict[str, Any]:
    """Отбракованный повод с причиной.

    Причина обязательна: тест, исчезнувший молча, неотличим от теста,
    которого не было, — и первый же вопрос «почему генератор ничего не
    предложил» превращается в археологию по коду.
    """
    row = {"campaign_id": campaign_id, "reason": reason}
    if test_kind is not None:
        row["test_kind"] = test_kind
    return row


def _resets_learning(entry: Dict[str, Any]) -> bool:
    """Сбрасывает ли тип теста обучение стратегии.

    Спрашиваем writer/learning.py по рычагу, а не объявляем в каталоге рядом
    с типом: что именно сбрасывает обучение — знание о кабинете, и оно живёт
    в одном месте. Объяви здесь — и таблица типов разъехалась бы с правилом
    молча, в ту сторону, которую никто не заметит.

    Бюджетный тест судится ВЕЛИЧИНОЙ шага, поэтому спрашивается пробным
    действием с тем же отношением лимитов, каким тест и поедет: сам шаг
    (BUDGET_STEP) взят из learning.BUDGET_SAFE_DELTA, так что ответ «safe»
    здесь не совпадение, а следствие — и сдвинется вместе с порогом.

    «unknown» читается как сброс. Обратный дефолт («не знаем — значит
    безопасно») тихо пропускал бы каждый новый рычаг: ровно тот довод, по
    которому allow-лист рельс устроен списком разрешённого, а не запрещённого.
    """
    lever = entry.get("lever")
    if not lever:
        return True
    probe: Dict[str, Any] = {"action_kind": lever}
    if lever in learning_mod.BUDGET_KINDS:
        base = MICROS
        step = float(entry.get("step") or 0.0)
        ratio = (1.0 + step) if entry.get("direction") == "up" else (1.0 - step)
        probe["payload"] = {"WeeklySpendLimit": int(base * ratio)}
        probe["previous_state"] = {"WeeklySpendLimit": base}
    return learning_mod.learning_impact(probe) != "safe"


def _comparison(row: Dict[str, Any], ctx: Dict[str, Any],
                ) -> Tuple[Optional[str], Optional[str]]:
    """База сравнения и объект, с которым сравниваем.

    Заповедник сильнее пары: DiD против него отделяет наше изменение от
    сезона, общего для всего кабинета. Пара того же направления — вторая
    база, законная, пока заповедника нет (его формирует задача 25).
    """
    holdout = {str(h) for h in (ctx.get("holdout_ids") or set())}
    if holdout:
        return COMPARISON_DID_VS_HOLDOUT, sorted(holdout)[0]
    pair = _text(row.get("pair_campaign_id"))
    if pair:
        return COMPARISON_PAIRED, pair
    return None, None


def _horizon(daily_leads: float, resets: bool) -> int:
    """Срок теста: накопление объёма плюс переобучение, если тест его сбивает.

    Дни переобучения замеру не принадлежат — стратегия в них работает хуже, и
    засчитать их в срок значило бы судить тест по чужому шуму. Поэтому они
    прибавляются к сроку, а не входят в него.
    """
    days = int(math.ceil(power_mod.AB_MIN_EFF_LEADS / daily_leads))
    horizon = max(days, HORIZON_DAYS)
    if resets:
        horizon += learning_mod.LEARNING_COOLDOWN_DAYS
    return horizon


def lost_before(ctx: Dict[str, Any]) -> set:
    """Пары (кампания, тип теста), проигранные своей же ставкой.

    Вход — registry.lost_lessons (ctx["lost_tests"]): адреса опровергнутых
    гипотез, без чисел. Генератор детерминирован, и без этой памяти он
    предложит проигравший тест тем же тактом — агент будет вечно проверять
    одну и ту же опровергнутую гипотезу.

    Запрет АДРЕСНЫЙ: проигрыш одного типа теста не запрещает остальные, а
    проигрыш на одной кампании — тот же тест на другой. Иначе первая же
    неудача выключала бы генератор целиком.
    """
    out = set()
    for lesson in (ctx or {}).get("lost_tests") or ():
        subject = lesson.get("subject") if isinstance(lesson, dict) else None
        subject = subject if isinstance(subject, dict) else lesson
        if not isinstance(subject, dict):
            continue
        campaign_id = _text(subject.get("campaign_id"))
        test_kind = _text(subject.get("test_kind"))
        if campaign_id and test_kind:
            out.add((campaign_id, test_kind))
    return out


def _one(row: Dict[str, Any], test_kind: str, entry: Dict[str, Any],
         ctx: Dict[str, Any], facts: Dict[str, Any],
         ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Кампания × тип теста → (идея, отбраковка)."""
    campaign_id = facts["campaign_id"]
    if (campaign_id, test_kind) in facts["lost_before"]:
        return None, _skip(campaign_id, REASON_LOST_BEFORE, test_kind)

    lever = entry.get("lever")
    if not lever or lever not in guardrails.ALLOWED_ACTION_KINDS:
        return None, _skip(campaign_id, REASON_NO_LEVER, test_kind)

    if entry.get("needs_binding_limit"):
        binds = facts["limit_binds"]
        if binds is None:
            return None, _skip(campaign_id, REASON_LIMIT_UNKNOWN, test_kind)
        if not binds:
            return None, _skip(campaign_id, REASON_LIMIT_NOT_BINDING, test_kind)

    resets = _resets_learning(entry)
    if resets:
        since = facts["days_since_learning_reset"]
        if since is None:
            return None, _skip(campaign_id, REASON_RESET_UNKNOWN, test_kind)
        if since < learning_mod.LEARNING_COOLDOWN_DAYS:
            return None, _skip(campaign_id, REASON_LEARNING_COOLDOWN, test_kind)

    horizon = _horizon(facts["daily_leads"], resets)
    if horizon > limits.max_horizon(ctx):
        return None, _skip(campaign_id, REASON_THIN_POWER, test_kind)

    change = {"lever": lever, "direction": entry.get("direction")}
    if entry.get("step") is not None:
        change["step"] = entry["step"]

    return {
        "source": SOURCE,
        "account": facts["account"],
        # Адрес теста — кампания и тип, и только они. Срок и смета плавают от
        # прогона к прогону, и войди они сюда, idea_id менялся бы каждым
        # прогоном: пустая история и снятый отказ человека на том же тесте.
        "subject": {"kind": SOURCE, "campaign_id": campaign_id,
                    "test_kind": test_kind, "resets_learning": resets},
        # Класс 3: нагрузку рычага расчётный такт собрать не может — см. шапку.
        "tier": tier_mod.TIER_PROPOSAL,
        "lane": lanes_mod.LANE_PROPOSAL,
        # Ценность теста — информация, а не рубли: она станет рублями только
        # если тест выиграет, и выдуманное число здесь вынесло бы тесты в
        # начало очереди реестра обещанием, которого никто не считал.
        # Непосчитанная цена уводит идею в хвост очереди (registry.rank) —
        # это правильно и честно.
        "expected_rub": None,
        # Под ударом — весь расход кампании за срок замера, а не дельта
        # рычага: проигравший тест портит кампанию целиком на всё это время.
        "test_cost_rub": round(facts["daily_cost"] * horizon, 2),
        "horizon_days": horizon,
        "success_rule": {
            "metric": METRIC,
            "op": "<=",
            # Побить свою же нынешнюю цену эффективного лида. Порог не
            # выдуман: это цена, по которой кампания покупает лиды СЕЙЧАС.
            "value": round(facts["eff_cpl"], 2),
            "comparison": facts["comparison"],
        },
        "detail": {
            "change": change,
            "comparison_object_id": facts["comparison_object_id"],
            "eff_leads": round(facts["eff_leads"], 2),
            "eff_cpl_rub": round(facts["eff_cpl"], 2),
            "daily_eff_leads": round(facts["daily_leads"], 4),
            "window_days": facts["window_days"],
            "direction": facts["direction"],
        },
    }, None


def _facts(row: Dict[str, Any], ctx: Dict[str, Any],
           ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Строка кампании → числа, общие для всех типов теста, либо отбраковка."""
    campaign_id = _text(row.get("campaign_id"))
    account = _text(row.get("account")) or _text(ctx.get("account"))
    if not campaign_id or not account:
        return None, _skip(campaign_id, REASON_NO_ADDRESS)

    holdout = {str(h) for h in (ctx.get("holdout_ids") or set())}
    if row.get("in_holdout") or campaign_id in holdout:
        return None, _skip(campaign_id, REASON_HOLDOUT)

    eff_leads = _number(row.get("eff_leads"))
    window = _number(row.get("window_days"))
    if not eff_leads or eff_leads <= 0 or not window or window <= 0:
        return None, _skip(campaign_id, REASON_NO_VOLUME)

    cost = _number(row.get("cost_rub"))
    if not cost or cost <= 0:
        return None, _skip(campaign_id, REASON_NO_COST)

    comparison, against = _comparison(row, ctx)
    if comparison is None:
        return None, _skip(campaign_id, REASON_NO_COMPARISON)

    since = _number(row.get("days_since_learning_reset"))
    # Связывает ли лимит расход — три значения, три разных факта: ключа нет
    # (состояние кабинета не снято), False (не связывает), True (связывает).
    # Подмена первого вторым предложила бы прибавку вслепую.
    binds = row.get("limit_binds")
    return {
        "campaign_id": campaign_id,
        "account": account,
        "direction": _text(row.get("direction")),
        "eff_leads": eff_leads,
        "window_days": int(window),
        "daily_leads": eff_leads / float(window),
        "daily_cost": cost / float(window),
        "eff_cpl": cost / eff_leads,
        "comparison": comparison,
        "comparison_object_id": against,
        "days_since_learning_reset": None if since is None else int(since),
        "limit_binds": None if binds is None else bool(binds),
        "lost_before": lost_before(ctx),
    }, None


def _order(idea: Dict[str, Any]) -> Tuple[int, float, str]:
    """Дешёвый тест вперёд.

    Цена переобучения не в смете, а в порядке: сбрасывающий тест стоит
    кабинету двух недель худшей работы стратегии сверх собственного риска, и
    предлагать его первым, когда рядом лежит тест без сброса, — значит
    продавать человеку дорогое под видом равного. Дальше — по сроку: короткий
    тест раньше отдаёт вердикт, а значит раньше освобождает кампанию под
    следующий. Хвост доопределён именем типа, чтобы порядок был
    детерминирован на одних и тех же данных.
    """
    subject = idea["subject"]
    return (1 if subject["resets_learning"] else 0,
            float(idea["horizon_days"]),
            str(subject["test_kind"]))


def scan(rows: Sequence[Dict[str, Any]],
         ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Кампании кабинета → {"ideas": [...], "skipped": [...]}.

    Отбракованные возвращаются рядом с принятыми и с причиной — и кампании
    целиком, и отдельные типы теста: «кампания в заповеднике» и «у типа ещё
    нет рычага» ведут к разным следующим шагам.
    """
    ctx = ctx or {}
    ideas: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for row in rows or ():
        if not isinstance(row, dict):
            continue
        facts, refusal = _facts(row, ctx)
        if facts is None:
            skipped.append(refusal)
            continue
        for test_kind in sorted(CATALOGUE):
            idea, refusal = _one(row, test_kind, CATALOGUE[test_kind],
                                 ctx, facts)
            if idea is not None:
                ideas.append(idea)
            else:
                skipped.append(refusal)

    ideas.sort(key=_order)
    return {"ideas": ideas, "skipped": skipped}


def candidates(rows: Sequence[Dict[str, Any]],
               ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Только идеи — форма вызова для реестра (registry.upsert)."""
    return scan(rows, ctx)["ideas"]
