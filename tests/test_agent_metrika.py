from sync.agent.metrika import (
    parse_campaign_behavior,
    parse_hourly_by_goal,
    pick_lead_goal,
    profile_rows,
)


def _hourly(data, goal_ids=(1,)):
    """Разбор + сборка строк по ведущей цели — путь fetch_hourly_profile."""
    visits, reaches = parse_hourly_by_goal(data, list(goal_ids))
    goal = pick_lead_goal(reaches)
    return profile_rows(visits, reaches[goal] if goal is not None else {})


def test_hourly_parses_hour_and_metrics():
    data = {"data": [
        {"dimensions": [{"name": "09:00"}], "metrics": [1000.0, 25.0]},
        {"dimensions": [{"name": "10:00"}], "metrics": [1500.0, 45.0]},
    ]}
    out = _hourly(data)
    assert [r["segment_key"] for r in out] == ["9", "10"]
    assert out[0]["clicks"] == 1000
    assert out[0]["sum_p_pay"] == 25.0
    assert all(r["segment_kind"] == "hour" for r in out)


def test_hourly_handles_midnight_and_plain_numbers():
    data = {"data": [
        {"dimensions": [{"name": "00"}], "metrics": [10.0, 1.0]},
        {"dimensions": [{"name": "23"}], "metrics": [20.0, 2.0]},
    ]}
    out = _hourly(data)
    assert [r["segment_key"] for r in out] == ["0", "23"]


def test_hourly_sorted_numerically_not_lexically():
    data = {"data": [
        {"dimensions": [{"name": "21"}], "metrics": [1.0, 0.0]},
        {"dimensions": [{"name": "3"}], "metrics": [1.0, 0.0]},
    ]}
    assert [r["segment_key"] for r in _hourly(data)] == ["3", "21"]


def test_hourly_skips_rows_without_metrics():
    data = {"data": [{"dimensions": [{"name": "09"}], "metrics": []}]}
    assert _hourly(data) == []


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
    assert _hourly({}) == []
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


def test_goal_columns_are_not_summed_because_goals_duplicate_each_other():
    """Целей в запросе несколько — но складывать их колонки нельзя.

    Проба 32579085232: у счётчика 96526110 «Страница "Спасибо" VseKolledzhi» и
    «Страницы спасибо» дали ПОБУКВЕННО совпадающие векторы 24 часов
    (37 252 = 37 252) — одно действие под двумя идентификаторами. Рядом
    «Автоцель: отправил контактные данные» — тот же поступок третьим способом.
    Сумма считает одну заявку дважды, а на счётчике 98627983 подмешивает к ней
    ступенчатую «CRM: Заказ оплачен».
    """
    data = {"data": [{"dimensions": [{"name": "09"}], "metrics": [100.0, 3.0, 4.0, 5.0]}]}
    out = _hourly(data, goal_ids=(1, 2, 3))

    # Ведущая цель — третья колонка (5.0), а не сумма 12.0.
    assert out[0]["sum_p_pay"] == 5.0
    assert out[0]["leads"] == 5


def test_identical_goals_give_the_same_answer_whichever_wins():
    """Дубли неразличимы по построению — выбор между ними обязан быть
    воспроизводим, иначе профиль дрожит от прогона к прогону."""
    data = {"data": [
        {"dimensions": [{"name": "09"}], "metrics": [100.0, 7.0, 7.0]},
        {"dimensions": [{"name": "10"}], "metrics": [100.0, 9.0, 9.0]},
    ]}
    visits, reaches = parse_hourly_by_goal(data, [369313502, 330070378])

    assert pick_lead_goal(reaches) == 330070378   # меньший идентификатор
    assert reaches[330070378] == reaches[369313502]


def test_stepped_goal_loses_to_the_application_it_belongs_to():
    """«Заказ оплачен» — подмножество заявки. Числителем обязана быть заявка:
    иначе расписание считается по оплатам, которых в разы меньше и которые
    созревают позже."""
    data = {"data": [
        {"dimensions": [{"name": "09"}], "metrics": [100.0, 33.0, 4.0]},
    ]}
    _visits, reaches = parse_hourly_by_goal(data, [349122551, 541664135])

    assert pick_lead_goal(reaches) == 349122551


def test_no_reaches_at_all_is_none_not_an_arbitrary_goal():
    """Ни одного достижения — считать не по чему. Вернуть первую попавшуюся
    цель значило бы построить расписание на двадцати четырёх нулях."""
    _visits, reaches = parse_hourly_by_goal(
        {"data": [{"dimensions": [{"name": "09"}], "metrics": [100.0, 0.0, 0.0]}]}, [1, 2])

    assert pick_lead_goal(reaches) is None


# --------------- лимит Метрики: 20 метрик на запрос (код 4015)
# Опыт 32577516946: у счётчика 98627983 девяносто целей, запрос всех сразу
# отвергается целиком. Срезать хвост нельзя — это тот же класс дефекта, что
# профиль по «любой цели»: считается не то, а выглядит посчитанным.

from sync.agent.metrika import MAX_GOALS_PER_REQUEST, goal_batches


def test_batches_fit_the_api_limit():
    batches = goal_batches(list(range(1, 91)))

    assert all(len(b) <= MAX_GOALS_PER_REQUEST for b in batches)
    # Одну метрику из двадцати занимают визиты.
    assert MAX_GOALS_PER_REQUEST == 19
    assert max(len(hourly_metrics(b).split(",")) for b in batches) <= 20


