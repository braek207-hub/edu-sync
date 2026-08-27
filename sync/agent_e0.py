# -*- coding: utf-8 -*-
"""
sync/agent_e0.py — прогон Э0 автопилота: фундамент и майнинг истории.

Э0 НИЧЕГО не пишет в Яндекс Директ. Только читает и складывает результат в БД.
Движок записи появляется на Э1.

Порядок: гейт данных → факты → заповедник → квазиэксперименты → вычисляемые
настройки → срезы → структура → снимок настроек → профиль → отчёт мощности.
Красный гейт останавливает прогон: работать на битых данных хуже, чем не работать.

Запуск:  python -m sync.agent_e0 [--days 180] [--skip-direct]
ENV:     DATABASE_URL, DIRECT_TOKEN, DIRECT_CLIENTS_JSON
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sync.agent import blackbox
from sync.agent import db as agent_db
from sync.agent.computed import compute_schedule, compute_segment_modifiers
from sync.agent.confidence import thresholds_from_config
from sync.agent.coverage import blind_spend
from sync.agent.demand import REGION as DEMAND_REGION
from sync.agent.demand import demand_regime, directions_without_series
from sync.agent.facts import assemble_facts
from sync.agent.guard import (
    check_continuity,
    check_freshness,
    check_funnel_depth,
    check_sum_reconciliation,
    check_volume_anomaly,
    verdict,
)
from sync.agent.holdout import select_holdout
from sync.agent import config as agent_config
from sync.agent import semantic

# Чем занимается проект, когда паспорта направления нет, — умолчание самого
# слоя (semantic.DEFAULT_CONTEXT). Своей копии здесь больше нет: две строки об
# одном продукте разъезжаются молча, и прогон судил бы фразы описанием, которое
# правили в другом файле.
SEMANTIC_CONTEXT = semantic.DEFAULT_CONTEXT

from sync.agent.metrika import (
    EDU_COUNTERS,
    fetch_campaign_behavior,
    fetch_counter_goal_ids,
    fetch_hourly_profile,
    resolve_counter_account,
)
from sync.agent.growth import LEVER_BUDGET, growth_candidates
from sync.agent.quality import (
    QUALITY_WINDOW_DAYS,
    lead_quality_section,
)
from sync.agent.hierarchy import hierarchical_modifiers
from sync.agent.ladder import ladder_report
from sync.agent.history import budget_response
from sync.agent.learning_loop import forecast_bias, track_record
from sync.agent.mining import mine_quasi_experiments, placebo_sigma
from sync.agent.portfolio import computed_rows as portfolio_computed_rows
from sync.agent.portfolio import portfolio_targets
from sync.agent.ideas import abtests as ideas_abtests
from sync.agent.ideas import bundles as ideas_bundles
from sync.agent.ideas import consolidate as ideas_consolidate
from sync.agent.ideas import market as ideas_market
from sync.agent.ideas import proven as ideas_proven
from sync.agent.ideas import registry as ideas_registry
from sync.agent.writer import db as writer_db
from sync.agent.writer import tier as tier_mod
from sync.agent.writer.negatives import computed_rows as negative_computed_rows
from sync.agent.writer.placements import computed_rows as placement_computed_rows
from sync.agent.tcpa import (
    build_inputs as build_tcpa_inputs,
    computed_rows as tcpa_computed_rows,
    tcpa_targets,
)
from sync.agent.headroom import VERDICT_BOUGHT_OUT as HEADROOM_BOUGHT_OUT
from sync.agent.headroom import VERDICT_ROOM as HEADROOM_ROOM
from sync.agent.headroom import VERDICT_UNDETERMINED as HEADROOM_UNDETERMINED
from sync.agent.headroom import computed_rows as headroom_computed_rows
from sync.agent.headroom import placement_modes, traffic_headroom
from sync.agent.saturation import RECENT_DAYS
from sync.agent.saturation import computed_rows as saturation_computed_rows
from sync.agent.saturation import saturation_curves
from sync.agent.objects import (
    build_object_rows,
    CANDIDATE_WINDOW_DAYS,
    minus_word_candidates,
    placement_candidates,
    core_words,
    phrases_cutting_only_waste,
    expansion_candidates,
    word_minus_candidates,
    top_queries_by_cost,
)
from sync.agent.power import power_report
from sync.agent.profile import build_profile, campaign_quality, distance_to_profile
from sync.agent.segment_quality import (
    apply_quality_to_modifiers,
    device_quality_ratios,
)
from sync.agent.segments import (
    fetch_account_goal_ids,
    fetch_campaign_ids,
    fetch_objects,
    fetch_placements,
    fetch_search_queries,
    fetch_segment_report,
)
from sync.agent.settings_snapshot import build_snapshot_rows
from sync.agent.slices import build_sliced_facts, collapse_tail

DEFAULT_DAYS = 180
# Срезы, структура и поисковые запросы — только за квартал: 90 дней покрывают все
# сезонные фазы, кроме прошлогодней приёмки, а объём таблиц держат в десятках МБ.
SLICE_WINDOW_DAYS = 90
# Поисковые запросы — самая объёмная витрина (450 МБ за 90 дней на 4 кабинета).
# Кандидаты в минус-слова считаются по свежим данным, глубокая история не нужна.
QUERY_WINDOW_DAYS = CANDIDATE_WINDOW_DAYS
PROFILE_FEATURES = ["groups_count", "phrases_per_group", "title2_fill_share"]
# Пороги свежести РАЗНЫЕ, потому что источники разной природы.
#
# Расход Директа приезжает своим синком и почти не отстаёт: трое суток без
# новых строк — это уже поломка синка, а не задержка.
DIRECT_MAX_AGE_HOURS = 72
# CRM отстаёт ШТАТНО на 2-4 дня: выгрузка из Битрикса в Google-таблицу идёт
# не каждый день (слова владельца кабинета + замер 21.08.2026). Общий порог в
# 72 часа ронял ВЕСЬ расчёт Э0 на этой норме — то есть защита срабатывала на
# штатной ситуации и просто останавливала работу. Это тот же класс дефекта,
# что вечный RED у гейта витрины сторожа.
#
# Шесть суток — запас поверх наблюдаемого лага, но заметно меньше настоящей
# поломки: 02.08.2026 таблица встала на четверо суток и никто не заметил
# четыре дня (см. sync/data_freshness.py). За неделю тишины падать обязаны.
CRM_MAX_AGE_HOURS = 144
# Оставлено для совместимости чтения старых записей гейта в edu_agent_guard.
HISTORY_MAX_AGE_HOURS = DIRECT_MAX_AGE_HOURS
# Директ ограничивает число одновременно формируемых отчётов на кабинет.
REPORT_WORKERS = 4
# Глубина чтения спроса Wordstat: базовое окно режима (demand.BASELINE_WEEKS = 8)
# плюс запас на пропущенные недели выгрузки — с трёхкратным запасом база
# набирается даже на рваном ряде.
DEMAND_HISTORY_WEEKS = 26


def _window(days: int) -> tuple:
    today = date.today()
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def _direct_clients() -> List[Dict[str, Any]]:
    """Кабинеты с целями. DIRECT_CLIENTS_JSON — список словарей
    {login, goal_ids, sheet_name}, формат задан sync/direct.py::_direct_clients.

    Цели нужны не для красоты: без Goals в запросе Reports API не отдаёт колонку
    Conversions и отвергает FieldNames с ошибкой 8000.

    Логин нормализуется общей функцией agent_db.normalize_login — той же, что
    у движка записи и у самой таблицы: он становится ключом object_id, и
    расхождение нормализаций разводит запись и чтение по разным ключам.
    """
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    out: List[Dict[str, Any]] = []
    if raw:
        for item in json.loads(raw):
            if isinstance(item, dict):
                login = agent_db.normalize_login(item.get("login"))
                goals = item.get("goal_ids") or item.get("goals") or []
            else:
                login, goals = agent_db.normalize_login(item), []
            if login:
                out.append({"login": login, "goal_ids": [str(g) for g in goals]})
        if out:
            return out
    login = agent_db.normalize_login(os.environ.get("DIRECT_CLIENT_LOGIN"))
    return [{"login": login, "goal_ids": []}] if login else []


def resolve_goal_ids(client: Dict[str, Any]) -> List[str]:
    """Цели кабинета: явно указанные оператором, иначе выведенные из кабинета.

    Без целей Reports API не отдаёт Conversions, а без Conversions сегментный
    расчёт отказывается работать целиком — на прогоне 32406152097 так отказали
    все двенадцать срезов, и применять автопилоту было нечего. Единственным
    источником целей был секрет DIRECT_CLIENTS_JSON: не проставил руками —
    агент слеп, и заметить это можно было только по причине отказа.

    Поэтому источник по умолчанию — сам кабинет: цели, на которые настроены
    стратегии его кампаний. Значение из секрета остаётся главнее — это явное
    решение оператора сузить набор, и молча подменять его нельзя.
    """
    explicit = [str(g) for g in (client.get("goal_ids") or [])]
    if explicit:
        return explicit
    login = client["login"]
    try:
        found = fetch_account_goal_ids(login)
    except Exception as e:
        # Отказ здесь не должен ронять весь прогон: остальные шаги Э0 (факты,
        # объекты, майнинг) от целей не зависят и обязаны отработать.
        print(f"  [agent_e0] цели кабинета {login} не получены: {e}")
        return []
    if not found:
        print(f"  [agent_e0] у кампаний кабинета {login} не задано ни одной цели "
              f"оптимизации — конверсии по срезам считаться не будут")
    else:
        print(f"  [agent_e0] цели кабинета {login} из стратегий кампаний: {len(found)}")
    return [str(g) for g in found]


def _daily_cost(direct_rows: List[Dict[str, Any]]) -> List[tuple]:
    """Расход по дням, по возрастанию даты: [(дата, сумма), ...].

    Последний день — «сегодняшний» для проверки аномалии, предыдущие — история.
    Дни считаются по строкам источника, а не по витрине: гейт стоит ДО сборки
    фактов и обязан судить о том, что пришло, а не о том, что мы записали.
    """
    by_day: Dict[str, float] = {}
    for row in direct_rows:
        by_day[str(row["date"])] = by_day.get(str(row["date"]), 0.0) + float(row.get("cost") or 0.0)
    return sorted(by_day.items())


def _cost_up_to(daily_cost: List[tuple], last_day: Optional[str]) -> float:
    """Расход источника по дням не позже last_day — сторона сверки сумм.

    last_day = None означает пустую витрину: сверять не с чем, отдаём ноль, и
    сверка уходит в свою ветку «обе стороны нули». Иначе сравнивались бы полное
    окно источника и пустая витрина — «поломка» на первом же прогоне.
    """
    if not last_day:
        return 0.0
    return sum(cost for day, cost in daily_cost if str(day) <= last_day)


# Окно лестницы: 90 дней зрелых данных. Длиннее — глубокие ступени набираются
# лучше, но оценка тянет прошлый сезон; 90 совпадает с SLICE_WINDOW_DAYS срезов.
LADDER_WINDOW_DAYS = 90
# Перцентиль лага оплаты, после которого когорта считается созревшей. По замеру
# probe_economics (прогон 32590373892): p90 = 33 дня, к 30-му дню видно 88 %
# выручки когорты. Значение НЕ константа — каждый прогон выводит его из данных.
LADDER_MATURITY_PERCENTILE = 0.90
# Возраст когорты, начиная с которого её лаги считаются НЕцензурированными.
# Свежая когорта физически не может показать длинный лаг: её длинные оплаты
# ещё не случились, и в выборку от неё попадают только короткие. Считать её
# наравне со зрелыми — правая цензура: p90 занижается, «зрелое» окно решений
# оказывается незрелым, а value направлений с длинным лагом занижена (аудит
# 2026-08-23). Порог — вдвое больше замеренного p90 = 33 дня: когорта старше
# 180 дней успела показать свой хвост целиком.
LAG_COHORT_MIN_AGE_DAYS = 180
# Мост «лид → устройство» для качества сегментов (Э2.2b): окно длинное, потому
# что глубокие ступени (сделки, оплаты) редкие — за квартал планшеты не набирают
# даже соединений. 540 дней = окно probe 32622086445, на котором мера доказана.
BRIDGE_LOOKBACK_DAYS = 540

_LADDER_FIELDS = {
    "paid": "payments_fact", "deals": "deals", "connected": "connected_leads",
    "eff": "eff_leads", "leads": "leads", "clicks": "clicks",
}


def _lag_percentile(lead_rows: List[Dict[str, Any]], q: float,
                    today: date = None,
                    min_age_days: int = LAG_COHORT_MIN_AGE_DAYS) -> int:
    """Перцентиль лага оплаты по НЕцензурированным когортам.

    Учитываются лиды, созданные не позже чем min_age_days назад: у более
    свежих длинные оплаты ещё не случились, и их короткие лаги утягивают
    перцентиль вниз — см. LAG_COHORT_MIN_AGE_DAYS. Зрелых когорт нет вовсе
    (история короче порога) — считаем по всем, что есть: цензурированная
    оценка всё же лучше нуля, а ноль означал бы «окно не сдвигать», то есть
    решения по совсем незрелым дням.
    """
    def _as_date(value):
        if value is None or isinstance(value, date):
            return value
        return date.fromisoformat(str(value)[:10])

    today = today or date.today()
    cutoff = today - timedelta(days=min_age_days)
    lags, mature_lags = [], []
    for r in lead_rows:
        paid_on, created = _as_date(r.get("payment_date")), _as_date(r.get("created_date"))
        if r.get("is_paid") and paid_on and created:
            lags.append((paid_on - created).days)
            if created <= cutoff:
                mature_lags.append((paid_on - created).days)
    ordered = sorted(mature_lags or lags)
    if not ordered:
        return 0
    return int(ordered[min(int(q * len(ordered)), len(ordered) - 1)])


def _count_by(rows, key):
    """Счётчик значений поля — для отчётных разбивок."""
    out = {}
    for row in rows:
        value = str(row.get(key) or "")
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items()))


# Сколько идей уезжает в отчёт списком. Счётчики выше показывают весь реестр,
# а разбирать глазами человек будет верх очереди: остальное лежит в
# edu_agent_ideas и достаётся запросом.
IDEAS_SAMPLE_LIMIT = 10


def _idea_line(idea: Dict[str, Any]) -> Dict[str, Any]:
    """Идея реестра → строка отчёта. Числа и адрес, без служебных колонок."""
    return {
        "idea_id": idea.get("idea_id"),
        "source": idea.get("source"),
        "account": idea.get("account"),
        "tier": idea.get("tier"),
        "lane": idea.get("lane"),
        "status": idea.get("status"),
        "expected_rub": idea.get("expected_rub"),
        "test_cost_rub": idea.get("test_cost_rub"),
        "horizon_days": idea.get("horizon_days"),
        "subject": idea.get("subject"),
        "success_rule": idea.get("success_rule"),
    }


def ideas_section(ideas: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Реестр идей в отчёте прогона. Печатается ВСЕГДА, в том числе пустым.

    Пустая секция и отсутствующая — разные новости. Первая говорит
    «генераторы отработали, находок нет», вторая читается как «генератор не
    запускался», и различить их задним числом по логу нечем: реестр к тому
    времени выглядит одинаково пустым в обоих случаях. Поэтому счётчики стоят
    нулями, а не сворачиваются в отсутствие ключа.

    Предложения (класс 3) считаются ОТДЕЛЬНО от очереди. Они не применяются
    никогда (writer/tier.py) и в такт записи не едут — но это и есть тот
    экран, ради которого реестр заведён, и смешать их с очередью значило бы
    показывать человеку список, часть которого агент молча заберёт себе.

    Порядок очереди берётся как есть: его задал реестр (registry.rank —
    ценность на рубль проверки), и вторая сортировка здесь развела бы экран
    человека с очередью такта записи.
    """
    items = list(ideas or [])
    queue = [i for i in items
             if ideas_registry.idea_tier(i) in tier_mod.APPLIED_TIERS]
    proposals = [i for i in items
                 if ideas_registry.idea_tier(i) == tier_mod.TIER_PROPOSAL]
    return {
        "open": len(items),
        "by_status": _count_by(items, "status"),
        "by_source": _count_by(items, "source"),
        "by_lane": _count_by(items, "lane"),
        # Классы считаются через registry.idea_tier, а не общим _count_by:
        # тот пишет пустую строку вместо нуля (str(0 or "")), и класс 0 —
        # арифметика, самая массовая часть находок — исчез бы из разбивки в
        # безымянную графу.
        "by_tier": _count_by(
            [{"tier": str(ideas_registry.idea_tier(i))} for i in items], "tier"),
        # Обещание реестра и цена его проверки. Без них счётчик идей не
        # говорит ничего: три идеи по сто рублей и три по миллиону выглядят
        # одинаково.
        "expected_rub": round(sum(float(i.get("expected_rub") or 0.0)
                                  for i in items), 2),
        "test_cost_rub": round(sum(float(i.get("test_cost_rub") or 0.0)
                                   for i in items), 2),
        "proposals": {"count": len(proposals),
                      "sample": [_idea_line(i)
                                 for i in proposals[:IDEAS_SAMPLE_LIMIT]]},
        "queue": [_idea_line(i) for i in queue[:IDEAS_SAMPLE_LIMIT]],
    }


