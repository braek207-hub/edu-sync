# -*- coding: utf-8 -*-
"""
sync/agent/ideas/market.py — генератор идей: спрос и рыночные поводы.

Четвёртый из пяти генераторов Ф13. Два источника поводов, и они РАЗНОЙ силы,
поэтому живут в одном модуле, но не смешиваются ни на входе, ни в классе:

  1. **Спрос Wordstat** — замер. Направление, у которого спрос на подъёме
     (demand.demand_regime), а часть фраз кабинет не покрывает вовсе.
  2. **Смысловая гипотеза модели** — текст. Поверх паспорта продукта модель
     предлагает, что попробовать.

**Ряд есть не у всех.** Шесть направлений кабинета фраз спроса не имеют
вовсе (demand.DIRECTIONS_WITHOUT_SERIES), и вердикт у них свой — «нет ряда», а
не «мало данных». Разница не косметическая: первое лечится добавлением фраз в
семантику спроса, второе — временем. Идея, выведенная из отсутствующего ряда,
это выдумка с видом вывода, и генератор её не заводит; отказ при этом ВИДЕН
строкой с причиной, иначе дыра в семантике спроса спрячется навсегда.

**Гипотеза модели — всегда класс 3.** Внешних источников о конкурентах и
кейсах у нас нет ни одного (docs/AGENT-DATA-SOURCES.md). Модель, которой
поручено «придумать идею с рынка», выдаёт правдоподобный текст — и класс 2
дал бы этому тексту право тратить деньги кабинета. Ограничение не в
формулировке промпта, а здесь, в коде: промпт правится за минуту.

**Ключ гипотезы, а не её текст.** Формулировка модели плавает от прогона к
прогону — тот же смысл другими словами. Войди она в адрес идеи, и idea_id
менялся бы каждый день: пустая история, снятый отказ человека, та же по сути
гипотеза под новым идентификатором. Поэтому гипотеза обязана прийти с
устойчивым ключом, а без него отвергается.

**Почему идеи спроса тоже класс 3.** Рычага у «завести новую семантику» нет:
это наряд билдеру (Ф14, задача 17), а `campaign.create` в allow-листе записи
отсутствует. Класс 2 при этом ТРЕБУЕТ нагрузки рычага (registry._check_action
отвергает применимую идею с пустой нагрузкой), и выдать её сейчас можно было
бы только выдумав контракт наряда до того, как он написан. Черновик плана
ставил здесь класс 2; правда сильнее черновика, а чего идее не хватает до
ставки — сказано в detail.needs, и это единственная правка, которую придётся
сделать, когда наряд появится.

**Смета не выдумывается.** Объём нового спроса в лидах нам неизвестен: у
фраз, которых в кабинете нет, нет и истории. Смета «на глаз» вынесла бы идею
вперёд посчитанных — непосчитанная цена уводит идею в хвост очереди
(registry.rank), и это честнее, чем правдоподобное число.

**Что модуль не делает.** Не ходит в базу, не считает режим спроса (это
demand.py) и не вызывает модель: гипотезы приходят строками, как и поводы
спроса (сборку описывает задача 16а). И не молчит об отбракованных: scan()
возвращает их списком с причиной.
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple

from sync.agent import demand as demand_mod
from sync.agent.experiments import HORIZON_DAYS, METRIC
from sync.agent.writer import lanes as lanes_mod, tier as tier_mod
from sync.agent.writer.learning import LEARNING_COOLDOWN_DAYS

# Имя источника в реестре. Входит в idea_id, поэтому меняться не может: смена
# завела бы все идеи генератора заново, с пустой историей и снятым отказом.
SOURCE = "market"

KIND_DEMAND = "demand"
KIND_HYPOTHESIS = "hypothesis"

# Срок, за который повод обязан отдать вердикт. Новая семантика едет новой
# кампанией или группой, а свежая стратегия учится заново — значит к горизонту
# замера ставки прибавляется срок переобучения. Оба числа не свои:
# experiments.HORIZON_DAYS и writer/learning.LEARNING_COOLDOWN_DAYS.
HORIZON_WITH_LEARNING = HORIZON_DAYS + LEARNING_COOLDOWN_DAYS

# Режимы спроса, которые вообще бывают поводом ЗАВЕСТИ семантику. Спад — повод
# пересмотреть ожидания от кампании (demand.py), а не заходить в новое; норма
# не повод вовсе.
EXPANSION_REGIMES = (demand_mod.REGIME_RISE,)

# Чего не хватает предложению, чтобы стать проверяемым. Пишется В ИДЕЮ, а не
# держится в голове: предложение без этого — тупик, человек видит идею и не
# знает, чем ей помочь.
NEEDS_BUILDER_ORDER = (
    "наряда билдеру (Ф14, задача 17): рычага «завести новую семантику» у "
    "агента нет, и до наряда это предложение человеку")
NEEDS_EXTERNAL_SOURCE = (
    "внешнего источника о конкурентах и кейсах — у нас нет ни одного, и "
    "проверить смысловую гипотезу сегодня можно только запуском")

REASON_NO_ADDRESS = "у повода нет кабинета или направления"
REASON_UNKNOWN_KIND = (
    "вид повода неизвестен: генератор не угадывает, чем ему считать строку")
REASON_NO_SERIES = (
    "у направления нет ряда спроса вовсе: это лечится фразами в "
    "sync/edu_demand.py, а не решением агента — идея из пустого ряда была бы "
    "выдумкой с видом вывода")
REASON_LOW_DATA = (
    "недель базы не хватило на вердикт о режиме спроса: «мало данных» — это "
    "отсутствие вердикта, а не вердикт «норма», и лечится оно временем")
REASON_NOT_RISING = (
    "спрос не на подъёме: спад меняет ожидания от кампании, а не зовёт "
    "заводить новую семантику, норма не зовёт тем более")
REASON_COVERED = (
    "растущий спрос кабинет уже покрывает: это повод перелить бюджет "
    "(portfolio.py), а не заводить семантику заново")
REASON_NO_PHRASES = (
    "«не покрываем» без единой названной фразы — утверждение без предмета: "
    "непонятно, что именно заводить")
REASON_NO_DIRECTION_PRICE = (
    "у направления не посчитана цена эффективного лида: критерий успеха не от "
    "чего отмерить, а придуманный порог закрыл бы идею по мерке, которой "
    "никто не назначал")
REASON_NO_KEY = (
    "у гипотезы нет устойчивого ключа: текст модели плавает от прогона к "
    "прогону, и идея заводилась бы заново каждый день — с пустой историей и "
    "снятым отказом человека")
REASON_NO_CRITERION = (
    "у гипотезы нет машинно проверяемого критерия: её нельзя ни закрыть, ни "
    "засчитать, и она осталась бы в реестре навсегда")


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None   # NaN — то же «неизвестно»


def _text(value: Any) -> str:
    return str(value or "").strip()


def _skip(row: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Отбракованный повод с причиной.

    Причина обязательна: повод, исчезнувший молча, неотличим от повода,
    которого не было, — и первый же вопрос «почему генератор ничего не
    предложил» превращается в археологию по коду.
    """
    return {
        "kind": _text(row.get("kind")),
        "direction": _text(row.get("direction")),
        "key": _text(row.get("key")),
        "reason": reason,
    }