def test_no_goal_is_dropped_by_batching():
    goals = list(range(1, 91))
    assert sorted(g for b in goal_batches(goals) for g in b) == goals


def test_single_batch_when_goals_fit():
    assert goal_batches([3, 1, 2]) == [[1, 2, 3]]


def test_visits_are_not_multiplied_across_batches(monkeypatch):
    """Визиты в каждой порции ОДНИ И ТЕ ЖЕ — это метрика часа, не цели.

    Сложить их значило бы раздуть знаменатель во столько раз, сколько было
    порций, и конверсионность часа упала бы кратно — молча и правдоподобно.
    """
    monkeypatch.setenv("YM_TOKEN", "t")
    monkeypatch.setattr(metrika, "_metrica_get", lambda params, token: {
        "data": [{"dimensions": [{"name": "09"}], "metrics": [1000.0] + [7.0] * (
            len(params["metrics"].split(",")) - 1)}]})

    rows, chosen = metrika.fetch_hourly_profile(
        1, "2026-01-01", "2026-03-31", list(range(1, 41)))

    assert len(rows) == 1
    assert rows[0]["clicks"] == 1000       # не 3000 на трёх порциях
    assert chosen["goals_offered"] == 40


def test_goal_from_a_later_batch_can_win(monkeypatch):
    """Ведущая цель ищется по ВСЕМ порциям, а не внутри первой: иначе набор
    из сорока целей молча считался бы по девятнадцати."""
    monkeypatch.setenv("YM_TOKEN", "t")

    def _get(params, token):
        goals = [int(m.split("goal")[1].split("reaches")[0])
                 for m in params["metrics"].split(",")[1:]]
        return {"data": [{"dimensions": [{"name": "09"}],
                          "metrics": [1000.0] + [100.0 if g == 40 else 1.0 for g in goals]}]}

    monkeypatch.setattr(metrika, "_metrica_get", _get)

    _rows, chosen = metrika.fetch_hourly_profile(
        1, "2026-01-01", "2026-03-31", list(range(1, 41)))

    assert chosen["goal_id"] == 40


def test_profile_is_counted_on_advertising_traffic_only(monkeypatch):
    """Результат накладывается на показы Директа — считать его по всему сайту
    значит выдавать за ценность часа состав его источников.

    Замер 32579085232: доля рекламы в визитах ночью 0.956-0.984 против
    0.976-0.983 днём. Примесь мала, но смещена в одну сторону, и соседняя
    fetch_campaign_behavior фильтр ставила с самого начала.
    """
    sent = {}
    monkeypatch.setenv("YM_TOKEN", "t")
    monkeypatch.setattr(metrika, "_metrica_get",
                        lambda params, token: sent.update(params) or {"data": []})

    metrika.fetch_hourly_profile(1, "2026-01-01", "2026-03-31", [317])

    assert sent["filters"] == metrika.AD_FILTER
    assert "lastTrafficSource" in sent["filters"]


def test_profile_carries_the_winning_goal_alone_not_the_sum(monkeypatch):
    """Сквозная проверка числителя.

    Тесты pick_lead_goal по отдельности зелёные и на сумме тоже: мутация
    «вернуть сумму всех колонок» их пережила. Здесь смотрим на то, что
    fetch_hourly_profile реально кладёт в строку часа.
    """
    monkeypatch.setenv("YM_TOKEN", "t")
    monkeypatch.setattr(metrika, "_metrica_get", lambda params, token: {
        "data": [{"dimensions": [{"name": "09"}], "metrics": [1000.0, 5.0, 40.0]}]})

    rows, chosen = metrika.fetch_hourly_profile(1, "2026-01-01", "2026-03-31", [11, 22])

    assert chosen["goal_id"] == 22
    # 40 — достижения ведущей цели. 45 означало бы, что дубль сложен с ней.
    assert rows[0]["sum_p_pay"] == 40.0
    assert rows[0]["leads"] == 40


def test_chosen_goal_is_reported_not_silent(monkeypatch):
    """«Самая массовая колонка» без имени рядом — тот же приём, каким сюда
    однажды пролезла микроцель прокрутки. Выбор обязан быть виден в отчёте."""
    monkeypatch.setenv("YM_TOKEN", "t")
    monkeypatch.setattr(metrika, "_metrica_get", lambda params, token: {
        "data": [{"dimensions": [{"name": "09"}], "metrics": [1000.0, 5.0, 40.0]}]})

    _rows, chosen = metrika.fetch_hourly_profile(1, "2026-01-01", "2026-03-31", [11, 22])

    assert chosen["goal_id"] == 22
    assert chosen["reaches"] == 40
    assert chosen["goals_offered"] == 2


def test_ninety_goals_go_out_in_five_requests(monkeypatch):
    """Сквозная половина: порции обязаны реально уйти отдельными запросами."""
    sent = []
    monkeypatch.setenv("YM_TOKEN", "t")
    monkeypatch.setattr(metrika, "_metrica_get", lambda params, token: sent.append(
        params["metrics"]) or {"data": [{"dimensions": [{"name": "09"}],
                                         "metrics": [100.0, 1.0]}]})

    rows, chosen = metrika.fetch_hourly_profile(
        1, "2026-01-01", "2026-03-31", list(range(1, 91)))

    assert len(sent) == 5
    assert all(len(m.split(",")) <= 20 for m in sent)
    # Час один, визиты не размножились, ни одна цель не потеряна по дороге.
    assert len(rows) == 1 and rows[0]["clicks"] == 100
    assert chosen["goals_offered"] == 90
