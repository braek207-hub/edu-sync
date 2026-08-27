# -*- coding: utf-8 -*-
"""
tests/test_agent_ideas_proven.py — генератор идей «масштабирование
доказанного» (sync/agent/ideas/proven.py).

Проверяется здесь то, что у генератора идей ломается молча:

  * тонкая связка становится идеей — и агент усиливает шум;
  * связка «чуть выше λ» становится идеей — и усиление идёт туда, где
    предельный рубль уже равен порогу, то есть растить нечего;
  * у идеи назван повод, но не назван рычаг — и она доезжает до такта записи,
    чтобы получить там отказ «применять нечем» сутки спустя;
  * идея не проходит РЕЕСТР. Это отдельная и самая дорогая болезнь: поля по
    отдельности выглядят правильными, тесты зелёные, а registry._prepare
    отвергает порцию целиком. Поэтому здесь идея гоняется и через
    registry._prepare, и через registry.upsert с подменённым доступом к базе,
    и через agent_e1.actions_from_ideas с рельсами writer/guardrails — то
    есть проверяется У ПОЛУЧАТЕЛЯ, а не у отправителя.

БД не требуется: реестр подменяется двойником (конвенция
tests/test_agent_ideas_registry.py), такт записи вызывается одной чистой
функцией.
"""

import pytest

from sync.agent import ladder
from sync.agent.ideas import proven, registry
from sync.agent.portfolio import GROWTH_LAMBDA_MARGIN
from sync.agent.writer import lanes, plan, tier
from sync.agent.writer.guardrails import check_action

# λ кабинета из плана беты (задача 12): порог, с которым сравнивается
# окупаемость связки. Число здесь — фикстура, а не порог: сам порог
# (запас ×GROWTH_LAMBDA_MARGIN) берётся из portfolio.py.
LAMBDA = 0.71

CAMPAIGN = "111"
ACCOUNT = "edu-vuz"


def _ctx(**over):
    ctx = {"account": ACCOUNT, "lambda": LAMBDA, "quality_drift": {}}
    ctx.update(over)
    return ctx


def _bundle(events=40, romi=2.6, **over):
    """Доказанная связка «кампания × сегмент × запрос», всё обязательное на месте.

    Помощник намеренно отдаёт связку, которая ДОЛЖНА стать идеей: тест на
    отказ ломает ровно одно поле, и тогда видно, что отказ пришёл именно
    из-за него, а не из-за случайно недостающего.

    events кладутся на ступень «лиды»: лестница выберет её, если их не меньше
    ladder.MIN_STEP_EVENTS, — и это единственный порог объёма в генераторе.
    """
    bundle = {
        "campaign_id": CAMPAIGN,
        "segment": {"kind": "bid_modifier:device", "key": "MOBILE"},
        "query": "колледж заочно",
        "counts": {"leads": events, "clicks": max(events, 900)},
        "romi": romi,
        # Окупаемость кампании в целом — знаменатель силы корректировки.
        "base_romi": 1.0,
        "segment_share": 0.25,
        "daily_cost_rub": 8_000.0,
        "cpa_rub": 900.0,
        "value_per_lead_rub": 2_400.0,
        # Корректировки на этом сегменте в кабинете нет — add применим.
        "current_modifier": None,
    }
    bundle.update(over)
    return bundle


def _run(ctx=None, **over):
    """candidates() на одной связке — форма вызова из плана беты."""
    ml_drop = over.pop("ml_score_drop", None)
    context = ctx or _ctx()
    if ml_drop is not None:
        # Тормоз качества живёт КАРТОЙ КАМПАНИЙ (quality.quality_drift), той
        # же самой, что уже останавливает доливку в growth.growth_candidates.
        # Своего порога падения скора у генератора нет.
        context = {**context,
                   "quality_drift": {CAMPAIGN: {"drop": ml_drop,
                                                "flagged": ml_drop >= 0.2}}}
    return proven.candidates([_bundle(**over)], context)


# ------------------------------------------------------------------ объём

def test_thin_evidence_is_not_a_proven_idea():
    # Шесть событий — это не доказательство, а шум: относительная ошибка
    # счётчика на них 41 %. Лестница на таком объёме ступени не даёт вовсе,
    # и генератор обязан молчать, каким бы красивым ни был ROMI.
    assert _run(events=6, romi=3.0) == []


def test_volume_threshold_is_the_ladders_own():
    # Порог объёма не заведён в генераторе второй копией: ровно
    # ladder.MIN_STEP_EVENTS отделяет идею от её отсутствия. Разъедься копии —
    # агент решал бы по одному правилу, а объяснял другим.
    below = int(ladder.MIN_STEP_EVENTS) - 1
    assert _run(events=below) == []
    assert _run(events=int(ladder.MIN_STEP_EVENTS))


def test_clicks_only_step_is_not_proof():
    # Лестница выбрала «клики» — значит конверсий на вердикт не набралось.
    # Окупаемость, посчитанная на том же объёме, которого лестнице не
    # хватило, — это оценка, а не доказательство, и класс 1 ей не положен.
    ideas = proven.candidates(
        [_bundle(counts={"clicks": 900, "leads": 3})], _ctx())
    assert ideas == []


