# -*- coding: utf-8 -*-
"""
tests/test_agent_ideas_audiences.py — генератор идей «аудитории и срезы»
(sync/agent/ideas/audiences.py).

Проверяется здесь то, что у этого генератора ломается молча и дорого:

  * идея без знаменателя. Атрибуция ретаргетинга ЗАМЕРЕНА и завышает вклад:
    счёт «по визиту» даёт ретаргетингу впятеро больше, чем счёт по людям
    (память lime-sections-overview-audit). Идея, не сказавшая, на что делила,
    сравнивает несравнимое — и выглядит при этом посчитанной;
  * разница в пределах шума. Сегмент на шестидесяти кликах «конвертит лучше»
    ровно до следующей недели;
  * дубль того, что движок делает сам. Корректировки по устройству, полу,
    возрасту и гео считаются каждым тактом (computed.compute_segment_modifiers),
    площадки режет Э3.7 (writer/placements.py). Идея поверх них — шум в
    очереди человека и второй хозяин у одной ручки;
  * повод, растворившийся в молчании: «поводов не нашлось» и «поводы были, но
    все на шуме» ведут к разным следующим шагам.

БД не требуется: реестр подменяется двойником (фикстура store в conftest).
"""

import pytest

from sync.agent.experiments import HORIZON_DAYS, METRIC
from sync.agent.ideas import audiences, registry
from sync.agent.ladder import MIN_STEP_EVENTS
from sync.agent.portfolio import GROWTH_LAMBDA_MARGIN
from sync.agent.writer import lanes, tier

ACCOUNT = "edu-vuz"
CAMPAIGN = "111"


def _ctx(**over):
    ctx = {"account": ACCOUNT}
    ctx.update(over)
    return ctx


def _segment(**over):
    """Срез, который ДОЛЖЕН дать идею.

    Тест на отказ ломает ровно одно поле — тогда видно, что отказ пришёл
    именно из-за него, а не из-за случайно недостающего.
    """
    row = {
        "kind": audiences.KIND_AUDIENCE,
        "segment_key": "retarget-234",
        "segment_name": "Были на странице СПО, не оставили заявку",
        "campaign_id": CAMPAIGN,
        # На что делили конверсию: люди или визиты. Поле обязательное.
        "denominator": "users",
        "clicks": 400.0,
        "conversions": 32.0,
        "base_cr": 0.03,
        "campaign_clicks": 5_000.0,
        "cost_rub": 24_000.0,
        "window_days": 28,
        "base_cpl_rub": 1_400.0,
    }
    row.update(over)
    return row


def _idea(rows=None, ctx=None):
    ideas = audiences.candidates(rows if rows is not None else [_segment()],
                                 ctx or _ctx())
    assert ideas, "срез должен был дать идею"
    return ideas[0]


# --------------------------------------------------------------- повод


def test_high_cr_low_volume_segment_becomes_an_idea():
    # Шаг 1 плана беты.
    assert audiences.candidates([_segment()], _ctx())


def test_idea_addresses_the_segment_and_its_campaign():
    subject = _idea()["subject"]
    assert subject["segment_key"] == "retarget-234"
    assert subject["campaign_id"] == CAMPAIGN


# ---------------------------------------------------------- знаменатель


def test_audience_idea_carries_its_denominator():
    # Шаг 2 плана беты.
    assert _idea()["subject"]["denominator"] in audiences.DENOMINATORS


def test_segment_without_a_denominator_is_refused():
    # Замер: счёт «по визиту» даёт ретаргетингу впятеро больше, чем счёт по
    # людям. Идея, умолчавшая о знаменателе, — это ×5 без предупреждения.
    result = audiences.scan([_segment(denominator=None)], _ctx())

    assert result["ideas"] == []
    assert any("знаменател" in row["reason"] for row in result["skipped"])


def test_unknown_denominator_is_refused_not_guessed():
    result = audiences.scan([_segment(denominator="как-то так")], _ctx())
    assert result["ideas"] == []


def test_denominator_is_part_of_the_address():
    # Тот же сегмент, посчитанный по людям и по визитам, — РАЗНОЕ утверждение
    # (замеренная разница впятеро). Один адрес на оба означал бы, что вторая
    # находка молча затирает первую.
    by_users = registry._prepare(_idea())
    by_visits = registry._prepare(_idea(rows=[_segment(denominator="visits")]))

    assert by_users["idea_id"] != by_visits["idea_id"]


