# -*- coding: utf-8 -*-
"""
sync/agent/build_order.py — наряд билдеру: единственный способ агента завести
кампанию.

Все прочие рычаги агента правят СУЩЕСТВУЮЩИЙ объект: ставку, бюджет, цель,
расписание. Наряд — другое: он создаёт объект, которого не было. Поэтому он
не элемент запроса к API, а файл, который пересекает границу двух
репозиториев: агент (edu-sync) пишет, билдер («EDU кампании») читает и
собирает по нему уровень.

**Контракт проверяется у получателя.** Форма наряда описана здесь и в
docs/BUILD-ORDER-CONTRACT.md, но правда о нём — в коде билдера (builder/
order.py) и в тесте на его стороне, который читает ПРИМЕР ИЗ ЭТОГО ЖЕ
репозитория (EXAMPLE_PATH) и превращает его в LevelConfig. Разъедься
спецификация с получателем — тест получателя покраснеет, а не бой.

**Что валидатор здесь сторожит, а билдер уже не сможет.** У билдера на входе
готовый файл: почему в нём нет доноров, он не узнает никогда. Поэтому целость
наряда проверяется на выходе агента:

  * КРОСС-МИНУСОВКА — каждая вынесенная фраза выключена У СВОЕГО донора.
    Проверка «поле не пустое» пропустила бы ровно тот случай, ради которого
    поле заведено: два донора, минусовка выписана одному. Новая кампания и
    донор остались бы в одном аукционе одного рекламодателя. Это измерено:
    на дистанте 73–75% московских ключей дублировались в РФ-версии со
    ставкой на треть ниже (builder/config.py, exclude_regions, 25.08.2026).
  * КРИТЕРИЙ И ГОРИЗОНТ — без них кампания не закрывается никогда: замер не
    знает, что считать удачей, и наряд остаётся в реестре навсегда.
  * ФАКТЫ ФРАЗ — вердикт наряда consolidate это ДЕНЬГИ, уже потраченные по
    фразе. Наряд без чисел — гипотеза, а гипотезы едут другим видом.

Модуль чистый: ни БД, ни сети, ни файлов, кроме чтения примера.
"""

import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Вид наряда. Различаются не «сложностью», а тем, чем подтверждена семантика:
# у consolidate — боевым расходом доноров, у остальных её только предстоит
# собрать, и билдер обязан пройти стадии семантики целиком.
KIND_CONSOLIDATE = "consolidate"   # вынести окупающиеся фразы в свою кампанию
KIND_REBUILD = "rebuild"           # пересобрать существующее направление
KIND_EXPAND = "expand"             # завести семантику, которой в кабинете нет
KINDS: Tuple[str, ...] = (KIND_CONSOLIDATE, KIND_REBUILD, KIND_EXPAND)

# Базы сравнения исхода. Своей истории у новой кампании нет вовсе, поэтому
# «до и после» здесь физически невозможно — сравнивать можно только с
# чем-то внешним: с заповедником, с направлением или С ДОНОРАМИ.
#
# vs_donors внесена не для полноты списка, а по дефекту, найденному 27.08:
# именно эту базу выставляет генератор выноса (ideas/consolidate — «побить
# донорскую цену конверсии, ту самую, по которой связки покупаются сейчас»),
# и наряд из его идеи валидатором не проходил. Тесты обеих сторон при этом
# были зелёными: они собирали наряд руками, с базой из этого же списка.
COMPARISONS: Tuple[str, ...] = ("did_vs_holdout", "vs_direction", "vs_donors")

# Предел Директа на имя кампании — 255 символов; берём с запасом на суффиксы
# заливки. Число не наше: см. direct/upload.py билдера.
NAME_LIMIT = 200

# Слаг становится ИМЕНЕМ ПАПКИ на диске билдера.
SLUG_RE = re.compile(r"^[a-z0-9_]{3,60}$")

