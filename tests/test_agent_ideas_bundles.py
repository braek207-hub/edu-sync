# -*- coding: utf-8 -*-
"""
tests/test_agent_ideas_bundles.py — сборка связок для генераторов идей
(sync/agent/ideas/bundles.py).

Пять генераторов Ф13 умеют судить о связке, но связку до сих пор никто не
собирал. Здесь проверяется то, что у сборщика ломается молча и дорого:

  * ПОДМЕНА НЕЗНАНИЯ НУЛЁМ. «Витрина настроек не снята» и «в кабинете
    корректировки нет» — разные факты. Выдай первое за второе — и в кабинет
    уедет bidmodifiers.add поверх существующей корректировки: Директ отвергнет
    элемент, а действие будет переотправляться каждый прогон, съедая потолок
    попыток. То же самое у связывающего лимита и у сброса обучения;

  * РАЗНЫЕ ОКНА У СВЯЗКИ И ЕЁ БАЗЫ. Окупаемость сегмента делится на
    окупаемость кампании (proven.py: отсюда сила корректировки). Возьми базу с
    другого окна — и сезон внутри отношения выглядел бы заслугой сегмента;

  * ВЫДУМАННЫЙ ПЕРЕСЧЁТ. Ожидаемые оплаты связки считает лестница воронки
    такта, а не множитель, заведённый здесь: у связки своих коэффициентов
    перехода быть не может, событий на них не набралось бы никогда;

  * МОЛЧАЛИВОЕ ВЫПАДЕНИЕ. Строка без адреса обязана уехать в skipped с
    причиной: «связок не нашлось» и «связки были, но их некуда приписать» —
    разные новости.

БД и сеть не нужны: на вход подаются те же структуры, которые собирает
расчётный такт.
"""

import pytest

from sync.agent import ladder as ladder_mod
from sync.agent.ideas import audiences, bundles, proven
from sync.agent.writer import plan as plan_mod

ACCOUNT = "acc-1"
CAMPAIGN = "111"
DIRECTION = "vuz"
WINDOW_DAYS = 90

# Пул воронки кабинета: по нему считаются коэффициенты перехода связок.
POOL = {"paid": 120.0, "deals": 200.0, "connected": 400.0,
        "eff": 900.0, "leads": 1800.0, "clicks": 90_000.0}


def _ladder(**over):
    section = {
        "window_from": "2026-05-01",
        "window_to": "2026-07-29",
        "by_object": {CAMPAIGN: {"step": "eff", "events_by_step": dict(POOL)}},
        "counts": {"by_direction": {DIRECTION: dict(POOL)}, "account": dict(POOL)},
        "avg_check": {CAMPAIGN: 60_000.0},
    }
    section.update(over)
    return section


def _portfolio(**move_over):
    move = {
        "direction": DIRECTION,
        "value_per_lead": 1_500.0,
        "cost_28d": 300_000.0,
        "leads_28d": 200,
        "limit_binding": True,
    }
    move.update(move_over)
    return {"accounts": {ACCOUNT: {"lambda": 1.0, "moves": {CAMPAIGN: move}}}}


def _facts():
    return [{"fact_date": "2026-07-01", "campaign_id": CAMPAIGN,
             "direction": DIRECTION, "cost": 10_000.0, "eff_leads": 8}]


def _index(ladder=None, portfolio=None, settings=None):
    return bundles.campaign_index(
        _facts(), ladder or _ladder(), portfolio or _portfolio(),
        login_by_campaign={CAMPAIGN: ACCOUNT},
        settings_by_campaign=settings or {},
        direction_by_campaign={CAMPAIGN: DIRECTION})


def _slice(key="MOBILE", clicks=6_000.0, conversions=120.0, cost=180_000.0,
           kind="device", campaign_id=CAMPAIGN):
    return {"week_start": "2026-07-06", "campaign_id": campaign_id,
            "slice_kind": kind, "slice_key": key, "clicks": clicks,
            "conversions": conversions, "cost": cost}


def _rows(sliced, settings=None):
    return bundles.segment_bundles(sliced, _index(settings=settings),
                                   window_days=WINDOW_DAYS)


def _bundle(sliced=None, settings=None, key="MOBILE"):
    result = _rows(sliced if sliced is not None else [_slice()], settings)
    return next(b for b in result["bundles"] if b["segment"]["key"] == key)


