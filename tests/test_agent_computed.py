# -*- coding: utf-8 -*-
"""
tests/test_agent_computed.py — вычисляемые настройки автопилота.

Фундаментальный дефект, который здесь закрыт: конверсионность сегмента считалась
по ожидаемым оплатам, размазанным по ДОЛЕ КЛИКОВ. Подстановка одного в другое
давала всем сегментам среза одну и ту же конверсионность — «корректировка по
сегменту» сегменты не различала, различался только вес байесовского сжатия.

Отсюда состав проверок: конверсионность идёт от реальных конверсий отчёта,
разные сегменты получают РАЗНЫЕ корректировки, а вырожденные срезы (нет
конверсий / у всех конверсионность совпала) корректировок не дают вовсе и
называют причину.
"""

import sync.agent.computed as computed
from sync.agent.computed import (
    DEGENERATE_REASON,
    NO_CLICKS_REASON,
    NO_CONVERSIONS_REASON,
    bid_modifier_percent,
    compute_schedule,
    compute_segment_modifiers,
    prior_trials,
    shrink_ratio,
)


def _seg(key, clicks, conversions):
    return {"segment_kind": "device", "segment_key": key,
            "clicks": clicks, "conversions": conversions}


def test_prior_is_measured_in_events_not_in_trials():
    """Единица наблюдения у срезов разная: клик Директа у корректировок,
    визит сайта у расписания. Постоянный вес в наблюдениях нёс бы в этих
    срезах разный объём информации.

    При базовой конверсии 4 % — порядок величины кабинетов EDU (37 252
    достижения на 1 010 261 визит) — сохраняются прежние 50 наблюдений.
    """
    assert prior_trials(0.04) == 50.0
    # Срез с конверсией вдесятеро ниже требует вдесятеро больше наблюдений,
    # чтобы получить тот же вес: событий за ними столько же.
    assert prior_trials(0.004) == 500.0


def test_hour_with_few_events_is_shrunk_even_with_thousands_of_visits():
    """Час суток набирает визиты тысячами, и вес n/(n+50) давал 0.999 —
    сжатие было выключено там, где достижений меньше десятка.

    Проба 32579085232: у счётчика 95348914 самый слабый час даёт около
    девяти достижений при сотнях визитов, а коэффициент прыгал до 60-130.
    """
    weak = shrink_ratio(segment_conv=0.03, segment_n=600, base_conv=0.015)
    strong = shrink_ratio(segment_conv=0.03, segment_n=60_000, base_conv=0.015)
    # Как считалось раньше: априор в наблюдениях, независимо от конверсии.
    old_weak = shrink_ratio(segment_conv=0.03, segment_n=600, base_conv=0.015,
                            prior_weight=50.0)

    assert weak < old_weak     # слабый час сжимается сильнее, чем сжимался
    assert weak < strong       # и сильнее, чем час с сотнями достижений
    assert strong > 1.9        # объёмный час почти не сжат — так и должно быть


def test_explicit_prior_weight_still_wins():
    """Явно переданный вес остаётся главнее — на нём держатся разовые
    расчёты, где априор задан снаружи."""
    got = shrink_ratio(0.06, 50, 0.02, prior_weight=50.0)
    assert abs(got - 2.0) < 1e-9


def test_shrink_pulls_small_sample_to_base():
    # 2 наблюдения с конверсией втрое выше базы — почти полностью сжимается.
    out = shrink_ratio(segment_conv=0.06, segment_n=2, base_conv=0.02)
    assert 1.0 <= out <= 1.15


def test_shrink_trusts_large_sample():
    out = shrink_ratio(segment_conv=0.06, segment_n=5000, base_conv=0.02)
    assert out > 2.5


def test_shrink_returns_one_on_zero_support():
    assert shrink_ratio(0.06, 0, 0.02) == 1.0


def test_shrink_returns_one_on_zero_base():
    assert shrink_ratio(0.06, 100, 0.0) == 1.0


def test_modifier_percent_is_rounded_and_capped():
    assert bid_modifier_percent(1.3) == 30
    assert bid_modifier_percent(0.8) == -20
    assert bid_modifier_percent(3.0, cap=0.5) == 50     # потолок +50%
    assert bid_modifier_percent(0.1, cap=0.5) == -50    # пол −50%