EXAMPLE_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "docs", "examples",
    "build-order-consolidate.json")


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _number(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_phrase(phrase: Any) -> str:
    """Фраза так, как её видит Директ: нижний регистр, один пробел.

    Сравнивай агент иначе — валидатор отбивал бы наряды, которые в кабинете
    сработали бы верно, и наоборот пропускал бы минусовку, которая ничего не
    выключает.
    """
    return re.sub(r"\s+", " ", _text(phrase)).lower()


# --------------------------------------------------------------- проверка


def _check_queries(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = order.get("queries")
    if not isinstance(raw, list) or not raw:
        raise ValueError("наряд без фраз: кампания без единого ключа")

    queries: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("фраза наряда должна быть объектом с полями")
        phrase = normalize_phrase(item.get("phrase"))
        if not phrase:
            raise ValueError("наряд без фраз: пустая строка вместо фразы")
        donor = _text(item.get("donor_campaign_id"))
        if not donor:
            raise ValueError(
                f"фраза {phrase!r} без донора: кросс-минусовку по ней не "
                "построить, а факт «здесь уже потратили деньги» нечем "
                "подтвердить")
        cost = _number(item.get("cost_rub"))
        conversions = _number(item.get("conversions"))
        if cost is None or conversions is None:
            raise ValueError(
                f"фраза {phrase!r} без фактов: вердикт наряда — деньги, уже "
                "потраченные по ней, а гипотезы едут видом expand")
        queries.append({"phrase": phrase, "donor_campaign_id": donor,
                        "cost_rub": cost, "conversions": conversions})
    return queries


def _check_negatives(order: Dict[str, Any],
                     queries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    raw = order.get("donor_negatives")
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            "наряд без кросс-минусовки доноров: новая кампания и донор "
            "остались бы в одном аукционе одного рекламодателя")

    donors = {q["donor_campaign_id"] for q in queries}
    negatives: List[Dict[str, Any]] = []
    muted: set = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("кросс-минусовка должна быть объектом с полями")
        campaign_id = _text(item.get("campaign_id"))
        phrases = [normalize_phrase(p) for p in (item.get("phrases") or ())]
        phrases = [p for p in phrases if p]
        if not campaign_id or not phrases:
            raise ValueError(
                "кросс-минусовка без кампании или без фраз ничего не выключает")
        if campaign_id not in donors:
            raise ValueError(
                f"кросс-минусовка адресована кампании {campaign_id}, у которой "
                "наряд ничего не забирал: это выключило бы чужой рабочий "
                "трафик, и в откате такой строки нет")
        negatives.append({"campaign_id": campaign_id, "phrases": phrases})
        muted.update((campaign_id, p) for p in phrases)

    # Главная проверка: пара (донор, фраза), а не множества по отдельности.
    # Минусовка не там, где фраза работала, ничего не выключает.
    for query in queries:
        pair = (query["donor_campaign_id"], query["phrase"])
        if pair not in muted:
            raise ValueError(
                f"фраза {query['phrase']!r} вынесена из кампании "
                f"{query['donor_campaign_id']}, но не выключена в ней: без "
                "кросс-минусовки донор продолжит по ней торговаться")
    return negatives


def _check_success_rule(order: Dict[str, Any]) -> Dict[str, Any]:
    rule = order.get("success_rule")
    if not isinstance(rule, dict) or not rule:
        raise ValueError(
            "наряд без критерия успеха: кампанию нечем закрыть, и она "
            "останется в реестре навсегда")
    metric = _text(rule.get("metric"))
    threshold = _number(rule.get("threshold"))
    if threshold is None:
        threshold = _number(rule.get("value"))
    if not metric or threshold is None:
        raise ValueError("критерий успеха без метрики или порога непроверяем")
    comparison = _text(rule.get("comparison"))
    if comparison not in COMPARISONS:
        raise ValueError(
            "критерий успеха без базы сравнения: у новой кампании нет своей "
            f"истории, и «до и после» невозможно. Допустимо: {COMPARISONS}")
    return {"metric": metric, "threshold": threshold, "comparison": comparison}


def _check_campaign(order: Dict[str, Any]) -> Dict[str, Any]:
    campaign = order.get("campaign")
    if not isinstance(campaign, dict) or not campaign:
        raise ValueError("наряд без настроек кампании залить нечем")
    budget = _number(campaign.get("weekly_budget"))
    cpa = _number(campaign.get("target_cpa"))
    counter = _number(campaign.get("counter_id"))
    goal = _number(campaign.get("goal_id"))
    if not budget or budget <= 0:
        raise ValueError("недельный лимит кампании не задан")
    if not cpa or cpa <= 0:
        raise ValueError("целевая цена конверсии не задана")
    if not counter or not goal:
        raise ValueError(
            "кампания без счётчика и цели: стратегия AVERAGE_CPA невозможна, "
            "и кампания встала бы в кабинете на ручных ставках")
    return {"weekly_budget": int(budget), "target_cpa": int(cpa),
            "counter_id": int(counter), "goal_id": int(goal)}


def validate(order: Dict[str, Any]) -> Dict[str, Any]:
    """Наряд → нормализованный наряд. Негодный — ValueError с причиной.

    Возвращается КОПИЯ с приведёнными фразами: получатель обязан видеть ровно
    то, что проверил валидатор, иначе проверка была о другом тексте.
    """
    if not isinstance(order, dict):
        raise ValueError("наряд должен быть объектом")

    kind = _text(order.get("kind"))
    if kind not in KINDS:
        raise ValueError(f"вид наряда {kind!r} неизвестен: {KINDS}")

    order_id = _text(order.get("order_id"))
    if not order_id:
        raise ValueError("наряд без order_id: связь с кампанией держать нечем")
    account = _text(order.get("account"))
    if not account:
        raise ValueError("наряд без кабинета некуда заливать")
    slug = _text(order.get("level_slug"))
    if not SLUG_RE.match(slug):
        raise ValueError(
            f"слаг уровня {slug!r} негоден: он становится именем папки на "
            "диске билдера, и допустимы только [a-z0-9_]")

    # Имя кампании — ПОЛЕ наряда, а не вычисление получателя. Считай его обе
    # стороны своей формулой — формулы разъедутся на первой же правке, и
    # идемпотентность заливки (direct/upload.py ищет кампанию по имени)
    # молча заведёт вторую кампанию на тот же наряд.
    name = _text(order.get("campaign_name"))
    if not name:
        raise ValueError("наряд без имени кампании: заливать нечего")
    if order_id not in name:
        raise ValueError(
            f"имя кампании {name!r} не несёт order_id {order_id!r}: связь "
            "«наряд → кампания в кабинете» держалась бы только на памяти "
            "человека")
    if len(name) > NAME_LIMIT:
        raise ValueError(f"имя кампании длиннее {NAME_LIMIT} символов")

    horizon = _number(order.get("horizon_days"))
    if not horizon or horizon <= 0:
        raise ValueError(
            "наряд без горизонта: кампания живёт вечно и никогда не попадает "
            "в разбор")

    # Окно, за которое собраны факты фраз. Без него cost_rub — не факт, а
    # число: 18 400 ₽ за неделю и за квартал требуют разных лимитов новой
    # кампании и разной цены кросс-минусовки. Пропуск этого поля в задаче 17
    # обнаружился на первой же попытке собрать по наряду действия.
    window = _number(order.get("window_days"))
    if not window or window <= 0:
        raise ValueError(
            "наряд без окна наблюдения: расход по фразам не приведёшь ко дню, "
            "и лимит новой кампании считать не из чего")

    queries = _check_queries(order)
    negatives = _check_negatives(order, queries)
    rule = _check_success_rule(order)
    campaign = _check_campaign(order)

    return {
        "order_id": order_id,
        "idea_id": _text(order.get("idea_id")) or None,
        "kind": kind,
        "account": account,
        "level_slug": slug,
        "campaign_name": name,
        "direction": _text(order.get("direction")),
        "queries": queries,
        "donor_negatives": negatives,
        "campaign": campaign,
        "window_days": int(window),
        "horizon_days": int(horizon),
        "success_rule": rule,
    }


# ------------------------------------------------------------ имя кампании


def campaign_name(order: Dict[str, Any]) -> str:
    """Имя кампании в Директе. order_id — В ХВОСТЕ и всегда целиком.

    Связь «наряд → кампания в кабинете» иначе держится только на памяти
    человека: drift.py сверяет кабинет с журналом по объектам, а у новой
    кампании Id появляется лишь после создания. Директ режет имя по длине,
    поэтому обрезается человеческая половина, а не идентификатор.
    """
    order_id = _text(order.get("order_id"))
    human = " / ".join(part for part in (
        _text(order.get("direction")), _text(order.get("kind"))) if part)
    tail = f" / {order_id}" if human else order_id
    room = NAME_LIMIT - len(tail)
    if room < 0:
        return order_id[:NAME_LIMIT]
    return f"{human[:room].rstrip()}{tail}" if human else order_id


def make_order_id(kind: str, direction: str) -> str:
    """Идентификатор наряда: вид и направление. БЕЗ ДАТЫ.

    Идемпотентность заливки держится на имени кампании (direct/upload.py ищет
    её по нему), а имя несёт order_id. Дата в нём означала бы новую кампанию
    каждым прогоном генератора: нагрузка идеи — поле обновляемое
    (registry.GENERATOR_FIELDS), и наряд пересобирается ежедневно, пока идея
    открыта. Старая кампания при этом продолжала бы тратить.

    Что теряется без даты: две разные консолидации одного направления в одном
    кабинете. Их и не бывает — у реестра ровно одна открытая идея на
    (кабинет, вид, направление), а повторная находка полгода спустя есть та же
    консолидация с обновлённым составом фраз. Билдер догрузит их в ту же
    кампанию (--resume), вместо того чтобы плодить помесячные близнецы,
    делящие между собой один аукцион.
    """
    parts = [_text(kind), _text(direction)]
    return "-".join(p for p in parts if p)


# --------------------------------------------------------- наряд из идеи


def from_idea(idea: Dict[str, Any], *, campaign: Dict[str, Any],
              account: Optional[str] = None) -> Dict[str, Any]:
    """Идея реестра → наряд. Кросс-минусовка ВЫВОДИТСЯ, а не выписывается.

    Донор каждой фразы уже назван в идее (генератор consolidate берёт их из
    отчёта запросов), поэтому забыть его здесь невозможно. Ручной список
    доноров означал бы, что самая дорогая ошибка наряда — та, которую человек
    делает молча.
    """
    subject = idea.get("subject") or {}
    direction = _text(subject.get("direction")) or _text(idea.get("direction"))
    queries = list((idea.get("detail") or {}).get("queries") or ())

    by_donor: Dict[str, List[str]] = {}
    for query in queries:
        donor = _text((query or {}).get("donor_campaign_id"))
        phrase = normalize_phrase((query or {}).get("phrase"))
        if donor and phrase and phrase not in by_donor.setdefault(donor, []):
            by_donor[donor].append(phrase)

    rule = dict(idea.get("success_rule") or {})
    if "threshold" not in rule and "value" in rule:
        rule["threshold"] = rule["value"]

    kind = _text(subject.get("kind")) or KIND_CONSOLIDATE
    order_id = make_order_id(kind, direction)
    return {
        "order_id": order_id,
        "idea_id": _text(idea.get("idea_id")) or None,
        "kind": kind,
        "account": account or _text(idea.get("account")),
        "level_slug": _slug_for(kind, direction),
        "campaign_name": campaign_name({"order_id": order_id, "kind": kind,
                                        "direction": direction}),
        "direction": direction,
        "queries": queries,
        "donor_negatives": [{"campaign_id": donor, "phrases": phrases}
                            for donor, phrases in sorted(by_donor.items())],
        "campaign": dict(campaign or {}),
        "window_days": (idea.get("detail") or {}).get("window_days"),
        "horizon_days": idea.get("horizon_days"),
        "success_rule": rule,
    }


def _slug_for(kind: str, direction: str) -> str:
    """Слаг папки уровня: только то, что переживёт файловую систему.

    Без даты — по той же причине, что и order_id: слаг становится именем
    папки на диске билдера, и плавающий месяц заводил бы новый уровень
    каждым прогоном, оставляя предыдущие мёртвыми.
    """
    raw = "_".join(p for p in (_text(direction), _text(kind)) if p)
    slug = re.sub(r"[^a-z0-9_]+", "_", raw.lower()).strip("_")
    return slug[:60]

