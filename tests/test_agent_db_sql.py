import os
from datetime import date, timedelta

import pytest

import sync.agent.db as agent_db
from sync.agent.db import AGENT_DDL

REQUIRED_TABLES = [
    "edu_agent_facts",
    "edu_agent_facts_sliced",
    "edu_agent_objects",
    "edu_agent_search_queries",
    "edu_agent_settings_snapshot",
    "edu_agent_behavior",
    "edu_agent_guard",
    "edu_agent_holdout",
    "edu_agent_experiments",
    "edu_agent_computed_settings",
    "edu_agent_profile",
]


def test_all_required_tables_present():
    ddl = "\n".join(AGENT_DDL)
    for table in REQUIRED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl, table


def test_ddl_is_idempotent_only():
    # Ни одного разрушительного выражения: миграция должна быть безопасна к повтору.
    ddl = "\n".join(AGENT_DDL).upper()
    for forbidden in ("DROP TABLE", "TRUNCATE", "DELETE FROM"):
        assert forbidden not in ddl


def test_experiments_table_has_reliability_class():
    ddl = "\n".join(AGENT_DDL)
    assert "reliability_class" in ddl


def test_facts_primary_key_is_date_campaign():
    ddl = "\n".join(AGENT_DDL)
    assert "PRIMARY KEY (fact_date, campaign_id)" in ddl


def test_sliced_facts_are_weekly_not_daily():
    # Срезы храним недельно: подневное декартово произведение
    # кампания × день × регион × площадка × устройство — это 17,6 млн комбинаций.
    ddl = "\n".join(AGENT_DDL)
    assert "week_start" in ddl
    assert "PRIMARY KEY (week_start, campaign_id, slice_kind, slice_key)" in ddl


def test_facts_have_crm_depth_columns():
    ddl = "\n".join(AGENT_DDL)
    for column in ("connected_leads", "deals", "mins_to_connection_sum", "days_to_pay_sum"):
        assert column in ddl, column


def test_objects_snapshot_versioned_by_hash():
    # Снимок структуры пишется новой версией только при изменении:
    # ежедневная копия 55k объектов дала бы 5 млн строк за квартал.
    ddl = "\n".join(AGENT_DDL)
    assert "content_hash" in ddl


# ------------------------------- вычисленные настройки живут ПО КАБИНЕТАМ
# Дефект 1: расчёт шёл в цикле по кабинетам, а писался с захардкоженным
# идентификатором объекта — одним на всех. Первичный ключ совпадал, запись
# построчная, поэтому дубликаты не падали, а тихо перетирали друг друга:
# выживали числа последнего успевшего кабинета. Загрузчик движка записи не
# фильтровал по кабинету вообще и раскатывал схлопнутый набор на все.


def test_upsert_computed_settings_requires_account(monkeypatch):
    # object_id без значения по умолчанию: забытый кабинет обязан уронить
    # вызов, а не подставить общий идентификатор и склеить кабинеты.
    monkeypatch.setattr(agent_db, "_batch", lambda sql, rows, **kw: len(rows))
    with pytest.raises(TypeError):
        agent_db.upsert_computed_settings([], calc_date="2026-08-19")


def test_upsert_computed_settings_stamps_account_on_every_row(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        agent_db, "_batch",
        lambda sql, rows, **kw: captured.update(rows=rows) or len(rows),
    )
    rows = [{"setting_kind": "bid_modifier:device", "setting_key": "MOBILE",
             "value": 10.0, "support_n": 500, "raw_value": 1.1},
            {"setting_kind": "bid_modifier:gender", "setting_key": "GENDER_MALE",
             "value": -10.0, "support_n": 500, "raw_value": 0.9}]

    agent_db.upsert_computed_settings(rows, calc_date="2026-08-19", object_id="acc-1")

    assert {r["object_id"] for r in captured["rows"]} == {"acc-1"}
    assert {r["object_level"] for r in captured["rows"]} == {"account"}


def test_load_latest_computed_settings_filters_by_account(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        agent_db, "_fetch_dicts",
        lambda sql, params=(): captured.update(sql=sql, params=params) or [],
    )

    agent_db.load_latest_computed_settings("acc-1")

    # Фильтр обязан стоять И в основной выборке, И в подзапросе MAX(calc_date):
    # без второго свежий расчёт одного кабинета прятал бы вчерашний расчёт
    # другого — выборка по MAX по всей таблице просто не находила бы строк.
    assert captured["sql"].count("object_id = %s") == 2
    assert captured["params"] == ("account", "acc-1", "account", "acc-1")