# --------------------------------------------------------------- порог λ

def test_bundle_above_lambda_with_margin_becomes_tier_one():
    # Гипотезы здесь нет — есть доказательство деньгами, поэтому класс 1.
    ideas = _run(events=40, romi=2.6)
    assert ideas and ideas[0]["tier"] == tier.TIER_MEASURED


def test_barely_above_lambda_is_not_worth_scaling():
    # Ровно на пороге растить нечего: предельный рубль там уже равен λ, и
    # прибавка сдвинет связку за него. Запас — тот же, при котором солвер
    # портфеля считает осмысленным доливать (portfolio.GROWTH_LAMBDA_MARGIN).
    # base_romi ниже связки в обеих половинах — чтобы отказ (и его отсутствие)
    # шёл именно от запаса по λ, а не от порога величины корректировки.
    assert _run(romi=LAMBDA * 1.01, base_romi=LAMBDA * 0.7) == []
    assert _run(romi=LAMBDA * GROWTH_LAMBDA_MARGIN, base_romi=LAMBDA * 0.7)


def test_lambda_absent_means_silence_not_a_default():
    # Порог кабинета не посчитан — сравнивать не с чем. Подставить сюда
    # единицу значило бы придумать за портфель порог, которого он не называл.
    assert proven.candidates([_bundle()], _ctx(**{"lambda": None})) == []


# ---------------------------------------------------------------- рычаг

def test_proven_idea_names_its_lever():
    # Повод без рычага — пустой повод. У идеи обязана быть полоса, а значит и
    # вид действия, отнесённый к ней картой writer/lanes.LANE_OF_KIND.
    idea = _run()[0]
    assert idea["lane"] == lanes.LANE_TUNING
    assert idea["action"]["action_kind"] in lanes.LANE_OF_KIND


def test_lever_moves_money_without_touching_the_limit():
    # Асимметрия рычага измерена: лимит вверх связывает у 9 кампаний из 62
    # (growth.py). Поэтому рычаг генератора — перенос ВНУТРИ кампании:
    # обещание не трогает рубли (rub_delta = 0) и обещает лиды.
    expected = _run()[0]["action"]["expected"]
    assert expected["rub_delta"] == 0.0
    assert expected["leads_delta"] > 0


def test_bundle_without_a_segment_yields_nothing():
    # Связка «кампания × запрос» рычага корректировки не имеет, а денежный ей
    # не поможет. Её место — у генератора consolidate (задача 13), и выдать
    # её здесь значило бы предложить действие, которого нет.
    assert proven.candidates([_bundle(segment=None)], _ctx()) == []


def test_segment_already_under_a_modifier_is_refused():
    # Перезапись требует Id и прежнего значения из ПРОЧИТАННОГО состояния
    # кабинета; витрина настроек снимается раз в сутки и может быть
    # просрочена — откат по такому previous_state вернул бы кампанию не туда.
    assert proven.candidates([_bundle(current_modifier=30)], _ctx()) == []


def test_unknown_cabinet_state_is_refused_not_assumed_empty():
    # Молчание источника и прочитанное «корректировки нет» — разные факты.
    # Подмена первого вторым отправила бы в кабинет элемент, который
    # bidmodifiers.add отвергает, и он переотправлялся бы каждый прогон.
    bundle = _bundle()
    bundle.pop("current_modifier")
    assert proven.candidates([bundle], _ctx()) == []


def test_small_step_is_not_worth_a_request():
    # Связка лучше кампании на 2 % — корректировка меньше ±5 % не стоит
    # запроса и риска (writer/plan.MIN_ABS_PERCENT). Порог не свой.
    assert _run(romi=1.02, base_romi=1.0) == []
    assert _run(romi=1.0 + plan.MIN_ABS_PERCENT / 100.0, base_romi=1.0)


def test_expectation_is_required_for_tier_one():
    # Класс 1 держится на заявленном обещании (writer/tier._computed). Без
    # доли сегмента обещание не считается — и идея не выдаётся вовсе, а не
    # выдаётся с нулём: ноль петля обучения зачла бы сбывшимся.
    assert proven.candidates([_bundle(segment_share=None)], _ctx()) == []


# ------------------------------------------------------- качество когорты

def test_cohort_quality_drop_removes_a_candidate():
    # Рост не покупает мусор: новый трафик холоднее старого, и ранний прокси
    # качества обязан остановить усиление до денежного чекпоинта на 35-й день.
    assert _run(events=40, romi=2.6, ml_score_drop=0.25) == []


def test_intact_cohort_keeps_the_candidate():
    # Зеркальная половина: падение ниже порога тормозом не является, иначе
    # список усиления пустел бы от дневных колебаний.
    assert _run(events=40, romi=2.6, ml_score_drop=0.05)


# ---------------------------------------------------------------- адрес

