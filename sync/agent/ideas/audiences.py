# -*- coding: utf-8 -*-
"""
sync/agent/ideas/audiences.py — генератор идей: аудитории и срезы.

Пятый и последний генератор Ф13. Ищет срез, который конвертит лучше базы, а
объёма забирает мало: такой сегмент — повод расширить охват, а не поправить
ставку.

**Знаменатель обязателен.** Атрибуция ретаргетинга ЗАМЕРЕНА и завышает вклад:
счёт «по визиту» даёт ретаргетингу впятеро больше, чем счёт по людям (разбор
разделов LIME, память lime-sections-overview-audit). Идея, не сказавшая, на
что делила конверсию, сравнивает несравнимое — и выглядит при этом
посчитанной. Поэтому знаменатель входит В АДРЕС идеи: тот же сегмент,
посчитанный по людям и по визитам, — разное утверждение, и один адрес на оба
означал бы, что вторая находка молча затирает первую.

**Чужая территория движка.** Корректировки по устройству, полу, возрасту и
гео считаются КАЖДЫМ тактом (computed.compute_segment_modifiers), площадки
режет Э3.7 (writer/placements.py). Идея поверх работающего рычага — это шум в
очереди человека и второй хозяин у одной ручки. Список чужого выведен из
карты движка (segments.SEGMENT_FIELDS), а не переписан рядом: добавят срез в
карту — генератор узнает об этом сам.

**Шум отсекается двумя порогами, и оба чужие.** Событий должно быть не меньше
ladder.MIN_STEP_EVENTS — той ступени, на которой лестница воронки вообще
выносит суждение; превышение базы — не меньше portfolio.GROWTH_LAMBDA_MARGIN,
запаса, которым портфель отделяет доказанное от «ровно на пороге». Ни одного
своего числа: разъехаться копиям негде.

**Почему класс 3.** Рычаг расширения аудитории — audience.add (Ф15, задача
23), и в allow-листе записи его нет. Класс 2 при этом ТРЕБУЕТ нагрузки рычага
(registry._check_action отвергает применимую идею с пустой нагрузкой), и выдать
её сейчас значило бы обещать отправку, которой не будет. Чего идее не хватает
до ставки, сказано в detail.needs.

**Что модуль не делает.** Не ходит в базу и не считает срезы: на вход подают
уже собранные строки (сборку описывает задача 16а). Не решает, применять ли
идею. И не молчит об отбракованных: scan() возвращает их списком с причиной.
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple

from sync.agent.experiments import HORIZON_DAYS, METRIC
from sync.agent.ladder import MIN_STEP_EVENTS
from sync.agent.portfolio import GROWTH_LAMBDA_MARGIN
from sync.agent.segments import SEGMENT_FIELDS
from sync.agent.writer import lanes as lanes_mod, tier as tier_mod
from sync.agent.writer.placements import PLACEMENT_KIND

# Имя источника в реестре. Входит в idea_id, поэтому меняться не может: смена
# завела бы все идеи генератора заново, с пустой историей и снятым отказом.
SOURCE = "audience"

KIND_AUDIENCE = "audience"

# Знаменатели, которые мы умеем отличать друг от друга. Разница между ними
# замерена и велика (×5 у ретаргетинга), поэтому третьего значения «неважно»
# здесь нет и быть не может.
DENOMINATORS = ("users", "visits")

# Срезы, которыми движок распоряжается сам. Выводится из карты сегментного
# отчёта, а не переписывается: копия разъехалась бы с движком молча, и первым
# симптомом стал бы второй хозяин у одной ручки.
ENGINE_OWNED_KINDS = frozenset(SEGMENT_FIELDS) | {"placement"}

# Доля кликов кампании, выше которой сегмент считается исчерпанным: расширять
# нечего, идея была бы предложением «таргетируйте то, что и так везде».
# Ручка, а не константа: где проходит «мало» — вопрос кабинета и сезона, а не
# арифметики. Четверть кампании — верх того, что в любом прочтении ещё можно
# назвать малым объёмом.
MAX_SHARE_KEY = "audience_max_share"
DEFAULT_MAX_SHARE = 0.25

NEEDS_AUDIENCE_LEVER = (
    "рычага audience.add (Ф15, задача 23): расширить охват аудитории агенту "
    "сегодня нечем, и до рычага это предложение человеку")

REASON_NO_ADDRESS = "у среза нет кабинета, кампании или устойчивого ключа"
REASON_ENGINE_OWNED = (
    "срезом распоряжается сам движок (корректировки сегментов считаются каждым "
    "тактом): идея поверх работающего рычага — второй хозяин у одной ручки")
REASON_PLACEMENTS = (
    "площадки режет рычаг Э3.7 (writer/placements.py) — повторять его идеей "
    "значит заводить второго хозяина у той же ручки")
REASON_UNKNOWN_KIND = (
    "вид среза неизвестен: генератор не угадывает, чем ему считать строку")
REASON_NO_DENOMINATOR = (
    "у среза не назван знаменатель: счёт «по визиту» даёт ретаргетингу впятеро "
    "больше, чем счёт по людям (замер), и идея без знаменателя сравнивает "
    "несравнимое")
REASON_NO_VOLUME = "у среза не посчитаны клики или конверсии"
REASON_THIN_EVENTS = (
    f"событий меньше {MIN_STEP_EVENTS}: на таком объёме «конвертит лучше» "
    "держится ровно до следующей недели")
REASON_NO_BASE = "у среза нет базовой конверсии: превышение не с чем сравнить"
REASON_THIN_LIFT = (
    f"превышение базы без запаса ×{GROWTH_LAMBDA_MARGIN}: разница в пределах "
    "шума расширения не окупит")
REASON_EXHAUSTED = (
    "сегмент уже забрал большую часть кликов кампании: расширять его некуда")
REASON_NO_PRICE = (
    "у кампании не посчитана цена эффективного лида: критерий успеха не от "
    "чего отмерить, а придуманный порог закрыл бы идею по мерке, которой "
    "никто не назначал")


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None   # NaN — то же «неизвестно»


def _text(value: Any) -> str:
    return str(value or "").strip()


def _skip(row: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Отбракованный срез с причиной.

    Причина обязательна: срез, исчезнувший молча, неотличим от среза,
    которого не было, — и первый же вопрос «почему генератор ничего не
    предложил» превращается в археологию по коду.
    """
    return {
        "kind": _text(row.get("kind")),
        "segment_key": _text(row.get("segment_key")),
        "campaign_id": _text(row.get("campaign_id")),
        "reason": reason,
    }