# ---------------------------------- логин кабинета: одна нормализация на оба конца
# Дефект: расчёт (agent_e0) обрезал пробелы у логина, движок записи (agent_e1)
# проверял обрезанное значение, а в список кабинетов клал сырое. Логин — это
# ключ object_id таблицы вычисленных настроек: пробел по краям любого логина в
# DIRECT_CLIENTS_JSON разводил запись и чтение по разным ключам, и прогон молча
# рапортовал, что применять нечего.


def test_calculation_and_writer_derive_the_same_login_from_env(monkeypatch):
    import json

    import sync.agent_e0 as agent_e0
    import sync.agent_e1 as agent_e1

    monkeypatch.setenv("DIRECT_CLIENTS_JSON", json.dumps(
        [{"login": " acc-1 ", "goal_ids": ["1"]}, {"login": "acc-2\t", "goal_ids": []}]))

    calc = [c["login"] for c in agent_e0._direct_clients()]
    writer = [c["login"] for c in agent_e1._clients()]

    assert calc == writer == ["acc-1", "acc-2"]


def test_computed_settings_written_under_normalized_key(monkeypatch):
    captured = {}
    monkeypatch.setattr(agent_db, "_batch",
                        lambda sql, payload: captured.update(payload=payload) or len(payload))

    agent_db.upsert_computed_settings(
        [{"setting_kind": "bid_modifier:device", "setting_key": "MOBILE",
          "value": 30.0, "support_n": 100, "raw_value": 1.3}],
        calc_date="2026-08-19", object_id=" acc-1 ",
    )

    assert captured["payload"][0]["object_id"] == "acc-1"


def test_computed_settings_read_under_normalized_key(monkeypatch):
    captured = {}
    monkeypatch.setattr(agent_db, "_fetch_dicts",
                        lambda sql, params: captured.update(params=params) or [])

    agent_db.load_latest_computed_settings(" acc-1 ")

    assert captured["params"] == ("account", "acc-1", "account", "acc-1")


# --------------- живое исполнение выборки ширины витрины
# GROUPING SETS ((fact_date), ()) — единственное место в агенте с таким
# синтаксисом, и от его итоговой строки (fact_date = NULL) зависит знаменатель
# гейта свежести. Ошибись в нём — гейт получит campaigns_total = 0, порог
# обнулится, и витрина будет считаться здоровой всегда. В тексте запроса это
# не видно, поэтому здесь запрос исполняется по-настоящему.

live_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL")


@live_db
def test_live_mart_day_breadth_returns_days_and_a_wider_total():
    today = date.today()
    out = agent_db.load_mart_day_breadth(
        (today - timedelta(days=30)).isoformat(), today.isoformat())

    assert set(out) == {"days", "campaigns_total"}
    assert isinstance(out["campaigns_total"], int)
    assert all(isinstance(v, int) and v > 0 for v in out["days"].values())
    # Итог — РАЗНЫЕ кампании за всё окно, поэтому он не меньше любого дня.
    # Максимумом по дням его считать нельзя: в разные дни кампании разные.
    if out["days"]:
        assert out["campaigns_total"] >= max(out["days"].values())
    else:
        assert out["campaigns_total"] == 0
    # Итоговая строка не должна протечь в дни отдельным ключом None.
    assert None not in out["days"]


@live_db
def test_live_mart_day_breadth_is_empty_outside_the_mart():
    # Окно в будущем строк не даёт — и запрос обязан вернуть честные нули,
    # а не упасть и не отдать итоговую строку без дней.
    future = date.today() + timedelta(days=365)
    out = agent_db.load_mart_day_breadth(
        future.isoformat(), (future + timedelta(days=7)).isoformat())

    assert out == {"days": {}, "campaigns_total": 0}


# --------------- граница зрелости CRM: важен ИСТОЧНИК, а не только число