def test_modifier_percent_neutral_is_zero():
    assert bid_modifier_percent(1.0) == 0


def test_compute_segment_modifiers_emits_rows():
    rows = [_seg("mobile", 50000, 1500), _seg("desktop", 10000, 100)]
    out, reason = compute_segment_modifiers(rows)
    assert reason is None
    assert {r["setting_key"] for r in out} == {"mobile", "desktop"}
    assert all(r["setting_kind"] == "bid_modifier:device" for r in out)
    assert all(isinstance(r["value"], (int, float)) for r in out)


def test_compute_segment_modifiers_skips_empty_segments():
    rows = [_seg("tv", 0, 0)]
    out, reason = compute_segment_modifiers(rows)
    assert out == []
    assert reason == NO_CLICKS_REASON


# --------------- дефект: сегменты не различались

def test_segments_with_different_conversion_get_different_modifiers():
    """Сильный и слабый сегменты обязаны разъехаться, причём в разные стороны.

    На старом коде (оплаты по доле кликов) конверсионность обоих равнялась
    средней по срезу, знак корректировки у обоих был один, и разъехаться они
    могли только модулем сжатия.
    """
    rows = [_seg("mobile", 20000, 1200), _seg("desktop", 20000, 200)]
    out, reason = compute_segment_modifiers(rows)

    assert reason is None
    value = {r["setting_key"]: r["value"] for r in out}
    assert value["mobile"] != value["desktop"]
    # Сегмент выше базы — вверх, сегмент ниже базы — вниз.
    assert value["mobile"] > 0 > value["desktop"]


def test_conversion_rate_comes_from_conversions_not_clicks_share():
    """Меньший по кликам, но более конверсионный сегмент выигрывает у большего.

    Ровно это невозможно, когда оплаты раздаются по доле кликов: там больше
    кликов = больше «оплат», и порядок сегментов задают клики, а не качество.
    """
    rows = [_seg("mobile", 40000, 400), _seg("desktop", 4000, 400)]
    out, _ = compute_segment_modifiers(rows)

    value = {r["setting_key"]: r["value"] for r in out}
    assert value["desktop"] > value["mobile"]


def test_slice_without_conversions_is_refused_with_reason():
    """Нули вместо конверсий (цели не настроены / отчёт их не отдал) — не повод
    выдать нулевые корректировки: расчёт отказывается и называет причину."""
    rows = [_seg("mobile", 50000, 0), _seg("desktop", 10000, 0)]
    out, reason = compute_segment_modifiers(rows)

    assert out == []
    assert reason == NO_CONVERSIONS_REASON


def test_degenerate_slice_is_refused_with_reason():
    """Одинаковая конверсионность у всех сегментов — данные не различают сегменты.

    Это сигнатура починенного дефекта: если она снова появится в данных,
    корректировок не будет, а причина будет названа.
    """
    rows = [_seg("mobile", 50000, 1000), _seg("desktop", 10000, 200)]
    out, reason = compute_segment_modifiers(rows)

    assert out == []
    assert reason == DEGENERATE_REASON


def test_degenerate_check_tolerates_double_noise():
    """Размазывание по доле кликов даёт совпадение с точностью до шума double —
    проверка обязана ловить и его, иначе дефект пройдёт мимо неё."""
    total_clicks = 60000
    total_expected = 2000.0
    rows = []
    for key, clicks in (("mobile", 50000), ("desktop", 10000)):
        share = clicks / total_clicks
        rows.append(_seg(key, clicks, total_expected * share))

    out, reason = compute_segment_modifiers(rows)
    assert out == []
    assert reason == DEGENERATE_REASON


def test_single_supported_segment_is_refused():
    """Порог прошёл один сегмент — сравнивать его не с чем.

    Старый код молча выдавал на такой срез нулевую корректировку: база почти
    совпадает с самим сегментом, отношение ≈ 1. Нуль без информации — это и есть
    вырождение, и оно должно называться, а не записываться в кабинет.
    """
    rows = [_seg("mobile", 20000, 1200), _seg("tv", 10, 0)]
    out, reason = compute_segment_modifiers(rows)

    assert out == []
    assert reason == DEGENERATE_REASON