def _settings(*items):
    return {CAMPAIGN: {"bidModifiers": {"total": len(items), "items": list(items)}}}


def _modifier(direct_type="MOBILE_ADJUSTMENT", percent=20, **over):
    item = {"id": 7, "type": direct_type, "level": "CAMPAIGN",
            "percent": percent, "detail": None, "regionId": None}
    item.update(over)
    return item


# ============================================ инвариант current_modifier
# Шаг 1 плана беты дословно: связка без снятой витрины настроек не
# утверждает, что корректировки нет.


def test_bundle_without_a_settings_snapshot_says_nothing_about_modifiers():
    # Ключа НЕТ вовсе — это и есть «состояние неизвестно». None на его месте
    # означал бы «корректировки в кабинете нет», и proven.py разрешил бы add.
    assert "current_modifier" not in _bundle()


def test_empty_snapshot_is_not_the_same_as_a_missing_one():
    # Витрина снята и корректировок в ней нет — вот это уже утверждение, и
    # add по нему законен.
    bundle = _bundle(settings=_settings())

    assert "current_modifier" in bundle
    assert bundle["current_modifier"] is None


def test_existing_modifier_travels_with_its_value():
    assert _bundle(settings=_settings(_modifier(percent=20)))["current_modifier"] == 20.0


def test_a_modifier_of_another_segment_does_not_count_as_this_ones():
    # Корректировка десктопа не говорит ничего о мобильных: спутай их — и add
    # по мобильным не поедет никогда, «потому что корректировка уже есть».
    bundle = _bundle(settings=_settings(_modifier("DESKTOP_ADJUSTMENT", 30)))

    assert bundle["current_modifier"] is None


def test_demographic_modifiers_are_told_apart_by_their_key():
    # У пола и возраста ОДИН тип корректировки (DEMOGRAPHICS_ADJUSTMENT), и
    # различает их только ключ. Сверяй лишь тип — и корректировка по полу
    # закрыла бы возрастную, и наоборот.
    settings = _settings(_modifier("DEMOGRAPHICS_ADJUSTMENT", 40,
                                   detail="GENDER_MALE"))
    result = bundles.segment_bundles(
        [_slice(kind="gender", key="GENDER_MALE"),
         _slice(kind="gender", key="GENDER_FEMALE")],
        _index(settings=settings), window_days=WINDOW_DAYS)
    value = {b["segment"]["key"]: b.get("current_modifier")
             for b in result["bundles"]}

    assert value["GENDER_MALE"] == 40.0
    assert value["GENDER_FEMALE"] is None


def test_adgroup_level_modifier_does_not_block_the_campaign_one():
    # Корректировка группы объявлений — другой объект Директа: кампанийный add
    # она не отвергнет, и выдавать её за кампанийную значит запретить рычаг
    # там, где он применим.
    settings = _settings(_modifier(level="ADGROUP"))

    assert _bundle(settings=settings)["current_modifier"] is None


def test_snapshot_without_the_modifiers_block_is_unknown_not_empty():
    # Витрина старой формы: строка есть, блока корректировок нет. Прочти это
    # как «корректировок нет» — и утверждение будет сделано из ничего.
    assert "current_modifier" not in _bundle(settings={CAMPAIGN: {"meta": {}}})


def test_untranslatable_segment_is_not_asked_about_its_modifier():
    # У сети (network) типа корректировки нет вовсе — спрашивать витрину не о
    # чем. Отказ по такому виду выносит генератор, а не сборщик.
    bundle = _bundle([_slice(kind="network", key="AD_NETWORK")],
                     settings=_settings(), key="AD_NETWORK")

    assert "current_modifier" not in bundle


# ================================================= окно связки и её базы


def test_bundle_and_its_base_are_counted_on_the_same_window():
    # Окупаемость сегмента и окупаемость кампании считаются по одному отчёту и
    # одному окну — отсюда отношение, которое задаёт силу корректировки.
    bundle = _bundle([_slice("MOBILE"), _slice("DESKTOP", clicks=6_000.0,
                                               conversions=40.0, cost=180_000.0)])

    assert bundle["romi"] > bundle["base_romi"] > 0