# ------------------------------------------------------------------ шум


def test_noise_level_difference_is_not_an_idea():
    # Шаг 3 плана беты: 60 кликов и разница в полпроцента.
    noise = _segment(clicks=60.0, conversions=2.1, base_cr=0.03)
    assert audiences.candidates([noise], _ctx()) == []


def test_volume_threshold_is_the_ladder_one():
    # Порог событий не свой: та же ступень, на которой лестница воронки
    # вообще выносит суждение (ladder.MIN_STEP_EVENTS). Две копии одного
    # порога разъехались бы при первой правке одной из них.
    enough = _segment(clicks=400.0, conversions=float(MIN_STEP_EVENTS))
    thin = _segment(clicks=400.0, conversions=float(MIN_STEP_EVENTS) - 1)

    assert audiences.candidates([enough], _ctx())
    assert audiences.candidates([thin], _ctx()) == []


def test_lift_must_clear_the_margin_not_just_the_base():
    # «Ровно на пороге» — не повод: запас тот же, которым отделяет доказанное
    # от пограничного портфель (portfolio.GROWTH_LAMBDA_MARGIN).
    base = 0.03
    barely = _segment(clicks=1_000.0, conversions=base * 1_000.0 * 1.05,
                      base_cr=base)
    clear = _segment(clicks=1_000.0,
                     conversions=base * 1_000.0 * (GROWTH_LAMBDA_MARGIN + 0.05),
                     base_cr=base)

    assert audiences.candidates([barely], _ctx()) == []
    assert audiences.candidates([clear], _ctx())


def test_segment_below_the_base_is_not_an_expansion_reason():
    weak = _segment(conversions=6.0, base_cr=0.05)
    assert audiences.candidates([weak], _ctx()) == []


def test_exhausted_segment_is_not_an_expansion_reason():
    # Сегмент, который уже забрал полкампании, расширять некуда: идея была бы
    # предложением «таргетируйте то, что и так везде».
    big = _segment(clicks=4_000.0, conversions=320.0, campaign_clicks=5_000.0)
    assert audiences.candidates([big], _ctx()) == []


def test_share_limit_is_a_setting_not_a_constant():
    # Доля, ниже которой сегмент считается неисчерпанным, — вопрос кабинета,
    # а не арифметики. Пара показывает, что отказ пришёл именно из-за неё.
    big = _segment(clicks=4_000.0, conversions=320.0, campaign_clicks=5_000.0)

    assert audiences.candidates([big], _ctx()) == []
    assert audiences.candidates(
        [big], _ctx(config={audiences.MAX_SHARE_KEY: 0.9}))


# -------------------------------------------- чужая территория движка


def test_engine_owned_segment_kinds_are_refused():
    # Устройство, пол, возраст и гео движок тюнит каждым тактом
    # (computed.compute_segment_modifiers). Идея поверх — второй хозяин у
    # одной ручки и шум в очереди человека.
    for kind in ("device", "gender", "age", "region"):
        result = audiences.scan([_segment(kind=kind)], _ctx())
        assert result["ideas"] == []
        assert result["skipped"]


def test_placements_are_refused_as_an_existing_lever():
    # Площадки режет Э3.7 (writer/placements.py) — рычаг живой, и повторять
    # его идеей значит заводить второго хозяина.
    result = audiences.scan([_segment(kind="placement")], _ctx())

    assert result["ideas"] == []
    assert any("площад" in row["reason"] for row in result["skipped"])


def test_engine_owned_kinds_are_listed_not_hardcoded_twice():
    # Список чужой территории выведен из карты движка, а не переписан рядом:
    # добавят срез в SEGMENT_FIELDS — генератор узнает об этом сам.
    assert "device" in audiences.ENGINE_OWNED_KINDS
    assert audiences.KIND_AUDIENCE not in audiences.ENGINE_OWNED_KINDS


def test_unknown_kind_is_refused_not_guessed():
    result = audiences.scan([_segment(kind="что-то новое")], _ctx())
    assert result["ideas"] == [] and result["skipped"]


# ------------------------------------------------------------ форма идеи


