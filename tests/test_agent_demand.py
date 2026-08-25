# -*- coding: utf-8 -*-
"""Спрос Wordstat как календарь направлений: подъём, спад, норма."""

from sync.agent.demand import (
    DIRECTION_BY_PHRASE, DIRECTIONS_WITHOUT_SERIES, demand_regime,
    directions_without_series, weekly_demand_by_direction,
)
from sync.edu_demand import EDU_DEMAND_PHRASES


def _row(week, phrase, frequency, region="ru"):
    return {"week_start": week, "region": region, "phrase": phrase,
            "frequency": frequency}


def _flat(weeks, phrase="колледж", frequency=100):
    """Восемь ровных недель базы: 2026-06-01 … 2026-07-22."""
    return [_row(f"2026-0{6 + w // 4}-{1 + (w % 4) * 7:02d}", phrase, frequency)
            for w in range(weeks)]


def test_phrases_map_to_campaign_directions():
    assert DIRECTION_BY_PHRASE["колледж"] == "spo"
    assert DIRECTION_BY_PHRASE["вуз"] == "vpo"
    assert DIRECTION_BY_PHRASE["магистратура"] == "vpo"
    assert DIRECTION_BY_PHRASE["заочное обучение"] == "dist"
    # ДПО в классификаторе кампаний отсутствует — направление своё, и это
    # само по себе сигнал: спрос есть, кампаний под него нет.
    assert DIRECTION_BY_PHRASE["переподготовка"] == "dpo"


def test_every_synced_phrase_is_mapped():
    # Фраза, добавленная в синк и забытая здесь, молча выпала бы из спроса.
    assert set(EDU_DEMAND_PHRASES) - set(DIRECTION_BY_PHRASE) == set()


def test_weekly_sums_phrases_within_direction():
    rows = [_row("2026-08-17", "колледж", 100), _row("2026-08-17", "техникум", 50),
            _row("2026-08-17", "вуз", 200)]
    out = weekly_demand_by_direction(rows)
    assert out["spo"]["2026-08-17"] == 150
    assert out["vpo"]["2026-08-17"] == 200


def test_only_ru_region_counted():
    # Москва — подмножество РФ, и складывать их значит считать москвичей дважды.
    rows = [_row("2026-08-17", "колледж", 100),
            _row("2026-08-17", "колледж", 40, region="msk")]
    assert weekly_demand_by_direction(rows)["spo"]["2026-08-17"] == 100


def test_rise_detected_against_baseline():
    rows = _flat(8)
    rows.append(_row("2026-08-17", "колледж", 200))
    out = demand_regime(rows, through_week="2026-08-17")
    assert out["spo"]["regime"] == "подъём"
    assert out["spo"]["frequency"] == 200


def test_fall_detected_against_baseline():
    rows = _flat(8)
    rows.append(_row("2026-08-17", "колледж", 20))
    assert demand_regime(rows, through_week="2026-08-17")["spo"]["regime"] == "спад"


def test_flat_series_is_normal():
    rows = _flat(8)
    rows.append(_row("2026-08-17", "колледж", 103))
    assert demand_regime(rows, through_week="2026-08-17")["spo"]["regime"] == "норма"


def test_zero_spread_baseline_still_detects_rise():
    # Ровная база даёт нулевой разброс: делить на него нельзя, а объявлять
    # удвоение спроса «нормой» — тем более. Пол разброса = шум счёта.
    rows = _flat(8)
    rows.append(_row("2026-08-17", "колледж", 200))
    out = demand_regime(rows, through_week="2026-08-17")
    assert out["spo"]["sigma"] > 0


def test_weeks_after_through_week_ignored():
    # Последняя неделя окна — та, что запросили: неполная свежая неделя
    # выгрузки иначе объявила бы спад на каждом прогоне.
    rows = _flat(8)
    rows.append(_row("2026-08-17", "колледж", 100))
    rows.append(_row("2026-08-24", "колледж", 5))
    out = demand_regime(rows, through_week="2026-08-17")
    assert out["spo"]["last_week"] == "2026-08-17"
    assert out["spo"]["regime"] == "норма"


def test_short_history_says_not_enough_data():
    rows = [_row("2026-08-10", "колледж", 100), _row("2026-08-17", "колледж", 300)]
    assert demand_regime(rows, through_week="2026-08-17")["spo"]["regime"] == "мало данных"


def test_direction_without_phrases_is_distinguishable():
    # «Нет ряда» и «мало данных» — разные диагнозы: первое чинится семантикой,
    # второе — временем. Один вердикт на оба прячет дыру навсегда.
    out = demand_regime([_row("2026-08-17", "колледж", 100)], through_week="2026-08-17")
    assert out["school"]["regime"] == "нет ряда"
    assert out["spo"]["regime"] == "мало данных"


def test_directions_without_series_listed_separately():
    # Отдельная строка отчёта Э0: дыра в семантике спроса обязана быть
    # видимой, а не растворяться в общем словаре режимов.
    out = demand_regime(_flat(8), through_week="2026-07-22")
    assert directions_without_series(out) == sorted(DIRECTIONS_WITHOUT_SERIES)
    # school — самое свежее направление кабинета (запуск 14.08.2026), и
    # именно у него ряда нет.
    assert "school" in DIRECTIONS_WITHOUT_SERIES


def test_unknown_phrase_is_ignored_not_crashing():
    rows = [_row("2026-08-17", "автошкола", 100)]
    assert weekly_demand_by_direction(rows) == {}
