# -*- coding: utf-8 -*-
"""
sync/agent/ideas/proven.py — генератор идей: масштабирование доказанного.

Первый из пяти генераторов Ф13. Все пятеро пишут в один реестр
(ideas/registry.py) и НИЧЕГО не применяют сами: идея едет к кабинету только
через такт записи (agent_e1.actions_from_ideas).

Источник — собственный журнал и факты, а не гипотеза. Связка «кампания ×
сегмент × запрос», у которой предельный рубль возвращает больше порога
кабинета λ С ЗАПАСОМ и объём которой проходит лестницу воронки, — это
утверждение о прошлом, подтверждённое деньгами. Поэтому класс здесь
tier.TIER_MEASURED (1), а не ставка: измерение есть, и рычаг у него есть.

Ни один порог в этом файле не заведён заново. Все четыре — из тех модулей,
где они уже работают и по которым уже принимаются решения:

  * ОБЪЁМ — ladder.choose_step: самая глубокая ступень воронки, на которой у
    связки набралось ladder.MIN_STEP_EVENTS = 25 событий (относительная
    ошибка счётчика 20 %). Ступени нет — верить связке нечем.
  * ЗАПАС ПО λ — portfolio.GROWTH_LAMBDA_MARGIN = 1.2. Число не наше: это тот
    же запас, при котором солвер портфеля считает осмысленным доливать сверху
    («ровно на цели растить нечего: предельный рубль там уже равен порогу»).
  * ТОРМОЗ КАЧЕСТВА — карта quality.quality_drift, та же самая, что уже
    останавливает доливку в growth.growth_candidates. Своего порога падения
    скора здесь нет и быть не должно: два определения «качество упало»
    разъехались бы на первой правке одного из них.
  * ЦЕНА ПРОВЕРКИ — risk.action_risk, тот же расчёт, которым такт записи
    списывает риск-бюджет полосы.

**Рычаг, а не повод.** Асимметрия рычага измерена: лимит вниз связывает
всегда, вверх — только у 9 кампаний из 62 (growth.py, docs/AGENT-AUDIT-
2026-08-23.md:214). Поэтому «дать связке больше денег» — не рычаг: у 53
кампаний из 62 поднятый потолок не купит ни одного показа, и писатель
откажет NOT_APPLICABLE_UP_REASON. Рычаг этого генератора один —
КОРРЕКТИРОВКА СТАВКИ СЕГМЕНТА (bidmodifier.add, полоса tuning), и выбран он
по двум причинам сразу:

  1. Его физика не зависит от того, связывает ли лимит. Корректировка не
     трогает бюджет: деньги переносятся ВНУТРИ кампании из остального объёма
     в доказанный сегмент (expectation._bid_modifier, rub_delta = 0). То есть
     объём доказанной связке она даёт и там, где доливка не даёт ничего.
  2. Его полезная нагрузка собирается расчётным тактом ЦЕЛИКОМ. Денежные
     рычаги — нет: budget.set требует блок BiddingStrategy, прочитанный из
     кабинета живьём (writer/budget.diff_budget), tcpa.set — прочитанную
     текущую цель. Идея с недособранной нагрузкой доехала бы до такта записи
     и получила там отказ «применять нечем» — через сутки, в чужом прогоне и
     в чужом логе.

Связка, у которой адреса сегмента нет (кампания × запрос), идеи здесь не
порождает: рычага для неё в этом генераторе нет. Вынос таких связок в
отдельную кампанию — работа задачи 13 (ideas/consolidate.py).

**Почему только add и никогда set.** Перезапись существующей корректировки
требует её Id и её прежнего значения, а previous_state обязан описывать
состояние В МОМЕНТ ПРИМЕНЕНИЯ (правило writer/diff.py). Витрина настроек
(edu_campaign_settings.bidModifiers) снимается раз в сутки, и Id из неё может
быть просроченным — откат по такому previous_state вернул бы кампанию не
туда. Поэтому связка, у которой сегмент УЖЕ под корректировкой, идеи не даёт,
а связка, состояние сегмента у которой вызывающему неизвестно, — тем более:
bidmodifiers.add поверх существующей корректировки Директ отвергает, и
действие переотправлялось бы каждый прогон, съедая потолок попыток.

**Столкновение с корректировками Э1a.** Тот же сегмент может в тот же прогон
получить корректировку из вычисленных настроек (computed.py → writer/diff.py).
Второго механизма разрешения здесь не заводится: план такта уже проходит
conflicts.resolve, где два действия на один сегмент — это
conflicts.DUPLICATE_SEGMENT, и остаётся первое. Идеи попадают в план ПОСЛЕ
рычагов расчёта (agent_e1), то есть уступают им, и это правильный порядок:
корректировка из вычисленных настроек построена по прочитанному состоянию
кабинета, а идея — по витрине.

**Что модуль не делает.** Не ходит в базу и не знает дат: на вход ему подают
уже собранные связки и контекст такта. Не решает, применять ли идею, — это
делают реестр, полосы и ступень автономии. И не молчит об отбракованных:
scan() возвращает их списком с названной причиной, потому что «связок не
нашлось» и «связки были, но все отсеяны» — разные новости.
"""