def _phrases(row: Dict[str, Any]) -> List[str]:
    raw = row.get("uncovered_phrases") or ()
    return [_text(p) for p in raw if _text(p)]


def _criterion(raw: Any) -> Optional[Dict[str, Any]]:
    """Критерий гипотезы: тот же машинный минимум, что требует реестр.

    Проверяется ЗДЕСЬ, а не только в registry._prepare: реестр принимает
    порцию целиком или никак, и одна гипотеза-мнение уронила бы вместе с
    собой все находки такта. Отказ на входе стоит одной строки в отчёте.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    metric = _text(raw.get("metric"))
    value = _number(raw.get("value"))
    op = _text(raw.get("op"))
    if not metric or value is None or op not in ("<=", ">=", "<", ">"):
        return None
    return {"metric": metric, "op": op, "value": value}


def _demand_idea(row: Dict[str, Any], account: str,
                 ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Повод спроса → (идея, отбраковка)."""
    direction = _text(row.get("direction"))
    regime = _text(row.get("regime"))

    if regime == demand_mod.REGIME_NO_SERIES:
        return None, _skip(row, REASON_NO_SERIES)
    if regime == demand_mod.REGIME_LOW_DATA:
        return None, _skip(row, REASON_LOW_DATA)
    if regime not in EXPANSION_REGIMES:
        return None, _skip(row, REASON_NOT_RISING)
    if row.get("covered"):
        return None, _skip(row, REASON_COVERED)

    phrases = _phrases(row)
    if not phrases:
        return None, _skip(row, REASON_NO_PHRASES)

    direction_cpl = _number(row.get("direction_cpl_rub"))
    if direction_cpl is None or direction_cpl <= 0:
        return None, _skip(row, REASON_NO_DIRECTION_PRICE)

    return {
        "source": SOURCE,
        "account": account,
        # Адрес повода — вид и направление. Фразы, сигма и частота плавают
        # каждую неделю, и войди они сюда, idea_id менялся бы вместе с ними.
        "subject": {"kind": KIND_DEMAND, "direction": direction},
        "tier": tier_mod.TIER_PROPOSAL,
        "lane": lanes_mod.LANE_PROPOSAL,
        "expected_rub": None,
        # Смета не выдумывается — см. шапку модуля.
        "test_cost_rub": None,
        "horizon_days": HORIZON_WITH_LEARNING,
        "success_rule": {
            "metric": METRIC,
            "op": "<=",
            # Побить цену, по которой направление покупает лиды СЕЙЧАС.
            "value": round(direction_cpl, 2),
            "comparison": "vs_direction",
        },
        "detail": {
            "needs": NEEDS_BUILDER_ORDER,
            "uncovered_phrases": phrases,
            "regime": regime,
            "sigma": _number(row.get("sigma")),
            "frequency": _number(row.get("frequency")),
            "baseline_median": _number(row.get("baseline_median")),
            "last_week": _text(row.get("last_week")) or None,
            "direction_cpl_rub": round(direction_cpl, 2),
        },
    }, None