def test_base_moves_with_the_rest_of_the_campaign_and_the_segment_does_not():
    # Прямая проверка «база — это кампания, а не сам сегмент»: ухудшили ВТОРОЙ
    # сегмент — база просела, окупаемость первого не шелохнулась.
    rich = _bundle([_slice("MOBILE"), _slice("DESKTOP", conversions=100.0)])
    poor = _bundle([_slice("MOBILE"), _slice("DESKTOP", conversions=30.0)])

    assert rich["romi"] == poor["romi"]
    assert rich["base_romi"] > poor["base_romi"]


def test_segment_share_is_counted_in_clicks_of_its_own_kind():
    # Доля нужна цене риска (writer/exposure.py): корректировка ставит под
    # удар только объём сегмента. Считается по кликам своего среза — доли
    # разных срезов не складываются.
    bundle = _bundle([_slice("MOBILE", clicks=3_000.0),
                      _slice("DESKTOP", clicks=1_000.0)])

    assert bundle["segment_share"] == pytest.approx(0.75)


def test_daily_cost_and_cpa_come_from_the_slice_itself():
    bundle = _bundle([_slice(cost=180_000.0, conversions=120.0)])

    assert bundle["daily_cost_rub"] == pytest.approx(180_000.0 / WINDOW_DAYS)
    assert bundle["cpa_rub"] == pytest.approx(1_500.0)


def test_segment_without_conversions_has_no_price_not_a_zero_one():
    # Ноль конверсий — это не «лид бесплатный». Ключа cpa_rub быть не должно.
    assert "cpa_rub" not in _bundle([_slice(conversions=0.0)])


def test_romi_is_absent_when_the_average_check_is_unknown():
    # Чек направления не посчитан — окупаемость считать не из чего. Отказ по
    # ней вынесет генератор своей причиной, а сборщик не выдумывает число.
    ladder = _ladder(avg_check={})

    result = bundles.segment_bundles([_slice()], _index(ladder=ladder),
                                     window_days=WINDOW_DAYS)

    assert "romi" not in result["bundles"][0]


# =================================================== молчаливое выпадение


def test_slice_without_a_campaign_is_refused_with_a_reason():
    result = _rows([_slice(campaign_id="")])

    assert result["bundles"] == []
    assert [r["reason"] for r in result["skipped"]] == [bundles.REASON_NO_CAMPAIGN]


def test_slice_of_an_unknown_campaign_is_refused_with_a_reason():
    # Кампании нет среди посчитанных тактом: ни ступени воронки, ни кабинета.
    result = _rows([_slice(campaign_id="999")])

    assert result["bundles"] == []
    assert result["skipped"][0]["reason"] == bundles.REASON_UNKNOWN_CAMPAIGN


def test_slice_without_a_segment_key_is_refused_with_a_reason():
    result = _rows([_slice(key="")])

    assert result["bundles"] == []
    assert result["skipped"][0]["reason"] == bundles.REASON_NO_SEGMENT_KEY


def test_weekly_rows_of_one_segment_are_summed_not_multiplied():
    # Срез приходит недельными строками. Не сложи их — и связка судилась бы по
    # одной неделе, а доля считалась бы от неё же.
    week_one = _slice(clicks=1_000.0, conversions=20.0, cost=30_000.0)
    week_two = {**week_one, "week_start": "2026-07-13"}

    bundle = _bundle([week_one, week_two])

    assert bundle["counts"]["clicks"] == 2_000.0
    assert bundle["daily_cost_rub"] == pytest.approx(60_000.0 / WINDOW_DAYS)


# ============================================== доноры выноса (consolidate)


def _query(phrase="колледж заочно", conversions=60.0, cost=90_000.0,
           campaign_id=CAMPAIGN, clicks=3_000.0):
    return {"query": phrase, "campaign_id": campaign_id, "clicks": clicks,
            "conversions": conversions, "cost": cost}


def _donors(rows, phrases=("колледж заочно",), index=None):
    return bundles.query_donors(rows, index or _index(), phrases=phrases,
                                window_days=30)


def test_only_the_phrases_the_tact_already_picked_become_donors():
    # Второго отбора «что достойно выноса» здесь нет: свой критерий разъехался
    # бы с расширением семантики, которое печатается в отчёте прогона.
    result = _donors([_query(), _query("мти", conversions=90.0)])

    assert [r["phrase"] for r in result["rows"]] == ["колледж заочно"]