from typing import Any, Dict, List, Optional, Sequence, Tuple

from sync.agent import computed as computed_mod
from sync.agent import ladder as ladder_mod
from sync.agent.portfolio import GROWTH_LAMBDA_MARGIN
from sync.agent.writer import exposure as exposure_mod
from sync.agent.writer import expectation as expectation_mod
from sync.agent.writer import lanes as lanes_mod
from sync.agent.writer import plan as plan_mod
from sync.agent.writer import risk as risk_mod
from sync.agent.writer import tier as tier_mod
from sync.agent.writer.diff import bidmod_idempotency_key

# Имя источника в реестре. Оно входит в idea_id, поэтому меняться не может:
# смена имени завела бы все идеи генератора заново, с пустой историей и снятым
# отказом человека.
SOURCE = "proven"

ACTION_KIND = "bidmodifier.add"

# Ступень лестницы, на которой доказательства окупаемости ещё НЕТ. «Клики» —
# самая мелкая ступень воронки: выбрав её, лестница сказала, что конверсий на
# вердикт связке не набралось. Окупаемость, посчитанная на таком объёме, — не
# доказательство, а оценка с точностью, которую лестница и отказалась дать.
STEP_WITHOUT_PROOF = "clicks"

REASON_NO_ADDRESS = "адрес связки неполон: нет кабинета или кампании"
REASON_NO_LAMBDA = "порог кабинета λ не посчитан — сравнивать окупаемость не с чем"
REASON_NO_ROMI = "окупаемость связки не посчитана"
REASON_THIN_MARGIN = (
    f"окупаемость выше λ, но без запаса ×{GROWTH_LAMBDA_MARGIN}: ровно на "
    "пороге усиливать нечего — предельный рубль там уже равен порогу")
REASON_NO_LADDER_STEP = ladder_mod.NO_STEP_REASON
REASON_CLICKS_ONLY = (
    "рабочая ступень связки — клики: конверсий на вердикт не набралось, и "
    "окупаемость посчитана на том же объёме, которого лестнице не хватило")
REASON_QUALITY_DROP = (
    "качество когорты кампании упало (quality.quality_drift): рост не покупает "
    "мусор, усиление ждёт вердикта по деньгам")
REASON_NO_SEGMENT = (
    "у связки нет адреса сегмента: рычага корректировки для неё нет, а "
    "денежный рычаг связке не поможет — вынос в отдельную кампанию это "
    "задача генератора consolidate")
REASON_MODIFIER_UNKNOWN = (
    "текущее состояние сегмента в кабинете неизвестно: bidmodifiers.add "
    "поверх существующей корректировки Директ отвергнет, и действие "
    "переотправлялось бы каждый прогон")
REASON_MODIFIER_EXISTS = (
    "сегмент уже под корректировкой: перезапись требует Id и прежнего "
    "значения из ПРОЧИТАННОГО состояния кабинета, а витрина настроек "
    "снимается раз в сутки и может быть просрочена")