def _max_share(ctx: Dict[str, Any]) -> float:
    config = (ctx or {}).get("config") or {}
    value = _number(config.get(MAX_SHARE_KEY))
    if value is None or value <= 0:
        return DEFAULT_MAX_SHARE
    return value


def _one(row: Dict[str, Any], ctx: Dict[str, Any], account: str,
         ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Срез → (идея расширения, отбраковка)."""
    kind = _text(row.get("kind"))
    if kind == "placement" or kind == PLACEMENT_KIND:
        return None, _skip(row, REASON_PLACEMENTS)
    if kind in ENGINE_OWNED_KINDS:
        return None, _skip(row, REASON_ENGINE_OWNED)
    if kind != KIND_AUDIENCE:
        return None, _skip(row, REASON_UNKNOWN_KIND)

    campaign_id = _text(row.get("campaign_id"))
    segment_key = _text(row.get("segment_key"))
    if not campaign_id or not segment_key:
        return None, _skip(row, REASON_NO_ADDRESS)

    denominator = _text(row.get("denominator"))
    if denominator not in DENOMINATORS:
        return None, _skip(row, REASON_NO_DENOMINATOR)

    clicks = _number(row.get("clicks"))
    conversions = _number(row.get("conversions"))
    if not clicks or clicks <= 0 or conversions is None or conversions < 0:
        return None, _skip(row, REASON_NO_VOLUME)
    if conversions < MIN_STEP_EVENTS:
        return None, _skip(row, REASON_THIN_EVENTS)

    base_cr = _number(row.get("base_cr"))
    if base_cr is None or base_cr <= 0:
        return None, _skip(row, REASON_NO_BASE)
    cr = conversions / clicks
    if cr < base_cr * GROWTH_LAMBDA_MARGIN:
        return None, _skip(row, REASON_THIN_LIFT)

    campaign_clicks = _number(row.get("campaign_clicks"))
    share = (clicks / campaign_clicks) if campaign_clicks else None
    if share is not None and share > _max_share(ctx):
        return None, _skip(row, REASON_EXHAUSTED)

    base_cpl = _number(row.get("base_cpl_rub"))
    if base_cpl is None or base_cpl <= 0:
        return None, _skip(row, REASON_NO_PRICE)

    cost = _number(row.get("cost_rub"))
    window = _number(row.get("window_days"))
    # Смета — расход сегмента за горизонт по нынешнему темпу. Непосчитанная
    # остаётся пустой: ноль вынес бы идею вперёд посчитанных, потому что
    # очередь реестра сравнивает ценность НА РУБЛЬ проверки.
    test_cost = (round(cost / window * HORIZON_DAYS, 2)
                 if cost and window and window > 0 else None)

    return {
        "source": SOURCE,
        "account": account,
        # Адрес — кампания, ключ сегмента и знаменатель. Числа сюда не входят:
        # они пересчитываются каждым прогоном, и войди они в отпечаток, идея
        # заводилась бы заново каждый день — с пустой историей и снятым
        # отказом человека.
        "subject": {"kind": KIND_AUDIENCE, "campaign_id": campaign_id,
                    "segment_key": segment_key, "denominator": denominator},
        "tier": tier_mod.TIER_PROPOSAL,
        "lane": lanes_mod.LANE_PROPOSAL,
        # Ожидаемая выгода не считается: сколько трафика даст расширение
        # аудитории, из её нынешнего объёма не следует — потолок аудитории мы
        # не знаем. Правдоподобное число здесь вынесло бы идею вперёд
        # посчитанных (registry.rank), и незнание оказалось бы аргументом.
        "expected_rub": None,
        "test_cost_rub": test_cost,
        # Аудитория обучение стратегии не сбрасывает (writer/lanes.py: та же
        # корректировка сегмента), значит недель переобучения к сроку не
        # прибавляется — горизонт ставки и есть срок.
        "horizon_days": HORIZON_DAYS,
        "success_rule": {
            "metric": METRIC,
            "op": "<=",
            # Побить цену, по которой кампания покупает лиды СЕЙЧАС.
            "value": round(base_cpl, 2),
            "comparison": "vs_campaign",
        },
        "detail": {
            "needs": NEEDS_AUDIENCE_LEVER,
            "segment_name": _text(row.get("segment_name")) or None,
            "denominator": denominator,
            "cr": round(cr, 6),
            "base_cr": base_cr,
            "lift": round(cr / base_cr, 3),
            "clicks": clicks,
            "conversions": conversions,
            "share_of_clicks": (round(share, 6) if share is not None else None),
            "cost_rub": cost,
            "window_days": (int(window) if window else None),
            "base_cpl_rub": round(base_cpl, 2),
        },
    }, None


def scan(rows: Sequence[Dict[str, Any]],
         ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Срезы кабинета → {"ideas": [...], "skipped": [...]}.

    Отбракованные возвращаются рядом с принятыми и с причиной: «поводов не
    нашлось» и «поводы были, но все на шуме» ведут к разным следующим шагам.
    """
    ctx = ctx or {}
    account_default = _text(ctx.get("account"))
    ideas: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for row in rows or ():
        if not isinstance(row, dict):
            continue
        account = _text(row.get("account")) or account_default
        if not account:
            skipped.append(_skip(row, REASON_NO_ADDRESS))
            continue
        idea, refusal = _one(row, ctx, account)
        if idea is not None:
            ideas.append(idea)
        else:
            skipped.append(refusal)

    # Порядок детерминирован: на одних и тех же данных человек обязан видеть
    # один и тот же экран.
    ideas.sort(key=lambda i: (i["subject"]["campaign_id"],
                              i["subject"]["segment_key"],
                              i["subject"]["denominator"]))
    return {"ideas": ideas, "skipped": skipped}


def candidates(rows: Sequence[Dict[str, Any]],
               ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Только идеи — форма вызова для реестра (registry.upsert)."""
    return scan(rows, ctx)["ideas"]