def test_idea_is_a_proposal_until_the_audience_lever_exists():
    # Рычаг audience.add — задача 23, в allow-листе записи его нет. Класс 2
    # требует нагрузки рычага (registry._check_action), и выдать её сейчас
    # значило бы обещать отправку, которой не будет.
    idea = _idea()
    assert idea["tier"] == tier.TIER_PROPOSAL
    assert idea["lane"] == lanes.LANE_PROPOSAL
    assert "action" not in idea


def test_idea_names_what_it_needs_to_become_a_bet():
    assert "23" in _idea()["detail"]["needs"]


def test_idea_carries_the_numbers_it_was_built_on():
    # Основание едет вместе с идеей: без конверсии, базы и объёма человек на
    # экране видит утверждение без доказательства.
    detail = _idea()["detail"]
    assert detail["cr"] == pytest.approx(0.08)
    assert detail["base_cr"] == 0.03
    assert detail["conversions"] == 32.0 and detail["clicks"] == 400.0
    assert detail["share_of_clicks"] == pytest.approx(0.08)


def test_horizon_is_the_stake_one_without_relearning():
    # Аудитория обучение стратегии не сбрасывает (writer/lanes.py: та же
    # корректировка сегмента), значит недель переобучения к сроку не
    # прибавляется — горизонт ставки и есть срок.
    assert _idea()["horizon_days"] == HORIZON_DAYS


def test_volume_at_risk_is_measured_but_is_not_the_price_of_the_check():
    # Расход сегмента за горизонт по нынешнему темпу (24 000 ₽ за 28 дней это
    # 857,14 ₽ в день) считается по-прежнему и виден человеку — но в detail, а
    # не в колонке сметы. Выгоду расширения аудитории посчитать нечем (потолок
    # сегмента неизвестен), а строка со сметой при пустом ожидании реестру
    # запрещена: считаем обе величины или ни одной
    # (ideas/limits.unpaired_reason). Смета к тому же означала бы списание
    # риск-бюджета полосы, а предложение им не платит вовсе.
    idea = _idea()

    assert idea["expected_rub"] is None
    assert idea["test_cost_rub"] is None
    assert idea["detail"]["segment_cost_at_risk_rub"] == pytest.approx(
        24_000.0 / 28 * HORIZON_DAYS, rel=1e-6)


def test_volume_at_risk_is_empty_when_the_spend_is_unknown():
    # Непосчитанный объём — не ноль: ноль читался бы на экране как «сегмент
    # ничего не тратит», хотя расход просто не приехал.
    idea = _idea(rows=[_segment(cost_rub=None)])
    assert idea["detail"]["segment_cost_at_risk_rub"] is None


def test_success_rule_beats_the_campaign_price():
    rule = _idea()["success_rule"]
    assert rule["metric"] == METRIC and rule["op"] in registry.COMPARISONS
    assert rule["value"] == 1_400.0


def test_segment_without_a_campaign_price_is_refused():
    result = audiences.scan([_segment(base_cpl_rub=None)], _ctx())
    assert result["ideas"] == []
    assert any("цен" in row["reason"] for row in result["skipped"])


def test_subject_does_not_carry_floating_numbers():
    subject = _idea()["subject"]
    assert not {"cr", "clicks", "conversions", "test_cost_rub"} & set(subject)


def test_identity_survives_a_change_of_numbers():
    first = registry._prepare(_idea())
    moved = _segment(clicks=520.0, conversions=45.0)
    second = registry._prepare(_idea(rows=[moved]))

    assert first["idea_id"] == second["idea_id"]
    assert first["detail"] != second["detail"]


def test_order_is_deterministic():
    rows = [_segment(segment_key="b-second"), _segment(segment_key="a-first")]
    first = [i["subject"]["segment_key"] for i in audiences.candidates(rows, _ctx())]
    second = [i["subject"]["segment_key"] for i in audiences.candidates(rows, _ctx())]

    assert first == second == ["a-first", "b-second"]


# ------------------------------------------------- проверка у получателя


def test_idea_is_accepted_by_the_registry():
    row = registry._prepare(_idea())
    assert row["source"] == audiences.SOURCE
    assert row["detail"]["denominator"] == "users"


def test_idea_survives_a_real_upsert(store):
    rows = registry.upsert(audiences.candidates([_segment()], _ctx()))
    assert len(rows) == 1 and rows[0]["status"] == registry.STATUS_NEW
    assert len(store.table) == 1