REASON_NO_BASE_ROMI = (
    "окупаемость кампании в целом не посчитана: силу корректировки не от чего "
    "отмерить — коэффициент выводится из отношения связки к остальному объёму")
REASON_SMALL_STEP = (
    f"сдвиг меньше ±{plan_mod.MIN_ABS_PERCENT}%: такая корректировка не стоит "
    "запроса и риска (writer/plan.MIN_ABS_PERCENT)")
REASON_NO_EXPECTATION = (
    "рычаг не смог заявить обещание (writer/expectation.of): без доли "
    "сегмента, дневного расхода и цены лида класс 1 не подтверждается, а "
    "замер такта не сможет закрыть наблюдение")


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None   # NaN — то же «неизвестно»


def _text(value: Any) -> str:
    return str(value or "").strip()


def _skip(bundle: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Отбракованная связка с причиной.

    Причина обязательна и всегда непустая: связка, исчезнувшая молча,
    неотличима от связки, которой не было, — и первый же вопрос «почему
    генератор ничего не предложил» превращается в археологию по коду.
    """
    return {
        "campaign_id": _text(bundle.get("campaign_id")),
        "segment": bundle.get("segment"),
        "query": bundle.get("query"),
        "reason": reason,
    }


def _subject(bundle: Dict[str, Any], segment_kind: str,
             segment_key: str) -> Dict[str, Any]:
    """Адрес объекта идеи — и ничего кроме адреса.

    Изменчивые числа (окупаемость, расход, посчитанный процент) сюда не кладут
    ни при каких обстоятельствах: из subject выведен idea_id, и число внутри
    заводило бы идею заново каждым прогоном — с пустой историей и снятым
    отказом человека (докстринг registry.py). Числа едут своими полями.

    Запрос — часть АДРЕСА, а не число: связка «кампания × сегмент × запрос»
    доказана именно на нём, и связка по другому запросу — другая идея.
    Запроса нет — ключа нет вовсе, а не None: пустой ключ дал бы двум формам
    одного и того же адреса разные отпечатки.
    """
    subject: Dict[str, Any] = {
        "campaign_id": _text(bundle.get("campaign_id")),
        "segment": {"kind": segment_kind, "key": segment_key},
    }
    query = _text(bundle.get("query"))
    if query:
        subject["query"] = query
    return subject


def _segment_address(bundle: Dict[str, Any]) -> Tuple[Optional[str], str, str, str]:
    """Сегмент связки → (тип корректировки Директа, вид, канонический ключ, отказ).

    Перевод делает writer/plan.direct_type_for — тот же код, которым в тип
    корректировки переводятся вычисленные настройки. Своя таблица здесь дала
    бы второй справочник устройств и демографии, и первое же расхождение
    отправляло бы в кабинет элемент, который он отвергает.
    """
    segment = bundle.get("segment")
    if not isinstance(segment, dict):
        return None, "", "", REASON_NO_SEGMENT
    kind = _text(segment.get("kind"))
    key = _text(segment.get("key"))
    if not kind or not key:
        return None, kind, key, REASON_NO_SEGMENT
    direct_type, canonical, reason = plan_mod.direct_type_for(kind, key)
    if direct_type is None:
        return None, kind, canonical, reason
    return direct_type, kind, canonical, ""


def _modifier_gate(bundle: Dict[str, Any]) -> str:
    """Пусто, если корректировку можно ДОБАВИТЬ; иначе причина отказа.

    Ключ current_modifier читается по трёхзначной логике, и все три значения
    разные по смыслу:

      * ключа нет         — состояние сегмента вызывающему неизвестно;
      * None              — корректировки в кабинете нет, add применим;
      * число             — корректировка есть, нужен set с её Id.

    Отсутствие ключа НЕ трактуется как «корректировки нет»: молчание источника
    и прочитанный ноль — разные факты, и подмена первого вторым отправила бы в
    кабинет элемент, который он отвергнет.
    """
    if "current_modifier" not in bundle:
        return REASON_MODIFIER_UNKNOWN
    current = bundle.get("current_modifier")
    if current is None:
        return ""
    if _number(current) is None:
        return REASON_MODIFIER_UNKNOWN
    return REASON_MODIFIER_EXISTS


def _action(bundle: Dict[str, Any], campaign_id: str, direct_type: str,
            segment_key: str, percent: int) -> Optional[Dict[str, Any]]:
    """Готовая полезная нагрузка рычага — или None, если обещания нет.

    Форма действия — та же, что у writer/diff.diff_modifiers на ветке add:
    payload несёт человеческие единицы (30 = «+30 %»), в 100-базный
    коэффициент Директа его переводит writer/apply.to_api_call.

    Ожидание навешивается expectation.attach, а не считается здесь. Это
    принципиально: без обещания в payload у действия нет ни ценности для
    отбора полосы, ни числа, с которым замер такта сравнит факт, — а класс 1
    именно на заявленном обещании и держится (tier._computed).
    """
    payload = {
        "CampaignId": int(campaign_id),
        "Type": direct_type,
        "key": segment_key,
        "BidModifier": int(percent),
    }
    share = _number(bundle.get("segment_share"))
    action = {
        "action_kind": ACTION_KIND,
        "object_level": "campaign",
        "object_id": str(campaign_id),
        # Под ударом доля сегмента × сила сдвига, а не расход всей кампании.
        "exposure": exposure_mod.bid_modifier_exposure(int(percent), share),
        # Вид и ключ на верхнем уровне действия — их читают кулдаун, счётчик
        # попыток и разбор конфликтов (conflicts._segment).
        "direct_type": direct_type,
        "key": segment_key,
        "payload": payload,
        # Корректировки в кабинете нет — откат обязан вернуть в нейтраль, а не
        # в выдуманное прежнее значение (то же правило, что у diff.py).
        "previous_state": {},
        "idempotency_key": bidmod_idempotency_key(
            str(campaign_id), direct_type, segment_key, int(percent)),
    }
    context = {
        "segment_share": share,
        "daily_cost_rub": _number(bundle.get("daily_cost_rub")),
        "cpa_rub": _number(bundle.get("cpa_rub")),
    }
    attached = expectation_mod.attach(action, context)
    if attached.get("expected") is None:
        return None
    return attached


def _one(bundle: Dict[str, Any], ctx: Dict[str, Any],
         ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Связка → (идея, отбраковка). Ровно одно из двух непусто."""
    account = _text(ctx.get("account")) or _text(bundle.get("account"))
    campaign_id = _text(bundle.get("campaign_id"))
    if not account or not campaign_id:
        return None, _skip(bundle, REASON_NO_ADDRESS)

    # 1. Окупаемость выше порога кабинета С ЗАПАСОМ.
    lam = _number(ctx.get("lambda"))
    if lam is None or lam <= 0:
        return None, _skip(bundle, REASON_NO_LAMBDA)
    romi = _number(bundle.get("romi"))
    if romi is None:
        return None, _skip(bundle, REASON_NO_ROMI)
    if romi < lam * GROWTH_LAMBDA_MARGIN:
        return None, _skip(bundle, REASON_THIN_MARGIN)

    # 2. Объём проходит лестницу воронки — её собственным порогом.
    counts = bundle.get("counts")
    step = ladder_mod.choose_step(counts if isinstance(counts, dict) else {})
    if step is None:
        return None, _skip(bundle, REASON_NO_LADDER_STEP)
    if step == STEP_WITHOUT_PROOF:
        return None, _skip(bundle, REASON_CLICKS_ONLY)

    # 3. Тормоз качества когорты — карта growth.py, не свой порог.
    drift = (ctx.get("quality_drift") or {}).get(campaign_id) or {}
    if drift.get("flagged"):
        return None, _skip(bundle, REASON_QUALITY_DROP)

    # 4. Рычаг: адрес сегмента и состояние кабинета.
    direct_type, _kind, segment_key, reason = _segment_address(bundle)
    if direct_type is None:
        return None, _skip(bundle, reason)
    reason = _modifier_gate(bundle)
    if reason:
        return None, _skip(bundle, reason)

    # 5. Сила сдвига — отношением связки к остальному объёму кампании, тем же
    #    переводом, каким его считают вычисленные настройки
    #    (computed.bid_modifier_percent: p = clip(r − 1) × 100). Знаменатель —
    #    окупаемость кампании В ЦЕЛОМ: коэффициент говорит «этот сегмент
    #    настолько лучше остального объёма», а не «настолько выше порога».
    base_romi = _number(bundle.get("base_romi"))
    if base_romi is None or base_romi <= 0:
        return None, _skip(bundle, REASON_NO_BASE_ROMI)
    percent = computed_mod.bid_modifier_percent(romi / base_romi)
    if percent < plan_mod.MIN_ABS_PERCENT:
        return None, _skip(bundle, REASON_SMALL_STEP)

    action = _action(bundle, campaign_id, direct_type, segment_key, percent)
    if action is None:
        return None, _skip(bundle, REASON_NO_EXPECTATION)

    lane = lanes_mod.lane_of(action)
    horizon = int(lanes_mod.MEASURE_DAYS[lane])
    expected = action["expected"]
    leads_delta = float(expected["leads_delta"])

    daily_cost = _number(bundle.get("daily_cost_rub")) or 0.0
    test_cost = risk_mod.action_risk(action, {campaign_id: daily_cost},
                                     days_to_measure=horizon)
    value_per_lead = _number(bundle.get("value_per_lead_rub"))

    return {
        "source": SOURCE,
        "account": account,
        "subject": _subject(bundle, _kind, segment_key),
        "tier": tier_mod.TIER_MEASURED,
        "lane": lane,
        # Ценность идеи — обещание рычага в рублях выручки. Нет цены лида —
        # None, а не ноль: непосчитанная ценность и посчитанный ноль стоят в
        # очереди реестра по-разному (registry.rank).
        "expected_rub": (round(leads_delta * value_per_lead, 2)
                         if value_per_lead is not None else None),
        # Цена проверки — тот же расчёт, которым такт записи списывает
        # риск-бюджет полосы (writer/risk.action_risk), а не второе мнение о
        # том, сколько денег под ударом.
        "test_cost_rub": test_cost,
        # Горизонт — срок замера полосы (lanes.MEASURE_DAYS). Своего срока
        # здесь нет: лимит полосы, обещание рычага и критерий идеи обязаны
        # мерить ОДНО окно, иначе факт сравнивается с прогнозом на другой срок.
        "horizon_days": horizon,
        # Критерий — САМО обещание рычага, без запаса и скидки. Порог «хотя бы
        # половина обещанного» был бы выдуманным числом; обещание же посчитано
        # моделью переноса и проверяемо целиком. База сравнения названа явно:
        # корректировка судится против заповедника, а не «до и после» — на
        # одной кампании эффект конфаундится сезоном и обучением.
        "success_rule": {
            "metric": "leads_delta",
            "op": ">=",
            "value": leads_delta,
            "comparison": "did_vs_holdout",
        },
        "action": action,
    }, None


def scan(bundles: Sequence[Dict[str, Any]],
         ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Связки → {"ideas": [...], "skipped": [...]}.

    Отбракованные возвращаются рядом с принятыми и с причиной. Молчаливый
    отсев здесь стоил бы ровно того же, что молчаливый отсев в плане записи:
    человек видит пустой список и не может отличить «поводов не было» от
    «поводы были, но у всех не хватило рычага».
    """
    ideas: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for bundle in bundles or ():
        if not isinstance(bundle, dict):
            continue
        idea, refusal = _one(bundle, ctx or {})
        if idea is not None:
            ideas.append(idea)
        else:
            skipped.append(refusal)
    return {"ideas": ideas, "skipped": skipped}


def candidates(bundles: Sequence[Dict[str, Any]],
               ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Только идеи — форма вызова для реестра (registry.upsert)."""
    return scan(bundles, ctx)["ideas"]