def _account_value_per_payment(index: Dict[str, Any], account: str) -> Optional[float]:
    """Средний чек кабинета — по СОСТОЯВШИМСЯ оплатам его кампаний.

    Взвешивание оплатами, а не расходом и не поровну: чек кабинета — это то,
    сколько в среднем принесла оплата, и кампания с двумя оплатами не вправе
    тянуть его наравне с кампанией, у которой их двести. Оплат нет ни у одной
    кампании — None, а не ноль: непосчитанная ценность и посчитанный ноль
    стоят в очереди реестра по-разному (registry.rank).
    """
    revenue = 0.0
    payments = 0.0
    for entry in index.values():
        if entry.get("account") != account or entry.get("avg_check") is None:
            continue
        paid = float((entry.get("counts") or {}).get("paid") or 0.0)
        if paid <= 0:
            continue
        revenue += float(entry["avg_check"]) * paid
        payments += paid
    return (revenue / payments) if payments > 0 else None


def collect_ideas(
    *,
    facts: List[Dict[str, Any]],
    ladder_section: Dict[str, Any],
    portfolio_section: Dict[str, Any],
    sliced_rows: List[Dict[str, Any]],
    query_rows: List[Dict[str, Any]],
    expansion: List[Dict[str, Any]],
    demand: Dict[str, Dict[str, Any]],
    settings_by_campaign: Dict[str, Dict[str, Any]],
    login_by_campaign: Dict[str, str],
    direction_by_campaign: Dict[str, str],
    holdout_ids: List[str],
    learning_reset: Dict[str, Any],
    quality_drift: Dict[str, Any],
    config: Dict[str, Any],
    slice_window_days: int,
    query_window_days: int,
    today: date = None,
) -> Dict[str, Any]:
    """Генераторы идей на данных такта: собрать вход, позвать, записать в реестр.

    До этой функции все пять генераторов Ф13 были мёртвым кодом: чистые
    функции существовали, а звать их было некому — registry.upsert в бою не
    вызывался ниоткуда, реестр оставался пустым, и секция ideas честно
    печатала нули. Отчёт при этом выглядел работающим.

    Порция пишется ОТДЕЛЬНО по каждому источнику. Реестр принимает порцию
    целиком или никак (registry.upsert), и одна кривая находка обязана уронить
    находки своего генератора, а не всего такта: у пяти генераторов нет ни
    общего кода, ни общей причины ошибиться. Отказ реестра при этом не роняет
    прогон — он становится строкой отчёта: расчётный такт считает деньги, и
    падать из-за экрана предложений ему нельзя.

    Кабинет — рамка всего. Идеи считаются по кабинетам, а не по кабинету
    вообще: порог λ, средний чек и заповедник у каждого свои, и связка одного
    кабинета, посуженная порогом другого, — это приговор по чужой мерке.
    Спрос рынка при этом общий (Wordstat не знает про кабинеты), поэтому
    поводы спроса раздаются кабинету только по ЕГО направлениям: растущее
    направление, которого в кабинете нет вовсе, адресовать некому, и выбор
    «кому его завести» из данных не выводится.
    """
    today = today or date.today()
    index = ideas_bundles.campaign_index(
        facts, ladder_section, portfolio_section,
        login_by_campaign=login_by_campaign,
        settings_by_campaign=settings_by_campaign,
        direction_by_campaign=direction_by_campaign)

    segments = ideas_bundles.segment_bundles(
        sliced_rows, index, window_days=slice_window_days)
    donors = ideas_bundles.query_donors(
        query_rows, index,
        phrases=[c.get("query") for c in (expansion or ())],
        window_days=query_window_days)
    tests = ideas_bundles.campaign_tests(
        index, holdout_ids=holdout_ids, learning_reset=learning_reset,
        today=today)

    # Цена эффективного лида по направлениям и живые направления кабинета —
    # на окне лестницы, том же, на котором посчитана ценность лида. Другое
    # окно означало бы, что критерий успеха идеи («побить нынешнюю цену»)
    # мерит не ту цену, о которой говорит остальной отчёт.
    window_from = ladder_section.get("window_from") or ""
    window_to = ladder_section.get("window_to") or ""
    cost_by_direction: Dict[str, float] = {}
    eff_by_direction: Dict[str, float] = {}
    live_by_account: Dict[str, set] = {}
    for fact in facts or ():
        fact_date = str(fact.get("fact_date"))[:10]
        if not (window_from <= fact_date <= window_to):
            continue
        direction = fact.get("direction") or ""
        if not direction:
            continue
        cost = float(fact.get("cost") or 0.0)
        cost_by_direction[direction] = cost_by_direction.get(direction, 0.0) + cost
        eff_by_direction[direction] = (eff_by_direction.get(direction, 0.0)
                                       + float(fact.get("eff_leads") or 0.0))
        if cost > 0:
            login = login_by_campaign.get(str(fact.get("campaign_id")))
            if login:
                live_by_account.setdefault(login, set()).add(direction)
    cpl_by_direction = {d: cost_by_direction[d] / eff_by_direction[d]
                        for d in cost_by_direction if eff_by_direction.get(d, 0.0) > 0}

    uncovered_by_direction: Dict[str, List[str]] = {}
    for candidate in expansion or ():
        for campaign_id in candidate.get("campaigns") or ():
            direction = direction_by_campaign.get(str(campaign_id))
            if not direction:
                continue
            phrases = uncovered_by_direction.setdefault(direction, [])
            if candidate.get("query") and candidate["query"] not in phrases:
                phrases.append(candidate["query"])

    accounts = sorted({entry["account"] for entry in index.values()
                       if entry.get("account")})
    lambdas = {login: acc.get("lambda")
               for login, acc in ((portfolio_section or {}).get("accounts") or {}).items()}

    by_source: Dict[str, Dict[str, Any]] = {}
    failed: Dict[str, str] = {}

    def _run(source: str, ideas: List[Dict[str, Any]],
             skipped: List[Dict[str, Any]]) -> None:
        slot = by_source.setdefault(
            source, {"ideas": 0, "upserted": 0, "skipped_by_reason": {}})
        slot["ideas"] += len(ideas)
        for row in skipped:
            reason = str(row.get("reason") or "")
            slot["skipped_by_reason"][reason] = (
                slot["skipped_by_reason"].get(reason, 0) + 1)
        if not ideas:
            return
        try:
            slot["upserted"] += len(ideas_registry.upsert(ideas))
        except Exception as exc:  # noqa: BLE001
            failed[source] = f"{type(exc).__name__}: {exc}"[:300]

    for account in accounts:
        own = {cid for cid, entry in index.items() if entry["account"] == account}
        ctx = {
            "account": account,
            "lambda": lambdas.get(account),
            "quality_drift": quality_drift,
            "holdout_ids": holdout_ids,
            "config": config,
            "value_per_payment_rub": _account_value_per_payment(index, account),
        }
        found = ideas_proven.scan(
            [b for b in segments["bundles"] if b["campaign_id"] in own], ctx)
        _run(ideas_proven.SOURCE, found["ideas"], found["skipped"])

        found = ideas_consolidate.scan(
            [r for r in donors["rows"] if r["campaign_id"] in own], ctx)
        _run(ideas_consolidate.SOURCE, found["ideas"], found["skipped"])

        found = ideas_abtests.scan(
            [r for r in tests if r["campaign_id"] in own], ctx)
        _run(ideas_abtests.SOURCE, found["ideas"], found["skipped"])

        directions = live_by_account.get(account, set())
        found = ideas_market.scan(ideas_bundles.demand_rows(
            {d: r for d, r in (demand or {}).items() if d in directions},
            account=account,
            uncovered_by_direction=uncovered_by_direction,
            cpl_by_direction=cpl_by_direction,
            live_directions=sorted(directions)), ctx)
        _run(ideas_market.SOURCE, found["ideas"], found["skipped"])

    return {
        "accounts": len(accounts),
        "bundles": {
            "segments": len(segments["bundles"]),
            "donors": len(donors["rows"]),
            "campaigns": len(tests),
            "skipped_by_reason": _count_by(
                segments["skipped"] + donors["skipped"], "reason"),
        },
        "by_source": by_source,
        # Генератор, которому такт не даёт входа вовсе. Ноль находок у него —
        # не «поводов не нашлось», а «спрашивать было не о чем», и по пустому
        # счётчику эти два состояния неразличимы.
        "sources_without_input": dict(ideas_bundles.SOURCES_WITHOUT_INPUT),
        # Отказ реестра в порции. Пусто — все порции приняты.
        "failed": failed,
    }


