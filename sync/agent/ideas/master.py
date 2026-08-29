# -*- coding: utf-8 -*-
"""
sync/agent/ideas/master.py — генератор идей: Мастер кампаний.

Шестой генератор. Единственный, чей повод — не находка внутри кампании, а сама
кампания: деньги, которыми агент не управляет и управлять не сможет.

**Повод.** Мастер кампаний не отдаётся методом `campaigns.get`, значит не
попадает в витрину настроек, значит выпадает из всего, что агент считает по
витрине — недобора трафика, корректировок, кривых насыщения, портфельной
раскладки. Замер 29.08.2026 на проде: 2 390 391 ₽ из 24 829 222 ₽ за окно
решений, 9,63 % расхода, три кампании Мастера в двух кабинетах. Это не
маленькая дыра: доля растёт (за два месяца расход тех же трёх кампаний вырос с
1 098 164 ₽ до 2 437 394 ₽), и растёт она сама, без участия агента.

**Почему класс 3 и полоса 7.** Рычага записи в Мастер кампаний нет ни у API
Директа, ни у агента, и появиться ему неоткуда — это не «пока не сделали», а
свойство продукта. Класс 2 при этом ТРЕБУЕТ нагрузки рычага
(`registry._check_action` отвергает применимую идею с пустой нагрузкой), и
выдать её значило бы обещать отправку, которой не будет. Поэтому предложение:
текст человеку, ничего не применяющий ни при какой ступени автономии.

**Что кампания в идее не «слепая», а посчитанная.** Статистика Мастера
доступна целиком: Reports API отдаёт по ней и расход, и конверсии, и поисковые
запросы (замер 29.08.2026 — от 5 010 до 5 042 строки на кампанию в
`edu_agent_search_queries`). Поэтому идея несёт не «разберитесь с этой
кампанией», а числа: сколько она стоит, по какой цене покупает эффективный
лид и на сколько эта цена отличается от цены кабинета.

**Чего генератор НЕ делает — и это главная его проверка.** Он молчит про
кампанию, которую API Директа ОТДАЁТ. Такая кампания не Мастер, а недоезд до
витрины: 25.08.2026 всю слепую зону списали на Мастер кампаний, а замер по
явным Id показал 12 обычных `TEXT_CAMPAIGN` из 15 — дефект синка на форме
массивов. Дефект кода чинится кодом; рекомендация человеку «посмотрите на эту
кампанию» увела бы разбор в ручной труд и оставила бы баг жить. Разделение
приходит готовым из `sync/agent/master.py` (поле `api`), а не выводится здесь
по «МК» в имени: переименование кампании молча меняло бы вывод о поломке.

**Порог — доля кабинета, а не рубли.** 50 000 ₽ значат разное при недельном
расходе 500 000 ₽ и 5 700 000 ₽, и абсолютный порог менял бы смысл вместе с
размером кабинета — та же болезнь, от которой полосы отказались от общего
лимита в рублях (writer/lanes.py). Ниже процента расхода кабинета рекомендация
человеку стоит дороже денег, о которых она.

**Что модуль не делает.** Не ходит ни в базу, ни в API: карточки собирает
`sync/agent/master.py`. Не решает, применять ли идею — применять её нечем. И
не молчит об отбракованных: `scan()` возвращает их списком с причиной, потому
что «Мастера в кабинете нет» и «Мастер есть, но мелкий» ведут к разным
следующим шагам.
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple

from sync.agent.experiments import METRIC
from sync.agent.master import MASTER_KIND
from sync.agent.writer import lanes as lanes_mod, tier as tier_mod

# Срок замера с переобучением. Не вторая копия суммы, а тот же объект: правку
# настроек Мастера человек делает руками, стратегия после неё учится заново, и
# горизонт здесь ровно тот же, что у поводов рынка (ideas/market.py) — оба
# сложены из experiments.HORIZON_DAYS и writer/learning.LEARNING_COOLDOWN_DAYS.
from sync.agent.ideas.market import HORIZON_WITH_LEARNING

# Имя источника в реестре. Входит в idea_id, поэтому меняться не может: смена
# завела бы все идеи генератора заново, с пустой историей и снятым отказом
# человека.
SOURCE = "master_campaign"

# Доля расхода кабинета, ниже которой Мастер не стоит внимания человека.
# Ручка, а не константа: где проходит «мелко» — вопрос кабинета, а не
# арифметики. Процент выбран как порог, ниже которого кампания не меняет ни
# одного вывода портфельной раскладки.
MIN_SHARE_KEY = "master_min_share"
DEFAULT_MIN_SHARE = 0.01

NEEDS_HUMAN_HANDS = (
    "рук человека: записи в Мастер кампаний нет ни у API Директа, ни у "
    "агента, и появиться ей неоткуда — правку делают в интерфейсе кабинета")

REASON_NO_ADDRESS = (
    "у карточки нет кабинета или кампании: идея без адреса уедет в чужую "
    "очередь и посчитается по чужому порогу")
REASON_API_VISIBLE = (
    "API Директа эту кампанию отдаёт: она не Мастер кампаний, а недоезд до "
    "витрины настроек — дефект синка, который чинится кодом. Замер 25.08.2026: "
    "12 из 15 «слепых» кампаний оказались обычными TEXT_CAMPAIGN, упавшими на "
    "форме массивов")
REASON_NO_SPEND = (
    "кампания за окно ничего не потратила: рекомендовать по ней нечего, а "
    "строка в очереди человека стоит его внимания")
REASON_SMALL = (
    "расход кампании меньше порога доли кабинета: разбор такой кампании "
    "человеком стоит дороже денег, о которых он")
REASON_NO_PRICE = (
    "у кабинета не посчитана цена эффективного лида: критерий успеха не от "
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
    """Отбракованная карточка с причиной.

    Причина обязательна: кампания, исчезнувшая молча, неотличима от кампании,
    которой не было, — и первый же вопрос «почему про Мастер ничего не
    предложили» превращается в археологию по коду.
    """
    return {
        "campaign_id": _text(row.get("campaign_id")),
        "campaign_name": _text(row.get("campaign_name")),
        "cost_rub": _number(row.get("cost_rub")),
        "reason": reason,
    }


def _min_share(ctx: Dict[str, Any]) -> float:
    config = (ctx or {}).get("config") or {}
    value = _number(config.get(MIN_SHARE_KEY))
    if value is None or value < 0:
        return DEFAULT_MIN_SHARE
    return value


def _one(row: Dict[str, Any], ctx: Dict[str, Any], account: str,
         ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Карточка кампании вне витрины → (предложение человеку, отбраковка)."""
    campaign_id = _text(row.get("campaign_id"))
    if not campaign_id:
        return None, _skip(row, REASON_NO_ADDRESS)

    if row.get("api"):
        return None, _skip(row, REASON_API_VISIBLE)

    cost = _number(row.get("cost_rub"))
    if cost is None or cost <= 0:
        return None, _skip(row, REASON_NO_SPEND)

    share = _number(row.get("share_of_account"))
    if share is None or share < _min_share(ctx):
        return None, _skip(row, REASON_SMALL)

    base_cpl = _number(row.get("base_cpl_rub"))
    if base_cpl is None or base_cpl <= 0:
        return None, _skip(row, REASON_NO_PRICE)

    eff_leads = _number(row.get("eff_leads")) or 0.0
    window_days = _number(row.get("window_days"))
    cpl = (cost / eff_leads) if eff_leads > 0 else None

    # Переплата против цены кабинета — утверждение о ПРОШЛОМ, растянутое на
    # горизонт по нынешнему темпу: столько денег кампания тратит сверх того,
    # во что те же эффективные лиды обходятся кабинету. Ноль эффективных лидов
    # даёт переплатой весь расход, и это не преувеличение, а ровно то, что
    # случилось. Цена дешевле кабинетной оставляет ожидание ПУСТЫМ: выгоду
    # правки из этих чисел не вывести, а правдоподобное число вынесло бы идею
    # вперёд посчитанных (registry.rank сравнивает ценность на рубль проверки).
    overpay = cost - eff_leads * base_cpl
    expected = (round(overpay / window_days * HORIZON_WITH_LEARNING, 2)
                if overpay > 0 and window_days and window_days > 0 else None)

    return {
        "source": SOURCE,
        "account": account,
        # Адрес — только кампания. Числа сюда не входят: они пересчитываются
        # каждым прогоном, и войди они в отпечаток, идея заводилась бы заново
        # каждое утро — с пустой историей и снятым отказом человека.
        "subject": {"kind": MASTER_KIND, "campaign_id": campaign_id},
        "tier": tier_mod.TIER_PROPOSAL,
        "lane": lanes_mod.LANE_PROPOSAL,
        "expected_rub": expected,
        # Цены проверки нет, и это не пропуск. Проверку проводит человек в
        # интерфейсе кабинета: агент за неё не платит ни риск-бюджетом, ни
        # разведочным карманом. Ноль здесь означал бы бесплатную проверку и
        # вынес бы идею вперёд тех, кто свою смету посчитал.
        "test_cost_rub": None,
        "horizon_days": HORIZON_WITH_LEARNING,
        "success_rule": {
            "metric": METRIC,
            "op": "<=",
            # Побить цену, по которой кабинет покупает эффективный лид СЕЙЧАС.
            # Цена посчитана по видимым кампаниям, без самой этой: при 10 %
            # расхода кабинета кампания заметно тянет на себя число, с которым
            # её же и сравнивают.
            "value": round(base_cpl, 2),
            "comparison": "vs_account",
        },
        "detail": {
            "needs": NEEDS_HUMAN_HANDS,
            "campaign_name": _text(row.get("campaign_name")) or None,
            "direction": _text(row.get("direction")) or None,
            "cost_rub": round(cost, 2),
            "share_of_account": round(share, 6),
            "account_cost_rub": _number(row.get("account_cost_rub")),
            "clicks": _number(row.get("clicks")),
            "leads": _number(row.get("leads")),
            "eff_leads": eff_leads,
            "revenue_rub": _number(row.get("revenue_rub")),
            "cpl_rub": (round(cpl, 2) if cpl else None),
            "base_cpl_rub": round(base_cpl, 2),
            "window_days": (int(window_days) if window_days else None),
        },
    }, None


def scan(rows: Sequence[Dict[str, Any]],
         ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Карточки кампаний вне витрины → {"ideas": [...], "skipped": [...]}.

    Отбракованные возвращаются рядом с принятыми и с причиной: «Мастера в
    кабинете нет», «Мастер есть, но мелкий» и «это не Мастер, а дефект синка»
    ведут к трём разным следующим шагам, а по пустому счётчику они
    неразличимы.
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
    ideas.sort(key=lambda i: i["subject"]["campaign_id"])
    return {"ideas": ideas, "skipped": skipped}


def candidates(rows: Sequence[Dict[str, Any]],
               ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Только идеи — форма вызова для реестра (registry.upsert)."""
    return scan(rows, ctx)["ideas"]