def test_donor_payments_are_counted_by_the_funnel_not_by_a_magic_ratio():
    # Ожидаемые оплаты — переход leads→paid лестницы кампании. Множитель,
    # заведённый здесь, был бы вторым мнением о воронке.
    donor = _donors([_query(conversions=60.0)])["rows"][0]

    assert 0 < donor["p_pay_sum"] < 60.0


def test_donor_payments_follow_the_campaign_funnel():
    # Тот же запрос в кампании с вдвое худшим переходом даёт вдвое меньше
    # ожидаемых оплат. Константа этого не умеет.
    weak = {**POOL, "paid": POOL["paid"] / 2}
    ladder = _ladder(counts={"by_direction": {DIRECTION: weak}, "account": weak})

    rich = _donors([_query()])["rows"][0]
    poor = _donors([_query()], index=_index(ladder=ladder))["rows"][0]

    assert poor["p_pay_sum"] == pytest.approx(rich["p_pay_sum"] / 2, rel=0.05)


def test_donor_carries_its_direction_and_account_from_the_index():
    donor = _donors([_query()])["rows"][0]

    assert (donor["direction"], donor["account"]) == (DIRECTION, ACCOUNT)


def test_donor_of_an_unknown_campaign_is_refused_with_a_reason():
    result = _donors([_query(campaign_id="999")])

    assert result["rows"] == []
    assert result["skipped"][0]["reason"] == bundles.REASON_UNKNOWN_CAMPAIGN


def test_query_row_without_a_phrase_is_refused_with_a_reason():
    result = _donors([_query(phrase="")])

    assert result["rows"] == []
    assert result["skipped"][0]["reason"] == bundles.REASON_NO_PHRASE


# ============================================== кампании тестов (abtest)


def _tests(index=None, holdout=(), reset=None, today=None):
    from datetime import date
    return bundles.campaign_tests(
        index or _index(), holdout_ids=holdout, learning_reset=reset or {},
        today=today or date(2026, 8, 27))


def test_campaign_row_carries_the_portfolio_numbers():
    row = _tests()[0]

    assert row["eff_leads"] == 200.0
    assert row["cost_rub"] == 300_000.0
    assert row["window_days"] == bundles.PORTFOLIO_WINDOW_DAYS


def test_binding_limit_is_three_valued():
    # Ключа нет — состояние кабинета не снято; False — лимит не связывает;
    # True — связывает. Подмена первого вторым предложила бы прибавку вслепую.
    portfolio = _portfolio()
    del portfolio["accounts"][ACCOUNT]["moves"][CAMPAIGN]["limit_binding"]

    unknown = _tests(index=_index(portfolio=portfolio))[0]
    known = _tests()[0]

    assert "limit_binds" not in unknown
    assert known["limit_binds"] is True


def test_days_since_learning_reset_is_absent_when_the_journal_is_silent():
    # Кампанию агент не трогал — «дней с последнего сброса» не существует.
    # Ноль на этом месте читался бы как «сбросили сегодня» и запретил бы тест.
    assert "days_since_learning_reset" not in _tests()[0]


def test_days_since_learning_reset_is_counted_from_the_journal():
    from datetime import date

    rows = _tests(reset={CAMPAIGN: date(2026, 8, 13)}, today=date(2026, 8, 27))

    assert rows[0]["days_since_learning_reset"] == 14


def test_holdout_campaign_is_marked_as_such():
    # Заповедник — база сравнения, а не полигон: тест на нём убил бы саму
    # возможность сравнивать.
    assert _tests(holdout=[CAMPAIGN])[0]["in_holdout"] is True


# ================================================= поводы рынка (market)


def _demand(regime="подъём"):
    return {DIRECTION: {"regime": regime, "sigma": 2.4, "frequency": 12_000,
                        "baseline_median": 9_000, "last_week": "2026-08-17"}}


def test_rising_demand_with_uncovered_phrases_is_not_covered():
    rows = bundles.demand_rows(
        _demand(), account=ACCOUNT,
        uncovered_by_direction={DIRECTION: ["колледж заочно"]},
        cpl_by_direction={DIRECTION: 1_400.0}, live_directions=[DIRECTION])

    assert rows[0]["covered"] is False
    assert rows[0]["uncovered_phrases"] == ["колледж заочно"]
    assert rows[0]["direction_cpl_rub"] == 1_400.0