def funnel_ladder_section(
    facts: List[Dict[str, Any]],
    lead_rows: List[Dict[str, Any]],
    today: date = None,
) -> Dict[str, Any]:
    """Лестница воронки по кампаниям (Э2.1) — на ЗРЕЛОМ окне.

    Окно сдвинуто в прошлое на p90 лага оплаты, выведенный из данных прогона:
    у свежих недель глубокие ступени (оплаты, сделки) ещё не доехали, и лестница
    выбирала бы мелкую ступень не потому, что данных нет, а потому что они не
    созрели. Средний чек — по направлению из зрелых оплат: покампанийный чек на
    единичных оплатах — шум.
    """
    today = today or date.today()
    maturity_days = _lag_percentile(lead_rows, LADDER_MATURITY_PERCENTILE,
                                    today=today)
    window_to = (today - timedelta(days=maturity_days)).isoformat()
    window_from = (today - timedelta(days=maturity_days + LADDER_WINDOW_DAYS)).isoformat()

    def _zero() -> Dict[str, float]:
        return {step: 0.0 for step in _LADDER_FIELDS}

    by_campaign: Dict[str, Dict[str, float]] = {}
    by_direction: Dict[str, Dict[str, float]] = {}
    account = _zero()
    direction_of: Dict[str, str] = {}
    for f in facts:
        fact_date = str(f["fact_date"])[:10]
        if not (window_from <= fact_date <= window_to):
            continue
        campaign_id = str(f["campaign_id"])
        direction = f.get("direction") or "БЕЗ_НАПРАВЛЕНИЯ"
        direction_of[campaign_id] = direction
        camp = by_campaign.setdefault(campaign_id, _zero())
        direct = by_direction.setdefault(direction, _zero())
        for step, field in _LADDER_FIELDS.items():
            value = float(f.get(field) or 0.0)
            camp[step] += value
            direct[step] += value
            account[step] += value

    # Средний чек направления — только зрелые оплаты.
    check_sum: Dict[str, float] = {}
    check_n: Dict[str, int] = {}
    for r in lead_rows:
        if not r.get("is_paid") or str(r.get("created_date"))[:10] > window_to:
            continue
        direction = r.get("direction") or "БЕЗ_НАПРАВЛЕНИЯ"
        check_sum[direction] = check_sum.get(direction, 0.0) + float(r.get("revenue") or 0.0)
        check_n[direction] = check_n.get(direction, 0) + 1
    avg_check = {d: check_sum[d] / check_n[d] for d in check_n if check_n[d] > 0}

    pools = {
        campaign_id: (
            (f"direction:{direction_of[campaign_id]}",
             by_direction[direction_of[campaign_id]]),
            ("account", account),
        )
        for campaign_id in by_campaign
    }
    checks = {
        campaign_id: avg_check[direction_of[campaign_id]]
        for campaign_id in by_campaign
        if direction_of[campaign_id] in avg_check
    }
    report = ladder_report(by_campaign, pools, avg_check_by_object=checks)
    report["maturity_days"] = maturity_days
    report["window_from"] = window_from
    report["window_to"] = window_to
    # Пулы и чеки — наружу, а не только внутрь отчёта. По ним генераторы идей
    # переводят события связки (сегмента, запроса) в оплаты и выручку: своих
    # коэффициентов перехода у связки быть не может — событий на них не
    # набралось бы никогда, — и второй расчёт рядом означал бы, что кампанию и
    # её сегмент такт судит по разным коэффициентам.
    report["counts"] = {"by_direction": by_direction, "account": account}
    report["avg_check"] = checks
    return report


def _top_confident_moves(moves: Dict[str, Dict[str, Any]], limit: int = 5) -> Dict[str, Any]:
    """Топ уверенных сдвигов бюджета по модулю переноса — для отчёта прогона."""
    confident = [(cid, m) for cid, m in moves.items()
                 if m["confident"] and m["move"] != "hold"]
    confident.sort(key=lambda kv: -abs(kv[1]["target_28d"] - kv[1]["cost_28d"]))
    return dict(confident[:limit])


def computed_rows_for_job(job: Dict[str, Any]) -> tuple:
    """Результат одного отчёта → (кабинет, строки настроек, причина отказа).

    Кабинет едет ВМЕСТЕ с числами и дальше становится object_id записи. Без
    него строки четырёх кабинетов ложились в один ключ таблицы и перетирали
    друг друга, а движок записи раскатывал выживший набор на всех.

    Конверсионность считается по Conversions самого отчёта Директа. Раньше сюда
    подавались ожидаемые оплаты, размазанные по доле кликов, — из-за чего
    конверсионность всех сегментов среза совпадала и «корректировка по сегменту»
    сегменты не различала. Причина отказа возвращается наружу и печатается в
    отчёте прогона: вырождение обязано быть видно, а не тихо давать нули.
    """
    rows, reason = compute_segment_modifiers(job["rows"])
    return job["login"], rows, reason


