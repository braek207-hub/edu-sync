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
