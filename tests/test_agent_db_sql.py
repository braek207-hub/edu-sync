from sync.agent.db import AGENT_DDL

REQUIRED_TABLES = [
    "edu_agent_facts",
    "edu_agent_facts_sliced",
    "edu_agent_objects",
    "edu_agent_search_queries",
    "edu_agent_settings_snapshot",
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