def main() -> int:
    days = DEFAULT_DAYS
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    skip_direct = "--skip-direct" in sys.argv
    date_from, date_to = _window(days)
    today_iso = date.today().isoformat()

    agent_db.ensure_agent_tables()

    # Настройки агента: пресет и переопределения из БД поверх кодовых
    # дефолтов. Битая настройка роняет прогон намеренно — молча
    # проигнорированная выглядит как применённая (sync/agent/config.py).
    try:
        stored_config = agent_db.load_agent_config()
    except Exception as exc:  # noqa: BLE001
        # Настройки — надстройка над кодовыми дефолтами: их недоступность не
        # имеет права ронять расчёт. Дефолты равны константам кода, то есть
        # поведение остаётся прежним, а причина видна в отчёте.
        stored_config = {"preset": None, "overrides": {},
                         "unavailable": f"{type(exc).__name__}: {exc}"[:200]}
    active_config = agent_config.resolve(stored_config["preset"],
                                         stored_config["overrides"])
    active_config_rows = agent_config.describe(stored_config["preset"],
                                               stored_config["overrides"])
    if stored_config.get("unavailable"):
        active_config_rows = [{"key": "__source__", "value": "кодовые дефолты",
                               "source": "unavailable",
                               "about": stored_config["unavailable"]}] + active_config_rows
    # Пороги уверенности по классам действий — из того же активного конфига.
    # Считаются один раз на прогон: они не зависят ни от кабинета, ни от
    # объекта, и пересчёт их в каждом вызове означал бы, что где-то они могут
    # разойтись.
    confidence_thresholds = thresholds_from_config(active_config)

    direct_rows = agent_db.load_direct_rows(date_from, date_to)
    lead_rows = agent_db.load_lead_rows(date_from, date_to)
    score_rows = agent_db.load_score_rows(date_from, date_to)

    # 1. Гейт качества данных.
    now_iso = datetime.now(timezone.utc).isoformat()
    latest_direct = max((str(r["date"]) for r in direct_rows), default=None)
    latest_lead = max((str(r["created_date"]) for r in lead_rows), default=None)
    # Два вызова с разными порогами: у источников разная норма отставания, и
    # мерить их одной меркой значит либо ронять расчёт на штатном лаге CRM,
    # либо проспать вставший синк Директа.
    checks: List[Dict[str, Any]] = check_freshness(
        {"direct_stats": f"{latest_direct}T00:00:00+00:00" if latest_direct else None},
        now_iso=now_iso,
        max_age_hours=DIRECT_MAX_AGE_HOURS,
    )
    checks += check_freshness(
        {"crm_lead_details": f"{latest_lead}T00:00:00+00:00" if latest_lead else None},
        now_iso=now_iso,
        max_age_hours=CRM_MAX_AGE_HOURS,
    )
    # Аномалия дневного объёма и сверка сумм — два слоя гейта, которые до сих
    # пор существовали, но НЕ ВЫЗЫВАЛИСЬ: написаны, покрыты тестами, зелёные и
    # ни разу не отработавшие. Гейт проверял только свежесть и непрерывность,
    # то есть обвал объёма вдвое проходил насквозь, если даты на месте.
    daily_cost = _daily_cost(direct_rows)
    if daily_cost:
        history = [cost for _, cost in daily_cost[:-1]]
        checks.append(check_volume_anomaly(history, daily_cost[-1][1]))
        # Расход по источнику против расхода, уже лежащего в витрине: расхождение
        # означает, что витрина собрана не из этих строк.
        #
        # Сверяется ОБЩИЙ интервал, а не полное окно. Витрина отстаёт от источника
        # на прогон по построению: свежие дни источник отдаёт сразу, а записывает
        # их только текущий прогон — ниже по коду. Сравнение полного окна с
        # недописанной витриной меряет лаг, а не сохранность данных, и 27.08.2026
        # уронило e0 на ровном месте: 141 287 763 против 139 824 423 (1,04 % при
        # пороге 1 %). Вся разница до копейки лежала в двух недописанных днях
        # (26.08 недобран, 27.08 отсутствует), ни одна кампания не терялась.
        # Хуже того, клин самоподдерживающийся: пока факты не записаны, разрыв
        # растёт с каждым днём, и следующий прогон падает вернее предыдущего.
        # Обе стороны берутся от одной границы: пустая витрина — это ноль против
        # нуля, а не полное окно источника против неизвестно чего.
        mart_last = agent_db.mart_last_fact_date(date_from, date_to)
        checks.append(check_sum_reconciliation(
            _cost_up_to(daily_cost, mart_last),
            agent_db.mart_cost_total(date_from, mart_last) if mart_last else 0.0,
        ))

    checks.append(check_continuity(
        sorted({str(r["date"]) for r in direct_rows}),
        expected_last=latest_direct or date_to,
    ))
    # Глубина воронки: зрелые лиды (60 дней — 95 % оплат когорты уже видно)
    # при живых сделках обязаны нести оплаты. Ловит обнуление is_paid синком.
    checks.append(check_funnel_depth(
        lead_rows,
        mature_before=(date.today() - timedelta(days=60)).isoformat(),
    ))
    agent_db.insert_guard_checks(checks)

    if verdict(checks) == "RED":
        print(json.dumps({"verdict": "RED", "checks": checks}, ensure_ascii=False, indent=2))
        return 1

    # 2. Факты.
    facts = assemble_facts(direct_rows, lead_rows, score_rows)
    agent_db.upsert_facts(facts)

    # Направление кампании — для иерархии пулинга (Э2.2) и лестницы (Э2.1).
    direction_by_campaign: Dict[str, str] = {}
    for f in facts:
        if f.get("direction"):
            direction_by_campaign[str(f["campaign_id"])] = f["direction"]

    # 3. Агрегаты последних 30 дней — для заповедника и отчёта мощности.
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    recent = [f for f in facts if f["fact_date"] >= cutoff]
    aggregates: Dict[str, Dict[str, Any]] = {}
    for f in recent:
        agg = aggregates.setdefault(f["campaign_id"], {
            "campaign_id": f["campaign_id"],
            "direction": f.get("direction"),
            "cost_30d": 0.0, "leads_30d": 0, "eff_leads_30d": 0, "sum_p_pay_30d": 0.0,
        })
        agg["cost_30d"] += f["cost"]
        agg["leads_30d"] += f["leads"]
        agg["eff_leads_30d"] += f["eff_leads"]
        agg["sum_p_pay_30d"] += f["sum_p_pay"]

    # 4. Заповедник. Состав держится весь сезон — пересборка только по явному флагу.
    if "--rebuild-holdout" in sys.argv:
        agent_db.clear_holdout()
    holdout = select_holdout(list(aggregates.values()))
    agent_db.upsert_holdout(holdout, included_on=today_iso)

    # 5. Квазиэксперименты → блокнот; тут же — их чтение (Э2.4): сигнал
    # насыщения по кампаниям и направлениям, вход кривых Э3.1.
    # Плацебо-разброс — один проход на прогон: его считают оба потребителя
    # (квазиэксперименты и пары недель), а проход дорогой.
    placebo_floor = placebo_sigma(facts) or 0.0
    quasi = mine_quasi_experiments(facts, error_floor=placebo_floor)
    agent_db.upsert_experiments(quasi)
    history = budget_response(
        quasi, {str(f["campaign_id"]): f.get("direction") for f in facts})

    # Настройки кабинета читаются ОДИН раз на прогон: их спрашивают недобор
    # трафика, целевой CPA, портфель и слепая зона, а витрина за прогон не
    # меняется — четыре одинаковых запроса были бы разной правдой только по
    # случайности.
    campaign_settings = agent_db.load_campaign_settings_raw()

    # Э7.6: недобор трафика — сколько показов кампания не покупает на своей
    # ставке. Окно ровно то же, что у текущей точки кривых (RECENT_DAYS
    # зрелых дней до границы CRM): иначе признак «есть куда расти» и вердикт
    # насыщения говорили бы о разных неделях. Размещение — из витрины
    # настроек: объём трафика осмыслен только на поиске, у сетевых он
    # приходит константой 100 (замер, docs/AGENT-DATA-SOURCES.md).
    mature_through = latest_lead or date_to
    headroom_from = (date.fromisoformat(mature_through)
                     - timedelta(days=RECENT_DAYS - 1)).isoformat()
    headroom_section = traffic_headroom(
        facts, headroom_from, mature_through, placement_modes(campaign_settings))
    headroom_rows_written = 0
    for campaign_id, rows in headroom_computed_rows(headroom_section).items():
        agent_db.upsert_computed_settings(
            rows, calc_date=today_iso, object_id=campaign_id,
            object_level="campaign")
        headroom_rows_written += len(rows)

    # Э3.1: кривые насыщения — эластичность из квазиэкспериментов плюс пары
    # соседних недель (оба DiD — сезон вычтен), из неё β и предельная цена
    # эфф. лида на текущем объёме. Граница зрелости — CRM: эффективные лиды
    # свежих дней ещё едут, и хвостовые недели занизили бы лиды всем окнам.
    saturation = saturation_curves(
        facts, quasi, direction_by_campaign, mature_through,
        error_floor=placebo_floor, headroom_by_campaign=headroom_section)
    saturation_count = 0
    for campaign_id, rows in saturation_computed_rows(saturation).items():
        agent_db.upsert_computed_settings(
            rows, calc_date=today_iso, object_id=campaign_id,
            object_level="campaign")
        saturation_count += len(rows)

    slice_from = (date.today() - timedelta(days=SLICE_WINDOW_DAYS)).isoformat()
    queries_from = (date.today() - timedelta(days=QUERY_WINDOW_DAYS)).isoformat()
    clients = [] if skip_direct else _direct_clients()

    # 6-7. Отчёты Директа. ПАРАЛЛЕЛЬНО: каждый отчёт формируется до 10 минут, а их
    # десятки — последовательный обход делал прогон многочасовым (run 31781846178
    # висел 25+ минут и был отменён). Воркеров немного: Директ ограничивает число
    # одновременно формируемых отчётов на кабинет.
    jobs: List[Dict[str, Any]] = []
    # Те же цели нужны шагу 9: почасовой профиль обязан считаться по лидам, а не
    # по «любой цели» счётчика (там 70 % — прокрутка и время на сайте).
    lead_goal_ids: set = set()
    # Цели решаются ОДИН РАЗ на кабинет и дальше берутся отсюда. Отчёт запросов
    # читал сырое client["goal_ids"] — поле секрета, которое у кабинетов EDU
    # пустое, — и уходил в Директ без Goals. Без Goals в ответе нет ни одной
    # колонки Conversions, поэтому у КАЖДОГО запроса стояло ноль конверсий, и
    # правило «дорого и без конверсий» объявляло мусором рабочее ядро:
    # на прогоне 32580972099 в кандидаты попали «колледжи москвы», «мти»,
    # «мед колледж» — 31 запрос на 271 975 ₽.
    goals_by_login: Dict[str, List[str]] = {}
    for client in clients:
        login, goals = client["login"], resolve_goal_ids(client)
        goals_by_login[login] = goals
        lead_goal_ids.update(int(g) for g in goals if str(g).isdigit())
        # Расписания здесь нет: HourOfDay отвергается Reports API (probe 31781715471),
        # почасовой профиль приходит из Метрики (шаг 9).
        #
        # Срезы запрашиваются независимо от объёма оплат: конверсионность сегмента
        # считается по Conversions самого отчёта Директа. Прежний гейт по оплатам
        # аккаунта остался от расчёта, который сегменты не различал.
        for kind in ("device", "gender", "age"):
            jobs.append({"purpose": "computed", "login": login, "goals": goals,
                         "kind": kind, "date_from": cutoff, "by_campaign": False})
        for kind in ("region", "network", "device"):
            jobs.append({"purpose": "sliced", "login": login, "goals": goals,
                         "kind": kind, "date_from": slice_from, "by_campaign": True})

    def _run_job(job: Dict[str, Any]) -> Dict[str, Any]:
        rows, goal = fetch_segment_report(
            job["login"], job["kind"], job["date_from"], date_to,
            by_campaign=job["by_campaign"], goals=job["goals"],
        )
        return {**job, "rows": rows, "goal": goal}

    # Вычисленные настройки копятся ПО КАБИНЕТАМ: числа посчитаны по аудитории
    # конкретного кабинета и записываются под его логином (object_id). Общий
    # идентификатор на всех схлопывал четыре набора в один — ключ таблицы
    # совпадал, и в базе оставались числа последнего успевшего кабинета.
    computed_by_account: Dict[str, List[Dict[str, Any]]] = {}
    # Срезы, по которым корректировки НЕ посчитаны, и почему. Молчаливый пропуск
    # неотличим от «данных нет» — а именно так и выглядел дефект размазывания.
    computed_skipped: List[Dict[str, Any]] = []
    sliced_rows: List[Dict[str, Any]] = []
    # Э2.2: покампанийные device-корректировки по кабинетам (и отказы с причиной).
    campaign_modifiers_by_login: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    campaign_modifiers_skipped: List[Dict[str, Any]] = []
    # По ОДНОЙ цели считается конверсионность каждого среза, а значит и все
    # корректировки ставок. Цель выбирается автоматически — самая массовая из
    # переданных, — а имён целей Директ в отчёте не отдаёт, только
    # идентификаторы. Единственная защита от «корректировки посчитаны по
    # прокрутке страницы» — видеть выбор в отчёте прогона и сверять глазами.
    segment_goals: List[Dict[str, Any]] = []
    if jobs:
        with ThreadPoolExecutor(max_workers=REPORT_WORKERS) as pool:
            for done in as_completed([pool.submit(_run_job, j) for j in jobs]):
                job = done.result()
                segment_goals.append({"account": job["login"], "slice": job["kind"],
                                      "purpose": job["purpose"], **job["goal"]})
                if job["purpose"] == "computed":
                    login, rows, reason = computed_rows_for_job(job)
                    if reason:
                        computed_skipped.append(
                            {"account": login, "slice": job["kind"], "reason": reason})
                    if rows:
                        computed_by_account.setdefault(login, []).extend(rows)
                else:
                    weekly = collapse_tail(build_sliced_facts(job["rows"], job["kind"]))
                    sliced_rows += weekly
                    # Э2.2: покампанийные корректировки — только device, это
                    # единственный срез с рабочим рычагом записи. Region мёртв
                    # (RegionId), network корректировкой ставки не является.
                    if job["kind"] == "device":
                        camp_rows, camp_reason = hierarchical_modifiers(
                            weekly, direction_by_campaign, "device")
                        if camp_reason:
                            campaign_modifiers_skipped.append(
                                {"account": job["login"], "reason": camp_reason})
                        else:
                            campaign_modifiers_by_login[job["login"]] = camp_rows

    # Э2.2b: качество лида по устройствам. Конверсия клик→лид сегмента — только
    # половина правды: замер по мосту (прогон 32622086445) показал, что
    # планшетный лид приносит 792 ₽ против 1625 ₽ у ПК и соединяется на
    # 8–15 п.п. хуже. Поэтому device-корректировки (и кабинетные, и
    # покампанийные Э2.2) домножаются на отношение ожидаемой выручки на лид
    # сегмента к базе моста. Окно моста — зрелое: свежие лиды ещё не доехали
    # до глубоких ступеней и занижали бы качество всем сегментам разом.
    quality_adjusted = 0
    bridge_maturity = _lag_percentile(lead_rows, LADDER_MATURITY_PERCENTILE,
                                      today=date.today())
    bridge_to = (date.today() - timedelta(days=bridge_maturity)).isoformat()
    bridge_from = (date.today() - timedelta(days=BRIDGE_LOOKBACK_DAYS)).isoformat()
    bridge_rows = agent_db.load_device_bridge(bridge_from, bridge_to)
    device_quality, quality_reason = device_quality_ratios(bridge_rows)
    if device_quality:
        for rows in computed_by_account.values():
            quality_adjusted += apply_quality_to_modifiers(rows, device_quality)
        for by_camp in campaign_modifiers_by_login.values():
            for rows in by_camp.values():
                quality_adjusted += apply_quality_to_modifiers(rows, device_quality)

    if computed_skipped:
        agent_db.insert_guard_checks([
            {"check_name": f"computed:{sk['account']}:{sk['slice']}", "status": "SKIP",
             "detail": {"reason": sk["reason"]}}
            for sk in computed_skipped
        ])

    computed_count = 0
    for login, rows in computed_by_account.items():
        agent_db.upsert_computed_settings(rows, calc_date=today_iso, object_id=login)
        computed_count += len(rows)
    agent_db.upsert_sliced_facts(sliced_rows)

    # Э2.2: покампанийные строки — под object_level='campaign'. Движок записи их
    # пока НЕ читает (применяется кабинетный уровень): сначала видимость и сверка
    # на бою, потом переключение применения — и только оно снимает max_campaigns=1.
    campaign_modifier_count = 0
    campaign_modifier_summary: Dict[str, Any] = {}
    for login, by_camp in campaign_modifiers_by_login.items():
        account_values = {
            (r["setting_kind"], r["setting_key"]): r["value"]
            for r in computed_by_account.get(login, [])}
        deltas: List[Dict[str, Any]] = []
        for campaign_id, rows in by_camp.items():
            agent_db.upsert_computed_settings(
                rows, calc_date=today_iso,
                object_id=campaign_id, object_level="campaign")
            campaign_modifier_count += len(rows)
            for r in rows:
                account_value = account_values.get(
                    (r["setting_kind"], r["setting_key"]))
                if account_value is not None:
                    deltas.append({
                        "campaign_id": campaign_id, "segment": r["setting_key"],
                        "campaign": r["value"], "account": account_value,
                        "delta": round(r["value"] - account_value, 1),
                    })
        deltas.sort(key=lambda d: -abs(d["delta"]))
        campaign_modifier_summary[login] = {
            "campaigns": len(by_camp),
            "rows": sum(len(v) for v in by_camp.values()),
            # Насколько личные значения расходятся с кабинетным, которое сейчас
            # раскатывается на всех: величина этого разрыва и есть цена Э2.2.
            "top_deltas_vs_account": deltas[:5],
        }

    # 8. Структура кабинета и поисковые запросы. Только по живым кампаниям окна.
    if "--rebuild-bulk" in sys.argv:
        agent_db.clear_bulk_tables()
    live_campaigns = {str(f["campaign_id"]) for f in facts
                      if f["fact_date"] >= slice_from and (f["cost"] > 0 or f["leads"] > 0)}
    object_rows: List[Dict[str, Any]] = []
    # Запросы копятся ПО КАБИНЕТАМ: пригодность отчёта к расчёту минус-слов
    # решается на уровне кабинета, а после слияния в общий список принадлежность
    # строки кабинету восстановить уже нечем.
    queries_by_login: Dict[str, List[Dict[str, Any]]] = {}
    # Та же цена ошибки, что и у сегментов: по этой цели отбираются кандидаты
    # в минус-слова.
    query_goals: List[Dict[str, Any]] = []
    login_by_campaign_id: Dict[str, str] = {}
    for client in clients:
        login = client["login"]
        goals = goals_by_login.get(login, [])
        account_campaigns = fetch_campaign_ids(login)
        # Архивные кампании здесь нужны: справочник «кампания → кабинет»
        # тем точнее, чем шире, а Метрика показывает и то, что уже выключено.
        for cid in account_campaigns:
            login_by_campaign_id[str(cid)] = login
        # Только ЖИВЫЕ кампании окна, а не весь кабинет за всю историю:
        # fetch_campaign_ids отдавал все 163+ кампании включая архивные, и снимок
        # структуры раздувался до 367k строк / 378 МБ (прогон 31785888375).
        campaign_ids = [cid for cid in account_campaigns if str(cid) in live_campaigns]
        for level in ("adgroup", "keyword", "ad"):
            object_rows += build_object_rows(
                fetch_objects(login, level, campaign_ids), level, seen_on=today_iso)
        rows_for_login, query_goal = fetch_search_queries(
            login, queries_from, date_to, goals=goals)
        queries_by_login[login] = rows_for_login
        query_goals.append({"account": login, **query_goal})
    agent_db.upsert_objects(object_rows)
    query_rows = top_queries_by_cost(
        [q for rows in queries_by_login.values() for q in rows])
    agent_db.upsert_search_queries(query_rows)

    # Запросы, сжигающие втрое больше целевого CPA без единой конверсии.
    # Расчёт существовал, но НЕ ВЫЗЫВАЛСЯ ни разу: кандидаты в минус-слова не
    # считались вообще, хотя данные для них собирались каждый прогон. Пока это
    # только счёт и отчёт — минусовать кабинет Э1a не умеет, и молчаливо
    # применять такое нельзя. Но «сколько денег уходит в запросы без
    # конверсий» обязано быть видно.
    # Порог — МЕДИАННЫЙ базовый CPA кабинета: среднее тянут вверх единичные
    # дорогие кампании, и от него порог «втрое дороже» перестал бы что-либо
    # значить. Нет базы — нет и порога: считать не от чего, и выдумывать
    # константу здесь нельзя.
    baselines = sorted(agent_db.load_baseline_cpa(date_from, date_to).values())
    cpa_limit = baselines[len(baselines) // 2] if baselines else 0.0
    # Кабинет, чей отчёт запросов не отдал ни одной колонки конверсий, из
    # расчёта исключается целиком. Ноль конверсий у каждого запроса такого
    # отчёта не значит «запросы бесполезны» — он значит «конверсии не
    # спрошены», а правило «дорого и без конверсий» на таких данных выносит
    # приговор всему кабинету. Причина обязана быть в отчёте: молчаливый
    # пропуск неотличим от «кандидатов не нашлось».
    blind_accounts = sorted(g["account"] for g in query_goals if not g["goal_column"])
    seeing_queries = [q for login, rows in queries_by_login.items()
                      if login not in set(blind_accounts) for q in rows]
    scored_queries = top_queries_by_cost(seeing_queries)
    # Базовая конверсия — по ВСЕМУ набору запросов кабинета, а не по топу по
    # расходу: топ смещён в сторону дорогих фраз, и порог «сколько кликов
    # нужно, чтобы ноль конверсий что-то значил» поехал бы вместе с ним.
    base_clicks = sum(int(q.get("clicks") or 0) for q in seeing_queries)
    base_conversions = sum(int(q.get("conversions") or 0) for q in seeing_queries)
    base_conversion = (base_conversions / base_clicks) if base_clicks else 0.0
    minus_candidates = (minus_word_candidates(scored_queries, cpa_limit=cpa_limit,
                                              base_conversion=base_conversion)
                        if cpa_limit > 0 else [])
    # Минус-фраза гасит не строку отчёта, а СЕМЕЙСТВО запросов, содержащих все
    # её слова (справка Директа). Судить кандидата по одной его строке — та же
    # ошибка, что уже исправлена для минус-СЛОВ: 25.08 в кандидаты попал
    # «университет синергия» — собственный бренд, — и та же фраза погасила бы
    # «университет синергия красноярск», который расширение в этом же отчёте
    # предлагало докупить. Семейство считается по ВСЕМ запросам кабинета, а не
    # по топу scored_queries: окупающийся хвост живёт как раз вне топа.
    minus_dropped: List[Dict[str, Any]] = []
    if minus_candidates:
        minus_candidates, minus_dropped = phrases_cutting_only_waste(
            minus_candidates, seeing_queries, cpa_limit=cpa_limit)
    # Минус-СЛОВА поверх минус-фраз: отдельная фраза почти никогда не набирает
    # объём для приговора (при базовой конверсии в проценты сотня кликов без
    # конверсий — редкость), а слово, общее для полусотни фраз, набирает и
    # гасит всё семейство разом. Считаются по ВСЕМ запросам кабинета, не по
    # топу: слово живёт как раз в хвосте, который топ отсекает.
    # Слова НАШЕЙ семантики (из ключевых фраз кабинета) минусации не подлежат:
    # запрет отменил бы собственную закупку. Дорогая своя семантика лечится
    # целевым CPA (Э3.5).
    protected_words = core_words(seeing_queries)
    word_candidates = (word_minus_candidates(seeing_queries, cpa_limit=cpa_limit,
                                             base_conversion=base_conversion,
                                             protected_words=protected_words)
                       if cpa_limit > 0 else [])

    # Расширение семантики — зеркало минусации: запросы, которые уже
    # окупаются, но своей ключевой фразы не имеют. Единственный генератор
    # гипотез, которому не нужны ни модель, ни рынок: доказательство лежит в
    # собственном журнале. Рычага записи у него пока нет — сначала список
    # смотрит человек.
    expansion = (expansion_candidates(seeing_queries, cpa_limit=cpa_limit)
                 if cpa_limit > 0 else [])

    # Смысловой слой поверх экономики: модель ВЕТИРУЕТ кандидатов, но своих
    # не добавляет (sync/agent/semantic.py). Спрашиваем только про уже
    # отобранных кандидатов — это десятки фраз, а не 237 тысяч строк отчёта.
    # Нет ключа — вердиктов нет, и рычаги работают ровно как раньше.
    ask = semantic.deepseek_asker()
    semantic_verdicts = {}
    if ask is not None:
        # Паспорт продукта — по направлению кампаний, где фраза откручивалась
        # (задача 20). Фраза, стоящая в кампаниях РАЗНЫХ направлений, едет на
        # общем описании: паспорта соседей противоречат друг другу там, где
        # это опаснее всего («после 9 класса» — анти-маркер у ВПО и целевой
        # маркер у СПО).
        semantic_verdicts, passport_stats = semantic.classify_by_direction(
            minus_candidates + word_candidates + expansion,
            ask=ask, direction_by_campaign=direction_by_campaign,
            context=SEMANTIC_CONTEXT)
        before_minus = len(minus_candidates) + len(word_candidates)
        minus_candidates = semantic.keep_minus_candidates(
            minus_candidates, semantic_verdicts)
        word_candidates = semantic.keep_minus_candidates(
            word_candidates, semantic_verdicts)
        expansion = semantic.keep_expansion_candidates(expansion, semantic_verdicts)
        semantic_stats = {
            "asked": len(semantic_verdicts),
            "vetoed_minus": before_minus - len(minus_candidates) - len(word_candidates),
            "core": sum(1 for v in semantic_verdicts.values()
                        if v["verdict"] == semantic.CORE),
            "junk": sum(1 for v in semantic_verdicts.values()
                        if v["verdict"] == semantic.JUNK),
            "unclear": sum(1 for v in semantic_verdicts.values()
                           if v["verdict"] == semantic.UNCLEAR),
            # Само по себе число unclear ничего не говорит: «модель честно
            # не знает» и «слой упал» дают одинаковую цифру и одинаковое
            # отсутствие вето. Разводит их только причина.
            "unclear_reasons": semantic.unclear_reasons(semantic_verdicts),
            # Разметка с паспортом и без — разного качества. Без этих долей
            # «паспорта не понадобились» неотличимо от «паспорта не завезли».
            "passports": passport_stats,
        }
    else:
        # Молчаливое отсутствие слоя неотличимо от «модель всё одобрила» —
        # поэтому причина стоит в отчёте явной строкой.
        semantic_stats = {"asked": 0, "reason": "DEEPSEEK_API_KEY не задан"}

    # Кандидаты в минус-фразы уезжают в computed по кампаниям: применяет их
    # писатель Э3.6 в прогоне применения, и отчёт Э0 сам по себе действий не
    # производит. Фраза попадает в строки каждой кампании, где жгла деньги.
    negative_rows_written = 0
    for campaign_id, rows in negative_computed_rows(
            minus_candidates + word_candidates).items():
        agent_db.upsert_computed_settings(
            rows, calc_date=today_iso, object_id=campaign_id,
            object_level="campaign")
        negative_rows_written += len(rows)

    # Площадки сети — тем же правилом, что фразы, и тем же мостом в computed.
    # Отдельный отчёт: в сегментных срезах площадок нет.
    placement_rows_written = 0
    placements_seen = 0
    placement_errors: List[Dict[str, Any]] = []
    placement_candidate_rows: List[Dict[str, Any]] = []
    if cpa_limit > 0:
        for login in sorted(queries_by_login):
            if login in set(blind_accounts):
                continue
            try:
                rows, _ = fetch_placements(login, slice_from, date_to,
                                           goals=goals_by_login.get(login, []))
            except Exception as exc:                     # noqa: BLE001
                # Отчёт прогона — единственный JSON в выводе, и печатать в
                # него посторонние строки нельзя: сбой одного кабинета едет
                # полем отчёта, а не мусором в stdout.
                placement_errors.append({"account": login,
                                         "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            placements_seen += len(rows)
            placement_candidate_rows += rows
        base_clicks_p = sum(int(r.get("clicks") or 0) for r in placement_candidate_rows)
        base_conv_p = sum(int(r.get("conversions") or 0) for r in placement_candidate_rows)
        placement_candidates_list = placement_candidates(
            placement_candidate_rows, cpa_limit=cpa_limit,
            base_conversion=(base_conv_p / base_clicks_p) if base_clicks_p else 0.0)
        for campaign_id, rows in placement_computed_rows(placement_candidates_list).items():
            agent_db.upsert_computed_settings(
                rows, calc_date=today_iso, object_id=campaign_id,
                object_level="campaign")
            placement_rows_written += len(rows)
    else:
        placement_candidates_list = []

    # 9. Снимок настроек.
    snapshot_rows = build_snapshot_rows(campaign_settings, seen_on=today_iso)
    agent_db.upsert_settings_snapshot(snapshot_rows)

    # 10. Профиль успеха и дистанции (после снимка структуры — признаки берутся оттуда).
    feature_rows = agent_db.load_campaign_features(date_from, date_to)
    profile = build_profile(feature_rows, PROFILE_FEATURES)
    profile_rows: List[Dict[str, Any]] = []
    if profile:
        qualities = sorted(
            (campaign_quality(r) if campaign_quality(r) is not None else float("inf"), r["campaign_id"])
            for r in feature_rows
        )
        quartile_by_campaign = {
            cid: min(int(i * 4 / max(len(qualities), 1)) + 1, 4)
            for i, (_, cid) in enumerate(qualities)
        }
        for row in feature_rows:
            distance, gaps = distance_to_profile(row, profile, PROFILE_FEATURES)
            profile_rows.append({
                "campaign_id": row["campaign_id"],
                "distance": distance,
                "gaps": gaps,
                "quartile": quartile_by_campaign.get(row["campaign_id"], 4),
            })
    agent_db.upsert_profile(profile_rows, calc_date=today_iso)

    # 9. Обогащение Метрикой: почасовой профиль (Директ HourOfDay не отдаёт) и
    # поведение по кампаниям — ранний сигнал качества до созревания оплат.
    #
    # Метрика отдаёт имя кампании, а не Id (probe 31788247020) — резолвим по
    # фактам. Справочник нужен здесь дважды: привязать счётчик к кабинету и
    # привязать поведение к кампании.
    id_by_name = {str(f.get("campaign_name") or "").strip(): f["campaign_id"]
                  for f in facts if f.get("campaign_name")}
    login_by_campaign_name = {
        name: login_by_campaign_id[str(cid)]
        for name, cid in id_by_name.items() if str(cid) in login_by_campaign_id
    }
    hourly_rows: List[Dict[str, Any]] = []
    hourly_skipped: List[Dict[str, Any]] = []
    hourly_numerator: List[Dict[str, Any]] = []
    behavior_rows: List[Dict[str, Any]] = []
    profile_by_counter: Dict[int, List[Dict[str, Any]]] = {}
    names_by_counter: Dict[int, set] = {}
    if not skip_direct and os.environ.get("YM_TOKEN"):
        for counter in EDU_COUNTERS:
            try:
                # Цель принадлежит ОДНОМУ счётчику: чужой идентификатор в metrics
                # Метрика отвергает целиком, обнуляя профиль всего счётчика.
                # Поэтому спрашиваем только пересечение целей Директа с целями
                # этого счётчика; пусто — считать нечего, и это не отказ.
                counter_goals = sorted(lead_goal_ids & set(fetch_counter_goal_ids(counter)))
                if counter_goals:
                    rows, chosen = fetch_hourly_profile(counter, cutoff, date_to,
                                                        counter_goals)
                    hourly_rows += rows
                    profile_by_counter[counter] = rows
                    # Чем именно считали — в отчёт. «Самая массовая колонка»
                    # без имени рядом уже приводила сюда микроцель прокрутки.
                    hourly_numerator.append({"counter": counter, **chosen})
                else:
                    hourly_skipped.append(
                        {"counter": counter, "reason": "целей Директа нет на счётчике"})
                counter_behavior = fetch_campaign_behavior(counter, slice_from, date_to)
                behavior_rows += counter_behavior
                names_by_counter[counter] = {r["campaign_name"] for r in counter_behavior}
            except Exception as exc:  # счётчик может быть недоступен токену
                agent_db.insert_guard_checks([{
                    "check_name": f"metrika:{counter}", "status": "FAIL",
                    "detail": {"error": f"{type(exc).__name__}: {exc}"[:300]},
                }])

    # Расписание считается ПО СЧЁТЧИКУ и пишется кабинету, которому счётчик
    # принадлежит. Раньше профили трёх счётчиков складывались в один и
    # раскатывались на все кабинеты — а счётчики о сутках не согласны: проба
    # 32579085232 дала у 98627983 часы 02-05 на уровне 130, у 96526110 те же
    # часы на уровне 90. Кампания ведёт на ОДИН сайт, и профиль чужого сайта
    # для неё — та же ошибка «величина посчитана по чужой популяции», что уже
    # чинилась в этом файле дважды.
    for counter, rows in profile_by_counter.items():
        login = resolve_counter_account(names_by_counter.get(counter) or set(),
                                        login_by_campaign_name)
        if not login:
            # Привязки нет — расписание применять некуда. Записать его «всем»
            # значило бы вернуть исходный дефект под другим именем.
            hourly_skipped.append(
                {"counter": counter, "reason": "счётчик не привязан к кабинету"})
            continue
        # База считается внутри compute_schedule по тем же строкам: внешняя база
        # в чужих единицах — та самая ошибка, что вырождала сегментные корректировки.
        schedule_rows, schedule_reason = compute_schedule(rows)
        if schedule_reason:
            computed_skipped.append(
                {"account": login, "slice": "hour", "reason": schedule_reason})
            agent_db.insert_guard_checks([{
                "check_name": f"computed:{login}:hour", "status": "SKIP",
                "detail": {"reason": schedule_reason, "counter": counter},
            }])
        if schedule_rows:
            agent_db.upsert_computed_settings(
                schedule_rows, calc_date=today_iso, object_id=login)
            computed_count += len(schedule_rows)

    resolved_behavior = []
    unresolved = 0
    for row in behavior_rows:
        campaign_id = id_by_name.get(row["campaign_name"])
        if not campaign_id:
            unresolved += 1
            continue
        resolved_behavior.append({k: v for k, v in row.items() if k != "campaign_name"}
                                 | {"campaign_id": campaign_id})
    if unresolved:
        agent_db.insert_guard_checks([{
            "check_name": "metrika:name_resolution", "status": "OK",
            "detail": {"unresolved_campaigns": unresolved, "resolved": len(resolved_behavior)},
        }])
    agent_db.upsert_behavior(resolved_behavior, window_from=slice_from, window_to=date_to)

    # 11. Отчёт мощности, лестница воронки и фактический объём таблиц.
    report = power_report(list(aggregates.values()))
    ladder_section = funnel_ladder_section(facts, lead_rows)

    # Э3.2: единый порог предельной окупаемости и целевые бюджеты — после
    # лестницы (оттуда ценность лида) и после справочника «кампания →
    # кабинет» (сумма переноса сходится на уровне кабинета). Расчётный слой:
    # запись бюджетов в Директ — Э3.3.
    # Э3.5: экономически допустимая цель CPA. Окно то же зрелое, что у
    # лестницы: выручка приписана дате создания лида. Настройки — из витрины
    # кабинета (edu_campaign_settings), кампании на стратегиях без цели CPA
    # рычагом не управляются и в расчёт не входят.
    tcpa_section = tcpa_targets(build_tcpa_inputs(
        facts, saturation["campaigns"], campaign_settings,
        ladder_section["window_from"], ladder_section["window_to"]))
    tcpa_count = 0
    for campaign_id, rows in tcpa_computed_rows(tcpa_section).items():
        agent_db.upsert_computed_settings(
            rows, calc_date=today_iso, object_id=campaign_id,
            object_level="campaign")
        tcpa_count += len(rows)

    def _solve_portfolio(**growth_args):
        return portfolio_targets(
            saturation["campaigns"], ladder_section["by_object"],
            login_by_campaign_id,
            # Заповедник вне солвера: агент его не двигает, и держать его
            # внутри ограничения «сумма целевых = сумме текущих» значит
            # задавать порог λ отчасти неподвижными кампаниями.
            holdout_ids={str(h["campaign_id"]) for h in holdout},
            # Доля разведки — из панели настроек, а не из константы модуля.
            explore_share=active_config["explore_share"],
            # Пороги уверенности — оттуда же. До 25.08.2026 панель объявляла
            # p_sign_bid/p_sign_budget/p_sign_state, но не передавала их
            # никуда: решали константы confidence.ACTION_CLASSES, и ручка на
            # панели ничего не двигала.
            thresholds=confidence_thresholds,
            # Настройки кабинета — только ради признака «лимит связывает
            # расход»: разведочная надбавка это деньги, и множитель недобора
            # трафика применяется там, где деньги действительно доедут (Э3.3).
            settings_by_campaign=campaign_settings, **growth_args)

    # Первая раскладка — при бюджете, равном факту окна. Она нужна не ради
    # чисел, а ради ЗАПАСА: сколько рублей кабинет освоит сегодня поднятием
    # лимитов, видно только после того, как солвер назвал цели (growth.py).
    # Итоговой станет вторая раскладка, ниже.
    preliminary_threshold = _solve_portfolio()
    # Слепая доля расхода: сколько денег прошло мимо настроек, которые агент
    # читает (Мастер кампаний и прочее вне API). Считается ДВАЖДЫ, и это не
    # дубль: замер 25.08.2026 дал 30,3 % на окне лестницы против 14,4 % на
    # окне решений, и вся разница — кампании, которые давно не тратят.
    # Окно лестницы (90 зрелых дней) относит долю к тем же числам, из которых
    # выведены кривые. Окно решений (последние RECENT_DAYS зрелых дней) —
    # к деньгам, которые агент двигает сегодня; архив в нём не участвует, и
    # завышенная им доля заставляла бы не доверять живым числам.
    decision_from = (date.fromisoformat(ladder_section["window_to"])
                     - timedelta(days=RECENT_DAYS - 1)).isoformat()
    blind = {
        "decision_window": blind_spend(facts, campaign_settings,
                                       decision_from, ladder_section["window_to"]),
        "ladder_window": blind_spend(facts, campaign_settings,
                                     ladder_section["window_from"],
                                     ladder_section["window_to"]),
    }

    # Петля обучения на СВОИХ действиях: послужной список рычагов и смещение
    # прогноза по журналу применённых изменений. Недоступность журнала расчёт
    # не роняет — это отчётный слой поверх него, ровно как настройки выше;
    # причина при этом видна в отчёте, а не молчит.
    try:
        closed = writer_db.closed_actions()
        learning = {"closed_actions": len(closed),
                    "track_record": track_record(closed),
                    # Ключ — вид действия ПЛЮС направление: модель может
                    # завышать эффект доливки и занижать эффект срезания, и
                    # одно усреднённое число спрятало бы обе ошибки.
                    "forecast_bias": forecast_bias(closed)}
    except Exception as exc:  # noqa: BLE001
        learning = {"unavailable": f"{type(exc).__name__}: {exc}"[:200]}

    # Э7.8: спрос рынка как календарь направлений. Окно — 26 недель: базовое
    # окно режима 8 недель плюс запас на дыры выгрузки Wordstat.
    demand = demand_regime(
        agent_db.load_wordstat_demand(
            (date.today() - timedelta(weeks=DEMAND_HISTORY_WEEKS)).isoformat()),
        through_week=date_to)

    # Ранний тормоз доливки: качество когорты лидов. Два СМЕЖНЫХ окна по
    # QUALITY_WINDOW_DAYS, кончающиеся на границе зрелости CRM, — свежая
    # когорта против предыдущей. Кончать окно сегодняшним днём нельзя: лиды
    # приезжают с отставанием 2–4 дня, и хвост окна был бы недосчитан ровно
    # там, где ищется падение.
    quality_end = date.fromisoformat(latest_lead or date_to)
    after_from = quality_end - timedelta(days=QUALITY_WINDOW_DAYS - 1)
    before_to = after_from - timedelta(days=1)
    before_from = before_to - timedelta(days=QUALITY_WINDOW_DAYS - 1)
    quality = lead_quality_section(
        agent_db.load_quality_facts(before_from.isoformat(), quality_end.isoformat()),
        before_from.isoformat(), before_to.isoformat(),
        after_from.isoformat(), quality_end.isoformat())

    # Предварительный список усиления — ради ОДНОГО числа: сколько кабинет
    # освоит сегодня поднятием лимитов. Раньше его не посчитать, оно выводится
    # из целей солвера.
    preliminary_growth = growth_candidates(preliminary_threshold, headroom_section,
                                           demand, expansion,
                                           quality_drift=quality["drift"])

    # Общая сумма кабинета перестала быть константой: при предельной
    # окупаемости выше цели с запасом агент растит её шагом до 20 % за такт,
    # в пределах месячного потолка из панели настроек. Запас считается ПО
    # КАБИНЕТАМ: счёт один на кабинет, и запас соседнего в рост не
    # складывается. Рычаг цены (LEVER_TCPA) сюда не входит — эти рубли
    # кабинет сегодня физически не выберет.
    room_by_login: Dict[str, float] = {}
    for candidate in preliminary_growth["candidates"]:
        if candidate["lever"] != LEVER_BUDGET or not candidate["campaign_id"]:
            continue
        login = login_by_campaign_id.get(candidate["campaign_id"]) or "unmapped"
        room_by_login[login] = (room_by_login.get(login, 0.0)
                                + float(candidate["room_rub"] or 0.0))

    budget_threshold = _solve_portfolio(
        target_romi=active_config["target_romi"],
        room_rub_by_login=room_by_login,
        # Потолок месячного освоения — деньги владельца. Ключ пуст: рост
        # только предлагается числом в отчёте, сумма кабинета не меняется.
        monthly_cap_rub=active_config["monthly_budget_cap_rub"],
        # Замыкание петли: история собственных промахов поправляет ОЖИДАНИЕ
        # такта, не цели. Журнал недоступен — поправки нет, расчёт идёт на
        # сырой модели, как до петли. Предварительная раскладка идёт без неё
        # намеренно: она считает запас по целям, а цели поправка не трогает.
        forecast_bias=learning.get("forecast_bias"))
    budget_target_count = 0
    for campaign_id, rows in portfolio_computed_rows(budget_threshold).items():
        agent_db.upsert_computed_settings(
            rows, calc_date=today_iso, object_id=campaign_id,
            object_level="campaign")
        budget_target_count += len(rows)

    # Вторая половина оптимизации: что УСИЛИТЬ. Собирается из уже посчитанного
    # — недобор трафика, упор в кап шага, режим спроса, запросы без своей
    # группы — и ничего нового в Директ не спрашивает. Карта дрейфа качества
    # едет сюда тормозом: кандидат с портящейся когортой в список не попадает.
    # Считается по ИТОГОВОЙ раскладке: предварительная знала бюджет-факт, и
    # печатать её значило бы отчитываться числами, которых писатель не увидит.
    growth = growth_candidates(budget_threshold, headroom_section, demand,
                               expansion, quality_drift=quality["drift"])

    # Ф13: генераторы идей. Считаются ПОСЛЕ итоговой раскладки — им нужен
    # порог λ, ценность лида и признак связывающего лимита, то есть те же
    # числа, по которым такт двигает деньги. Реестр — единственный выход
    # генераторов наружу: применять они ничего не умеют, и всё, что найдено,
    # уезжает человеку на экран и в очередь такта записи.
    #
    # Журнал сбросов обучения читается тем же способом, что и петля обучения
    # выше: недоступность журнала расчёт не роняет, но и молча пустым не
    # притворяется — генератор тестов обязан отличать «сбросов не было» от
    # «журнал не прочитан», иначе срок теста поедет на две недели.
    try:
        learning_reset = writer_db.last_learning_reset()
        learning_reset_error = None
    except Exception as exc:  # noqa: BLE001
        learning_reset, learning_reset_error = {}, f"{type(exc).__name__}: {exc}"[:200]
    generated = collect_ideas(
        facts=facts, ladder_section=ladder_section,
        portfolio_section=budget_threshold, sliced_rows=sliced_rows,
        query_rows=scored_queries, expansion=expansion, demand=demand,
        settings_by_campaign=campaign_settings,
        login_by_campaign=login_by_campaign_id,
        direction_by_campaign=direction_by_campaign,
        holdout_ids=[str(h["campaign_id"]) for h in holdout],
        learning_reset=learning_reset, quality_drift=quality["drift"],
        config=active_config, slice_window_days=SLICE_WINDOW_DAYS,
        query_window_days=QUERY_WINDOW_DAYS)
    if learning_reset_error:
        generated["learning_reset_unavailable"] = learning_reset_error

    sizes = agent_db.table_sizes()
    total_mb = round(sum(int(s["size_bytes"] or 0) for s in sizes) / 1024 / 1024, 1)

    # Граница зрелости CRM и величина отставания — в отчёт каждого прогона.
    # Без них «лидов за последние дни нет» читается как обвал конверсии, а не
    # как «выгрузка ещё не приехала», и разбор уходит не туда.
    crm_lag = (date.today() - date.fromisoformat(latest_lead)).days if latest_lead else None

    report = {
        "verdict": "GREEN",
        "crm_through": latest_lead,
        "crm_lag_days": crm_lag,
        "facts_rows": len(facts),
        # Доля расхода, прошедшая мимо настроек агента, на двух окнах.
        # Печатается всегда, в том числе нулём: отсутствие строки неотличимо
        # от отсутствия слепой зоны, а решения принимаются по числам рядом с
        # ней. Смотреть на decision_window — по нему агент двигает деньги.
        "blind_spend": blind,
        "sliced_rows": len(sliced_rows),
        "objects": len(object_rows),
        "search_queries": len(query_rows),
        # Активный конфиг с источником каждого значения: «почему агент стал
        # резче» не должно требовать археологии по коммитам.
        "config": active_config_rows,
        "semantic": semantic_stats,
        "expansion_candidates": {
            "count": len(expansion),
            "conversions": sum(c["conversions"] for c in expansion),
            "cost": round(sum(c["cost"] for c in expansion), 2),
            "headroom": round(sum(c["headroom"] for c in expansion), 2),
            "sample": expansion[:10],
        },
        "minus_word_candidates": {
            "cpa_limit": round(cpa_limit, 2),
            "count": len(minus_candidates),
            "cost_burned": round(sum(float(q.get("cost") or 0.0)
                                     for q in minus_candidates), 2),
            # Разбивка по причине: «нет конверсий при достаточном объёме» и
            # «конверсии есть, но дороже допустимого» — разные решения, и
            # смешивать их в одном счётчике значит прятать половину.
            "by_reason": _count_by(minus_candidates, "reason"),
            # Порог наблюдаемости, выведенный из данных прогона: столько
            # кликов без конверсий нужно, чтобы приговор был осмысленным.
            "base_conversion": round(base_conversion, 5),
            "computed_rows": negative_rows_written,
            # Слова считаются и применяются тем же рычагом, что фразы, но
            # отчитываются отдельно: одно слово гасит десятки фраз, и мерить
            # их одним счётчиком значит прятать разницу в цене решения.
            "words": {
                "count": len(word_candidates),
                "cost_burned": round(sum(float(w.get("cost") or 0.0)
                                         for w in word_candidates), 2),
                "sample": [w.get("query") for w in word_candidates[:10]],
            },
            "sample": [q.get("query") for q in minus_candidates[:10]],
            # Что снято проверкой семейства и почему. Без этой строки отсев
            # неотличим от «кандидатов не нашлось», а именно здесь видно, что
            # рычаг едва не отрезал собственный бренд.
            "family_check": {
                "dropped": len(minus_dropped),
                "by_reason": _count_by(minus_dropped, "reason"),
                "cost_saved": round(sum(float(q.get("cost") or 0.0)
                                        for q in minus_dropped), 2),
                "sample": [{"query": q.get("query"), "reason": q.get("reason"),
                            "family": q.get("family")}
                           for q in minus_dropped[:10]],
            },
            "blind_accounts": blind_accounts,
        },
        "placements": {
            "rows_seen": placements_seen,
            "candidates": len(placement_candidates_list),
            "cost_burned": round(sum(float(c.get("cost") or 0.0)
                                     for c in placement_candidates_list), 2),
            "sample": [c.get("placement") for c in placement_candidates_list[:10]],
            "computed_rows": placement_rows_written,
            "errors": placement_errors,
        },
        "settings_snapshots": len(snapshot_rows),
        "holdout": len(holdout),
        "quasi_experiments": len(quasi),
        # Сколько из них деклассировано фильтром предыстории: правку сделали
        # в ответ на всплеск, и её эффект неотличим от возврата к среднему.
        # Такие в эластичность не идут (history.elasticity) — счётчик обязан
        # быть виден, иначе «наблюдений стало меньше» читается как потеря данных.
        "quasi_rtm_declassed": sum(
            1 for q in quasi if q.get("reliability_class") == "C"),
        # Э2.4: чтение истории. Полный покампанийный разрез в отчёт не влезает —
        # печатаются направления целиком и только уверенные кампании.
        "history": {
            **{k: v for k, v in history.items() if k != "campaigns"},
            "campaigns_confident_sample": {
                cid: row for cid, row in sorted(
                    history["campaigns"].items(),
                    key=lambda kv: -(kv[1]["p_sign"] or 0.0),
                )[:10] if row["verdict"] != "неопределённо"
            },
        },
        # Э3.1: кривые насыщения. Покампанийный разрез целиком не влезает —
        # печатаются направления и края шкалы предельной цены: самые дорогие
        # предельные лиды — кандидаты на срезание, самые дешёвые — на долив.
        "saturation": {
            **{k: v for k, v in saturation.items() if k != "campaigns"},
            "most_expensive_marginal": {
                cid: row for cid, row in sorted(
                    saturation["campaigns"].items(),
                    key=lambda kv: -kv[1]["marginal_cpl"])[:5]
            },
            "cheapest_marginal": {
                cid: row for cid, row in sorted(
                    saturation["campaigns"].items(),
                    key=lambda kv: kv[1]["marginal_cpl"])[:5]
            },
        },
        "saturation_rows": saturation_count,
        # Э7.6: недобор трафика. Считается по всем кампаниям с показами, но
        # вердикт получают только поисковые — поэтому «неопределённо» с
        # разбивкой по причине печатается рядом со счётчиками, иначе «сетям
        # некуда расти» и «про сети ничего не известно» слились бы в одно.
        "traffic_headroom": {
            "window": [headroom_from, mature_through],
            "campaigns": len(headroom_section),
            "with_room": sum(1 for r in headroom_section.values()
                             if r["verdict"] == HEADROOM_ROOM),
            "bought_out": sum(1 for r in headroom_section.values()
                              if r["verdict"] == HEADROOM_BOUGHT_OUT),
            "undetermined_by_reason": _count_by(
                [{"reason": r["reason"]} for r in headroom_section.values()
                 if r["verdict"] == HEADROOM_UNDETERMINED], "reason"),
            # Расход в кампаниях, которым есть куда расти: цена вопроса.
            "cost_with_room": round(sum(r["cost"] for r in headroom_section.values()
                                        if r["verdict"] == HEADROOM_ROOM), 2),
            "computed_rows": headroom_rows_written,
        },
        # Э3.2: порог и перенос бюджетов. Полные moves не влезают — по
        # кабинету сводка и топ уверенных сдвигов по модулю переноса.
        "budget_threshold": {
            "campaigns_no_value": budget_threshold["campaigns_no_value"],
            "accounts": {
                login: {
                    **{k: v for k, v in acc.items() if k != "moves"},
                    "top_confident_moves": _top_confident_moves(acc["moves"]),
                }
                for login, acc in budget_threshold["accounts"].items()
            },
        },
        # Рост ОБЩЕЙ суммы кабинета — отдельной секцией, а не строкой внутри
        # раскладки: пока потолок месяца не задан, это единственное место,
        # где владелец видит цену решения «тратить больше» — сколько агент
        # долил бы сегодня и чем он ограничен.
        "budget_growth": {
            login: {"cost_28d": acc["cost_28d"],
                    "budget_28d": acc["budget_28d"],
                    "growth_rub": acc["growth_rub"],
                    "proposed_growth_rub": acc["proposed_growth_rub"],
                    "deferred_growth_rub": acc["deferred_growth_rub"],
                    "capped_by": acc["growth_capped_by"],
                    "monthly_cap_rub": active_config["monthly_budget_cap_rub"],
                    "lambda": acc["lambda"]}
            for login, acc in budget_threshold["accounts"].items()
        },
        "budget_target_rows": budget_target_count,
        "computed_settings": computed_count,
        "computed_settings_by_account": {k: len(v) for k, v in computed_by_account.items()},
        "computed_settings_skipped": computed_skipped,
        "device_quality": {
            "bridge_leads": len(bridge_rows),
            "bridge_window": [bridge_from, bridge_to],
            "ratios": device_quality,
            "reason": quality_reason,
            "modifiers_adjusted": quality_adjusted,
        },
        "campaign_modifiers": campaign_modifier_summary,
        "campaign_modifiers_rows": campaign_modifier_count,
        "campaign_modifiers_skipped": campaign_modifiers_skipped,
        "segment_goal_columns": sorted(
            segment_goals, key=lambda g: (g["account"], g["purpose"], g["slice"])),
        "query_goal_columns": sorted(query_goals, key=lambda g: g["account"]),
        "profile_rows": len(profile_rows),
        "metrika_hourly": len(hourly_rows),
        "metrika_hourly_numerator": hourly_numerator,
        "metrika_hourly_goals": sorted(lead_goal_ids),
        "metrika_hourly_skipped": hourly_skipped,
        "metrika_behavior": len(resolved_behavior),
        "power": report,
        "funnel_ladder": ladder_section,
        # Э3.5: целевой CPA. Отчёт показывает не только сдвиги, но и молчание:
        # сколько кампаний рычагом не управляются и почему.
        "tcpa": {
            "target_romi": tcpa_section["target_romi"],
            "campaigns": len(tcpa_section["targets"]),
            "moves_up": tcpa_section["moves_up"],
            "moves_down": tcpa_section["moves_down"],
            "moves_confident": tcpa_section["moves_confident"],
            "no_target": tcpa_section["no_target"],
            "computed_rows": tcpa_count,
        },
        # Задача 13: чем закончились СОБСТВЕННЫЕ действия агента. Доля
        # попаданий печатается ещё и раздельно для растящих и сокращающих:
        # мера исхода судит по цене, а срезав объём, кампания почти всегда
        # дешевеет — общий hit_rate систематически хвалил бы резаков.
        "learning_loop": learning,
        # Э7.8: режим спроса по направлениям. Сезонный подъём или спад меняет
        # ожидания от кампаний направления, а не объявляется их провалом.
        "demand_regime": {
            "regimes": demand,
            # Направления, у которых ряда спроса нет ВОВСЕ, — отдельной
            # строкой. Это не «мало данных» (лечится временем), а дыра в
            # семантике спроса (лечится фразами в sync/edu_demand.py), и
            # среди них school — самое свежее направление кабинета.
            "no_series": directions_without_series(demand),
            # Регион один — 'ru'. Гео кампании нигде не вычисляется, сопоставить
            # московский срез спроса с московскими кампаниями нечем;
            # всероссийский ряд включает Москву и потому консервативен
            # (docs/AGENT-DATA-SOURCES.md).
            "region": DEMAND_REGION,
        },
        # Ранний тормоз роста: качество когорты лидов до и после последних
        # доливок. Печатается всегда — молчание тормоза и его поломка иначе
        # выглядят одинаково. Падение ПОКРЫТИЯ скором вынесено отдельным
        # списком: оно про ingest поведения, а не про качество трафика, и
        # лечится в другом месте.
        "lead_quality": {k: v for k, v in quality.items() if k != "drift"},
        # Что УСИЛИТЬ. Печатается каждый такт, в том числе пустым: молчание
        # про усиление неотличимо от «усиливать нечего», а агент, отвечающий
        # только на «что срезать», ведёт кабинет к «эффективно и мало».
        # Денег два числа: room_rub_budget кабинет освоит поднятием лимитов
        # сразу, room_rub_tcpa — только после эскалации цены конверсии.
        "growth": growth,
        # Ф12: реестр идей. Печатается КАЖДЫЙ такт, в том числе пустым:
        # пустая секция говорит «генераторы отработали, находок нет», а её
        # отсутствие читается как «генератор не запускался» — и восстановить,
        # что из двух было, задним числом уже нечем.
        "ideas": {**ideas_section(ideas_registry.open_ideas()),
                  # Что нашли ИМЕННО В ЭТОТ такт, рядом с тем, что стоит в
                  # реестре. Одного счётчика открытых идей мало: реестр помнит
                  # находки прошлых прогонов, и по нему не отличить «сегодня
                  # генераторы отработали и ничего не нашли» от «сегодня
                  # генераторы не работали вовсе».
                  "generated": generated},
        "db_total_mb": total_mb,
        "db_tables": [{"t": s["table_name"], "size": s["size"]} for s in sizes],
    }
    # Такт расчёта — половина ответа на вопрос «почему агент так решил»:
    # вторая половина (что он с этим сделал) лежит в отчёте такта записи.
    # Разбирать их порознь бессмысленно, поэтому оба едут в один чёрный ящик.
    #
    # Итог записи вкладывается в отчёт, а не печатается вторым JSON-ом: вывод
    # такта расчёта — ровно один документ, и на этом держится его разбор.
    report["blackbox"] = blackbox.save_run(
        blackbox.new_run_id(), stage="e0",
        mode=blackbox.MODE_COMPUTE, report=report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
