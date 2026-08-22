from sync.agent.metrika import parse_campaign_behavior, parse_hourly


def test_hourly_parses_hour_and_metrics():
    data = {"data": [
        {"dimensions": [{"name": "09:00"}], "metrics": [1000.0, 25.0]},
        {"dimensions": [{"name": "10:00"}], "metrics": [1500.0, 45.0]},
    ]}
    out = parse_hourly(data)
    assert [r["segment_key"] for r in out] == ["9", "10"]
    assert out[0]["clicks"] == 1000
    assert out[0]["sum_p_pay"] == 25.0
    assert all(r["segment_kind"] == "hour" for r in out)


def test_hourly_handles_midnight_and_plain_numbers():
    data = {"data": [
        {"dimensions": [{"name": "00"}], "metrics": [10.0, 1.0]},
        {"dimensions": [{"name": "23"}], "metrics": [20.0, 2.0]},
    ]}
    out = parse_hourly(data)
    assert [r["segment_key"] for r in out] == ["0", "23"]


def test_hourly_sorted_numerically_not_lexically():
    data = {"data": [
        {"dimensions": [{"name": "21"}], "metrics": [1.0, 0.0]},
        {"dimensions": [{"name": "3"}], "metrics": [1.0, 0.0]},
    ]}
    assert [r["segment_key"] for r in parse_hourly(data)] == ["3", "21"]


def test_hourly_skips_rows_without_metrics():
    data = {"data": [{"dimensions": [{"name": "09"}], "metrics": []}]}
    assert parse_hourly(data) == []


def test_behavior_converts_rates_to_counts():
    # Храним суммы и счётчики: среднее по среднему не складывается
    # при перегруппировке по неделям и направлениям.
    data = {"data": [
        {"dimensions": [{"name": "vuz / ВПО / Поиск"}], "metrics": [200.0, 25.0, 3.5, 120.0]},
    ]}
    out = parse_campaign_behavior(data)
    assert len(out) == 1
    row = out[0]
    # Метрика отдаёт ИМЯ кампании, не Id — привязка к Id делается снаружи.
    assert row["campaign_name"] == "vuz / ВПО / Поиск"
    assert row["visits"] == 200
    assert row["bounces"] == 50          # 25% от 200
    assert row["pageviews"] == 700       # 3.5 × 200
    assert row["visit_seconds"] == 24000  # 120 × 200


def test_behavior_skips_placeholder_campaign():
    data = {"data": [
        {"dimensions": [{"name": "не задано"}], "metrics": [10.0, 0.0, 1.0, 5.0]},
        {"dimensions": [{"name": "vuz / СПО"}], "metrics": [10.0, 0.0, 1.0, 5.0]},
    ]}
    assert [r["campaign_name"] for r in parse_campaign_behavior(data)] == ["vuz / СПО"]


def test_behavior_skips_incomplete_rows():
    data = {"data": [{"dimensions": [{"name": "vuz / СПО"}], "metrics": [10.0, 0.0]}]}
    assert parse_campaign_behavior(data) == []


def test_empty_response():
    assert parse_hourly({}) == []
    assert parse_campaign_behavior({}) == []


# --------------- расписание считается по ЛИДАМ, а не по «любой цели»
# Замер 22.08.2026 (счётчик 98627983, окно 90 дней) показал, что
# ym:s:sumGoalReachesAny — это не заявки: 0.38 достижения на визит складывались
# на ~70 % из микроцелей «2 минуты на сайте», «глубина прокрутки», «прокрутка:
# середина блока», а заявки давали 2 %. По суткам они расходятся ПРОТИВОПОЛОЖНО:
# прокрутка ночью 1.17–1.49 к дню, «CRM: новая заявка» — 0.45, «новая сделка» —
# 0.41. Расписание по Any поднимало ставку на 3:00–7:00 (+13, +27, +21 %) — на
# часы, где заявок вдвое меньше. Тесты ниже держат метрику на месте.

import pytest

import sync.agent.metrika as metrika
from sync.agent.metrika import hourly_metrics


def test_request_asks_for_goals_and_never_for_any_goal(monkeypatch):
    """Проверка по УХОДЯЩЕМУ ЗАПРОСУ, а не по тексту файла.

    Тест «в исходнике нет sumGoalReachesAny» был бы беззубым: в модуле эта
    строка законно живёт в объяснении «почему так больше нельзя», и отличить её
    от вернувшейся боевой строки по тексту нельзя. Смотрим на то, что реально
    уходит в Метрику.
    """
    sent = {}
    monkeypatch.setenv("YM_TOKEN", "t")
    monkeypatch.setattr(metrika, "_metrica_get",
                        lambda params, token: sent.update(params) or {"data": []})

    metrika.fetch_hourly_profile(98627983, "2026-01-01", "2026-03-31", [317, 42])

    assert "sumGoalReachesAny" not in sent["metrics"]
    assert sent["metrics"] == "ym:s:visits,ym:s:goal42reaches,ym:s:goal317reaches"
    assert sent["dimensions"] == "ym:s:hour"
    assert sent["ids"] == 98627983


def test_metrics_ask_one_column_per_goal():
    assert hourly_metrics([17, 5]) == "ym:s:visits,ym:s:goal5reaches,ym:s:goal17reaches"


def test_metrics_are_stable_regardless_of_input_order_and_duplicates():
    # Один и тот же набор целей обязан давать один и тот же запрос: иначе
    # два прогона подряд дают разные колонки и профиль «дрожит» без причины.
    assert hourly_metrics([5, 17, 5]) == hourly_metrics([17, 5])


def test_profile_without_goals_is_refused_not_silently_any():
    """Нет целей — отказ, а не тихий откат к «любой цели».

    Тихий откат и был исходным дефектом: расписание считалось, выглядело
    правдоподобно и было посчитано не по тому.
    """
    with pytest.raises(ValueError, match="целей"):
        metrika.fetch_hourly_profile(1, "2026-01-01", "2026-01-02", [])


def test_all_goal_columns_are_summed_not_just_the_first():
    """Целей в запросе несколько — час обязан получить их СУММУ.

    Взять metrics[1] значило бы считать расписание по одной цели из четырёх,
    молча и без следа в отчёте.
    """
    data = {"data": [{"dimensions": [{"name": "09"}], "metrics": [100.0, 3.0, 4.0, 5.0]}]}
    out = parse_hourly(data)

    assert out[0]["sum_p_pay"] == 12.0
    assert out[0]["leads"] == 12