def test_maturity_asks_crm_not_the_facts_mart(monkeypatch):
    """Граница берётся из CRM, а не из витрины фактов.

    Разница не косметическая. В edu_agent_facts лежат дни, собранные по
    расходу Директа, — включая те, где лидов ещё нет вовсе (19.08.2026:
    927 945 рублей, 0 лидов). Взяв максимум оттуда, граница накроет ровно те
    дни, от которых обязана защищать, и защита исчезнет молча — окно снова
    станет включать бесконечный CPA.
    """
    seen = {}
    monkeypatch.setattr(agent_db, "_fetch_dicts",
                        lambda sql, params=(): seen.update({"sql": sql}) or [{"d": None}])

    agent_db.crm_maturity_date()

    assert "crm_lead_details" in seen["sql"]
    assert "created_date" in seen["sql"]
    assert "edu_agent_facts" not in seen["sql"], (
        "граница из витрины фактов накроет дни с расходом и нулём лидов")


def test_maturity_is_none_when_crm_is_empty(monkeypatch):
    # Пустая CRM — это «наблюдать не по чему», а не «граница сегодня».
    monkeypatch.setattr(agent_db, "_fetch_dicts", lambda sql, params=(): [{"d": None}])
    assert agent_db.crm_maturity_date() is None

    monkeypatch.setattr(agent_db, "_fetch_dicts", lambda sql, params=(): [])
    assert agent_db.crm_maturity_date() is None


def test_maturity_accepts_both_date_and_string(monkeypatch):
    # Драйвер отдаёт date, но через REST/JSON та же величина приходит строкой.
    from datetime import date as _date

    monkeypatch.setattr(agent_db, "_fetch_dicts",
                        lambda sql, params=(): [{"d": _date(2026, 8, 18)}])
    assert agent_db.crm_maturity_date() == _date(2026, 8, 18)

    monkeypatch.setattr(agent_db, "_fetch_dicts",
                        lambda sql, params=(): [{"d": "2026-08-18"}])
    assert agent_db.crm_maturity_date() == _date(2026, 8, 18)


@live_db
def test_live_maturity_is_not_ahead_of_the_facts_mart():
    """Живая проверка на боевых данных: граница CRM отстаёт от витрины фактов.

    Именно это расхождение и есть лаг: расход за вчера в витрине уже есть, а
    лидов за него ещё нет. Обгони граница витрину — значит запрос смотрит не
    туда, и защита не работает.
    """
    from datetime import date as _date

    maturity = agent_db.crm_maturity_date()
    assert maturity is not None, "в CRM нет ни одного лида"

    rows = agent_db._fetch_dicts("SELECT MAX(fact_date) AS d FROM edu_agent_facts")
    facts_through = rows[0]["d"] if rows else None
    if facts_through is not None:
        if not isinstance(facts_through, _date):
            facts_through = _date.fromisoformat(str(facts_through))
        assert maturity <= facts_through
    assert maturity <= _date.today()


def test_maturity_needs_a_full_day_not_a_single_early_lead(monkeypatch):
    # MAX(created_date) объявлял день зрелым по ОДНОМУ раннему лиду: граница
    # уезжала вперёд, в окна наблюдения попадали дни, где CRM ещё почти
    # пуста, и CPA этих дней завышен всегда в одну сторону. Зрелым считается
    # последний день, набравший заметную долю типичного дня.
    from datetime import date as _date

    days = [{"d": _date(2026, 8, 1 + i), "n": 100} for i in range(18)]
    days.append({"d": _date(2026, 8, 19), "n": 3})   # день пришёл едва начатым
    monkeypatch.setattr(agent_db, "_fetch_dicts", lambda sql, params=(): days)
    assert agent_db.crm_maturity_date() == _date(2026, 8, 18)


def test_maturity_takes_the_last_full_day(monkeypatch):
    from datetime import date as _date

    days = [{"d": _date(2026, 8, 1 + i), "n": 100} for i in range(19)]
    monkeypatch.setattr(agent_db, "_fetch_dicts", lambda sql, params=(): days)
    assert agent_db.crm_maturity_date() == _date(2026, 8, 19)


def test_direct_rows_carry_conversions(monkeypatch):
    # Конверсии целей Директа нужны рычагу целевого CPA (Э3.5) — они лежат в
    # том же источнике, что расход, и обязаны ехать одним запросом.
    seen = {}
    monkeypatch.setattr(agent_db, "_fetch_dicts",
                        lambda sql, params=(): seen.update({"sql": sql}) or [])
    agent_db.load_direct_rows("2026-08-01", "2026-08-07")
    assert "conversions" in seen["sql"]


def test_facts_upsert_writes_conversions():
    assert "conversions" in agent_db.UPSERT_FACTS_SQL
    assert "conversions = EXCLUDED.conversions" in agent_db.UPSERT_FACTS_SQL