def test_subject_carries_no_mutable_numbers():
    # Изменчивое число внутри subject заводило бы идею заново каждым
    # прогоном — с пустой историей и снятым отказом человека. Пересчитались
    # окупаемость и расход — идентификатор обязан остаться прежним.
    first = _run(romi=2.6, daily_cost_rub=8_000.0)[0]
    second = _run(romi=3.4, daily_cost_rub=11_500.0)[0]
    assert (registry.idea_id(first["source"], first["subject"])
            == registry.idea_id(second["source"], second["subject"]))


def test_subject_separates_queries():
    # Связка доказана на конкретном запросе; другой запрос — другая связка и
    # другая идея, а не обновление той же строки.
    a = proven.candidates([_bundle(query="колледж заочно")], _ctx())[0]
    b = proven.candidates([_bundle(query="колледж дистанционно")], _ctx())[0]
    assert registry.idea_id(a["source"], a["subject"]) != \
        registry.idea_id(b["source"], b["subject"])


# ------------------------------------------------------------ отбраковка

def test_refused_bundles_carry_a_named_reason():
    # «Связок не нашлось» и «связки были, но все отсеяны» — разные новости.
    # Молчаливый отсев оставляет человека без ответа на «почему пусто».
    report = proven.scan([_bundle(events=6)], _ctx())
    assert report["ideas"] == []
    assert report["skipped"] and report["skipped"][0]["reason"]


# ------------------------------------------------- приёмка реестром (Ф12)

def test_idea_is_accepted_by_the_registry():
    # Самая дорогая из молчаливых поломок: поля по отдельности правильные,
    # тесты зелёные, а реестр отвергает порцию целиком.
    row = registry._prepare(_run()[0])
    assert row["idea_id"] and row["tier"] == tier.TIER_MEASURED
    assert row["action"]["action_kind"] == proven.ACTION_KIND
    assert row["lane"] in lanes.ALL_LANES
    assert row["horizon_days"] == lanes.MEASURE_DAYS[lanes.LANE_TUNING]


def test_idea_survives_a_real_upsert(store):
    # Тот же путь целиком, включая слияние и запись строки: _prepare
    # проверяет форму, upsert — что порция доезжает до таблицы.
    rows = registry.upsert(_run())
    assert len(rows) == 1 and rows[0]["status"] == registry.STATUS_NEW
    assert store.writes == 1


def test_success_rule_is_machine_checkable():
    # Критерий-мнение («станет лучше») закрыть может только человек, а реестр
    # заведён ровно затем, чтобы не гонять его по одному и тому же списку.
    rule = registry._prepare(_run()[0])["success_rule"]
    assert rule["op"] in registry.COMPARISONS
    assert rule["metric"] and rule["value"] > 0


def test_success_rule_names_its_comparison_base():
    # Корректировку нельзя судить «до и после» на одной кампании: эффект
    # конфаундится сезоном и обучением стратегии.
    assert _run()[0]["success_rule"]["comparison"] == "did_vs_holdout"


def test_test_cost_and_expected_value_are_priced():
    # Без обоих чисел идея не встаёт в очередь по ценности на рубль проверки
    # (registry.rank) и уходит в хвост как непосчитанная.
    idea = _run()[0]
    assert idea["test_cost_rub"] > 0
    assert idea["expected_rub"] > 0


# ------------------------------------------------ приёмка тактом записи (Ф12)

def test_idea_reaches_the_write_plan():
    # Проверка У ПОЛУЧАТЕЛЯ: идея едет к кабинету через actions_from_ideas, и
    # именно там вскрылось бы, что вид действия не отнесён ни к одной полосе
    # или что применять идею нечем.
    from sync import agent_e1

    row = registry._prepare(_run()[0])
    actions, refused = agent_e1.actions_from_ideas([row])
    assert refused == []
    assert len(actions) == 1
    assert actions[0]["tier"] == tier.TIER_MEASURED


def test_idea_action_passes_the_guardrails():
    # Рельсы записи — последний рубеж перед кабинетом: вид вне allow-листа и
    # корректировка выше потолка ±50 % не должны доезжать до API.
    ok, reason = check_action(_run()[0]["action"])
    assert ok, reason


def test_idea_action_is_addressed_to_its_campaign():
    # Такт записи ограничивает идеи областью прогона по object_id: идея без
    # адреса кампании ушла бы в кабинет, где её объекта нет.
    assert _run()[0]["action"]["object_id"] == CAMPAIGN


# --------------------------------------------------------------- двойник

class FakeIdeas:
    """edu_agent_ideas в памяти: те же три примитива доступа, без БД."""

    def __init__(self):
        self.table = {}
        self.writes = 0

    def read_rows(self, idea_ids):
        return {i: dict(self.table[i]) for i in idea_ids if i in self.table}

    def read_rejections(self, subject_keys):
        return {}

    def write_rows(self, rows):
        self.writes += 1
        for row in rows:
            self.table[row["idea_id"]] = dict(row)
        return len(rows)


@pytest.fixture
def store(monkeypatch):
    fake = FakeIdeas()
    monkeypatch.setattr(registry, "_read_rows", fake.read_rows)
    monkeypatch.setattr(registry, "_read_rejections", fake.read_rejections)
    monkeypatch.setattr(registry, "_write_rows", fake.write_rows)
    return fake