def test_live_direction_without_uncovered_phrases_is_covered():
    # Кабинет в этом направлении торгуется, и окупающихся фраз без своей
    # ключевой расширение не нашло — растущий спрос он уже забирает.
    rows = bundles.demand_rows(
        _demand(), account=ACCOUNT, uncovered_by_direction={},
        cpl_by_direction={DIRECTION: 1_400.0}, live_directions=[DIRECTION])

    assert rows[0]["covered"] is True


def test_direction_the_cabinet_does_not_run_is_not_called_covered():
    # Пустое расширение у направления, которого в кабинете нет, — это не
    # покрытие: там нечего покрывать, и повод остаётся поводом.
    rows = bundles.demand_rows(
        _demand(), account=ACCOUNT, uncovered_by_direction={},
        cpl_by_direction={}, live_directions=[])

    assert rows[0]["covered"] is False


def test_demand_row_keeps_the_regime_verdict_as_it_is():
    # Режим спроса выносит demand.py. Пересуди его здесь — и у кабинета
    # оказались бы два несогласных мнения о том же ряде.
    rows = bundles.demand_rows(
        _demand(regime="мало данных"), account=ACCOUNT,
        uncovered_by_direction={}, cpl_by_direction={}, live_directions=[])

    assert rows[0]["regime"] == "мало данных"


# ==================================================== справочник кампаний


def test_index_takes_the_account_from_the_portfolio_first():
    # Раскладка знает кабинет кампании точно (по ней двигаются деньги),
    # справочник имён — с запозданием на пересборку.
    assert _index()[CAMPAIGN]["account"] == ACCOUNT


def test_index_keeps_a_campaign_the_portfolio_skipped():
    # Кампания без ценности лида в раскладку не попадает (portfolio.py:
    # campaigns_no_value). Выбрось её и здесь — и связки по ней исчезли бы
    # молча, вместе с названной причиной отказа генератора.
    portfolio = {"accounts": {}}

    entry = _index(portfolio=portfolio)[CAMPAIGN]

    assert entry["account"] == ACCOUNT and entry["direction"] == DIRECTION
    assert entry["value_per_lead_rub"] is None


def test_index_pools_are_the_ladder_ones():
    # Пулы связки — направление и кабинет, те же, что у лестницы такта.
    names = [name for name, _counts in _index()[CAMPAIGN]["pools"]]

    assert names == [f"direction:{DIRECTION}", "account"]


# ================================================== чего входа нет вовсе


def test_the_generator_without_an_input_is_named_not_silent():
    # Ноль находок у генератора аудиторий значит «спрашивать было не о чем», а
    # не «поводов не нашлось». По пустому счётчику это неразличимо.
    assert audiences.SOURCE in bundles.SOURCES_WITHOUT_INPUT
    assert bundles.SOURCES_WITHOUT_INPUT[audiences.SOURCE]


# ========================================= проверка у получателя (proven)


def test_bundle_is_accepted_by_the_generator_it_is_built_for():
    # Сборщик может собрать честно, а генератор — не найти в связке того, что
    # читает. Проверка идёт у получателя: связка обязана дать идею.
    bundle = _bundle([_slice("MOBILE", clicks=6_000.0, conversions=300.0,
                             cost=90_000.0),
                      _slice("DESKTOP", clicks=6_000.0, conversions=40.0,
                             cost=180_000.0)],
                     settings=_settings())
    ideas = proven.candidates([bundle], {"account": ACCOUNT, "lambda": 1.0})

    assert [i["subject"]["segment"]["key"] for i in ideas] == ["MOBILE"]


def test_the_segment_address_is_the_one_the_write_lever_understands():
    # Вид сегмента переводится в тип корректировки Директа тем же
    # справочником, которым его переводит план записи. Своя таблица здесь
    # означала бы, что в кабинет уедет элемент, которого он не знает.
    kind = _bundle()["segment"]["kind"]
    direct_type, _key, reason = plan_mod.direct_type_for(kind, "MOBILE")

    assert direct_type == "MOBILE_ADJUSTMENT" and reason == ""


def test_bundle_without_a_query_omits_the_key_instead_of_emptying_it():
    # Директ не отдаёт срез в разрезе запросов. Пустой ключ дал бы двум формам
    # одного адреса разные отпечатки (докстринг proven._subject).
    assert "query" not in _bundle()


def test_counts_are_the_ladder_steps_not_invented_names():
    counts = _bundle()["counts"]

    assert set(counts) <= set(ladder_mod.STEP_ORDER)