# --------------- расписание: та же механика на достижениях целей из Метрики

def test_compute_schedule_covers_all_hours_present():
    # sum_p_pay почасового профиля — ym:s:sumGoalReachesAny по часу
    # (sync/agent/metrika.py::fetch_hourly_profile), то есть реальные достижения
    # целей, а не размазанная по визитам величина.
    # Разброс часов должен быть РЕАЛЬНЫМ, а не на уровне биномиального шума:
    # приор теперь выводится из данных (empirical_prior_trials), и профиль,
    # чей разброс объясняется шумом, честно сжимается в нули.
    rows = [
        {"segment_kind": "hour", "segment_key": str(h), "leads": 100,
         "sum_p_pay": 100.0 + 20.0 * h, "clicks": 5000}
        for h in range(24)
    ]
    out, reason = compute_schedule(rows)
    assert reason is None
    assert len(out) == 24
    assert all(r["setting_kind"] == "schedule:hour" for r in out)
    # Часы с разной конверсионностью обязаны получить разные значения.
    assert len({r["value"] for r in out}) > 1


def test_schedule_refuses_degenerate_profile():
    rows = [
        {"segment_kind": "hour", "segment_key": str(h), "leads": 100,
         "sum_p_pay": 100.0, "clicks": 5000}
        for h in range(24)
    ]
    out, reason = compute_schedule(rows)
    assert out == []
    assert reason == DEGENERATE_REASON


def test_cap_constants_carry_their_unit_in_the_name():
    # Одноимённые потолки в РАЗНЫХ шкалах уже роняли ветку смешением единиц:
    # здесь доля (0.5), в рельсах движка записи — проценты (50). Пока имена
    # совпадали, спутать их можно было только внимательностью, а единица
    # обязана читаться из имени.
    from sync.agent.writer import guardrails

    assert not hasattr(computed, "MODIFIER_CAP"), (
        "имя без единицы измерения вернулось — рядом живёт одноимённая "
        "константа в процентах (guardrails.MODIFIER_CAP)")
    assert computed.MODIFIER_CAP_RATIO == 0.5
    assert guardrails.MODIFIER_CAP == 50
    assert computed.MODIFIER_CAP_RATIO * 100 == guardrails.MODIFIER_CAP


# ------------------- эмпирический Байес: приор из самих данных


def test_prior_comes_from_between_segment_spread():
    # PRIOR_EVENTS=2 «подогнан под легаси» и не знает, различаются ли сегменты
    # среза вообще. Приор обязан выводиться из данных: разброс сегментов сверх
    # биномиального шума (τ̂²) — это и есть сила сигнала. Срез, где сегменты
    # различаются вдвое, доверяет им; срез, где разброс объясняется шумом,
    # сжимает почти к базе.
    noisy = [_seg("a", 1000, 40), _seg("b", 1000, 42), _seg("c", 1000, 38)]
    real = [_seg("a", 1000, 20), _seg("b", 1000, 40), _seg("c", 1000, 60)]
    assert computed.empirical_prior_trials(noisy, "conversions") > \
        computed.empirical_prior_trials(real, "conversions")


def test_prior_is_infinite_when_segments_do_not_differ_beyond_noise():
    # Разброс целиком объясняется биномиальным шумом — межсегментной
    # дисперсии нет, сигнала нет, сжатие полное.
    same = [_seg("a", 1000, 40), _seg("b", 1000, 40), _seg("c", 1000, 40)]
    assert computed.empirical_prior_trials(same, "conversions") == float("inf")


def test_noise_only_slice_gets_near_zero_modifiers():
    # Сквозная проверка: срез из одного шума не должен давать корректировок
    # заметного размера, сколько бы кликов в нём ни было.
    rows, reason = compute_segment_modifiers(
        [_seg("a", 5000, 200), _seg("b", 5000, 205), _seg("c", 5000, 195)])
    assert reason is None
    assert all(abs(r["value"]) <= 1 for r in rows)


def test_real_spread_survives_the_empirical_prior():
    rows, reason = compute_segment_modifiers(
        [_seg("a", 5000, 100), _seg("b", 5000, 200), _seg("c", 5000, 300)])
    assert reason is None
    assert max(abs(r["value"]) for r in rows) >= 30