def _hypothesis_idea(row: Dict[str, Any], account: str,
                     ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Смысловая гипотеза модели → (идея класса 3, отбраковка)."""
    key = _text(row.get("key"))
    if not key:
        return None, _skip(row, REASON_NO_KEY)

    rule = _criterion(row.get("success_rule"))
    if rule is None:
        return None, _skip(row, REASON_NO_CRITERION)

    horizon = _number(row.get("horizon_days"))
    return {
        "source": SOURCE,
        "account": account,
        # Адрес гипотезы — её устойчивый ключ, а не текст: текст плавает.
        "subject": {"kind": KIND_HYPOTHESIS, "key": key},
        "tier": tier_mod.TIER_PROPOSAL,
        "lane": lanes_mod.LANE_PROPOSAL,
        "expected_rub": None,
        "test_cost_rub": None,
        "horizon_days": (int(horizon) if horizon and horizon > 0
                         else HORIZON_WITH_LEARNING),
        "success_rule": rule,
        "detail": {
            # Дефицит гипотезы: свой, если модель его назвала, общий — если
            # промолчала. Молчание модели не должно превращать предложение в
            # тупик: общий дефицит известен и без неё.
            "needs": _text(row.get("needs")) or NEEDS_EXTERNAL_SOURCE,
            "statement": _text(row.get("statement")) or None,
            "direction": _text(row.get("direction")) or None,
        },
    }, None


def scan(rows: Sequence[Dict[str, Any]],
         ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Поводы рынка → {"ideas": [...], "skipped": [...]}.

    Отбракованные возвращаются рядом с принятыми и с причиной: «поводов не
    нашлось» и «поводы были, но у направления нет ряда» ведут к разным
    следующим шагам — второе чинится семантикой спроса, первое ничем.
    """
    ctx = ctx or {}
    account_default = _text(ctx.get("account"))
    ideas: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for row in rows or ():
        if not isinstance(row, dict):
            continue
        account = _text(row.get("account")) or account_default
        kind = _text(row.get("kind"))
        if not account or (kind == KIND_DEMAND and not _text(row.get("direction"))):
            skipped.append(_skip(row, REASON_NO_ADDRESS))
            continue

        if kind == KIND_DEMAND:
            idea, refusal = _demand_idea(row, account)
        elif kind == KIND_HYPOTHESIS:
            idea, refusal = _hypothesis_idea(row, account)
        else:
            idea, refusal = None, _skip(row, REASON_UNKNOWN_KIND)

        if idea is not None:
            ideas.append(idea)
        else:
            skipped.append(refusal)

    # Порядок детерминирован: на одних и тех же данных человек обязан видеть
    # один и тот же экран. Поводы спроса вперёд гипотез — за ними замер.
    ideas.sort(key=lambda i: (0 if i["subject"]["kind"] == KIND_DEMAND else 1,
                              _text(i["subject"].get("direction")),
                              _text(i["subject"].get("key"))))
    return {"ideas": ideas, "skipped": skipped}


def candidates(rows: Sequence[Dict[str, Any]],
               ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Только идеи — форма вызова для реестра (registry.upsert)."""
    return scan(rows, ctx)["ideas"]
