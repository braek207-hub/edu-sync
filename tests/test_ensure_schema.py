# -*- coding: utf-8 -*-
"""ensure_schema не трогает таблицы, если каталог уже содержит всю схему.

09.10 edu_visits упал с «deadlock detected»: ensure_schema на каждой записи гонял
ALTER TABLE по шести горячим таблицам (AccessExclusiveLock даже на «ADD COLUMN IF NOT
EXISTS» существующей колонки), и читатель дашборда замкнул цикл блокировок.
"""

import re
from contextlib import contextmanager

import pytest

from sync import db


class FakeCatalog:
    """Каталог в памяти: отвечает на запросы _ddl_done, DDL записывает как исполненный."""

    def __init__(self, columns, relations, rls, constraints):
        self.columns = columns  # таблица -> set колонок
        self.relations = relations  # set таблиц и индексов
        self.rls = rls  # set таблиц с RLS
        self.constraints = constraints  # set (таблица, имя)
        self.ddl = []
        self._row = None

    def execute(self, sql, params=()):
        if "information_schema.columns" in sql:
            self._rows = [(c,) for c in self.columns.get(params[0], ())]
        elif "relrowsecurity" in sql:
            self._row = (params[0].split(".", 1)[1] in self.rls,)
        elif "pg_constraint" in sql:
            table = params[0].split(".", 1)[1]
            self._row = ((table, params[1]) not in self.constraints,)
        elif "to_regclass" in sql:
            self._row = (params[0].split(".", 1)[1] in self.relations,)
        else:
            self.ddl.append(sql)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def _full_catalog():
    """Каталог, в котором применён весь _SCHEMA_DDL — собран из самого DDL."""
    ident = r"([A-Za-z_][A-Za-z0-9_]*)"
    columns, relations, rls = {}, set(), set()
    for sql in db._SCHEMA_DDL:
        stmt = " ".join(sql.split())
        m = re.match(rf"CREATE TABLE IF NOT EXISTS {ident} \((.*)\)$", stmt)
        if m:
            relations.add(m.group(1))
            columns.setdefault(m.group(1), set()).update(
                part.split()[0] for part in m.group(2).split(",") if part.split()
            )
            continue
        m = re.match(rf"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS {ident}", stmt)
        if m:
            relations.add(m.group(1))
            continue
        m = re.match(rf"ALTER TABLE {ident} ENABLE ROW LEVEL SECURITY$", stmt)
        if m:
            rls.add(m.group(1))
            continue
        m = re.match(rf"ALTER TABLE {ident} ADD COLUMN", stmt)
        if m:
            columns.setdefault(m.group(1), set()).update(
                re.findall(rf"ADD COLUMN IF NOT EXISTS {ident}", stmt)
            )
    return FakeCatalog(columns, relations, rls, constraints=set())


@pytest.fixture
def wire(monkeypatch):
    def _wire(cur):
        @contextmanager
        def conn():
            yield FakeConn(cur)

        monkeypatch.setattr(db, "get_connection", conn)
        monkeypatch.setattr(db, "_schema_ready", False)
        return cur

    return _wire


def test_every_ddl_statement_is_recognised():
    """Нераспознанный оператор считается неприменённым и гонял бы DDL каждый прогон."""
    cur = _full_catalog()
    assert all(db._ddl_done(cur, sql) for sql in db._SCHEMA_DDL)


def test_applied_schema_runs_no_ddl(wire):
    cur = wire(_full_catalog())
    db.ensure_schema()
    assert cur.ddl == []


def test_missing_column_runs_ddl(wire):
    cur = _full_catalog()
    cur.columns["edu_visit_behavior"].discard("traffic_source")
    wire(cur)
    db.ensure_schema()
    assert len(cur.ddl) == len(db._SCHEMA_DDL)


@pytest.mark.parametrize(
    "spoil",
    [
        lambda c: c.relations.discard("crm_payments_segment_key"),
        lambda c: c.rls.discard("crm_lead_details"),
        lambda c: c.constraints.add(("crm_leads", "crm_leads_date_campaign_id_key")),
        lambda c: c.relations.discard("edu_visit_behavior"),
    ],
    ids=["index", "rls", "old-constraint", "table"],
)
def test_each_gap_is_detected(spoil):
    cur = _full_catalog()
    spoil(cur)
    assert not all(db._ddl_done(cur, sql) for sql in db._SCHEMA_DDL)


def test_unknown_statement_counts_as_missing():
    assert db._ddl_done(_full_catalog(), "CREATE VIEW v AS SELECT 1") is False


def test_once_per_process(wire):
    cur = _full_catalog()
    cur.columns["crm_leads"].discard("audience")
    wire(cur)
    db.ensure_schema()
    db.ensure_schema()
    assert len(cur.ddl) == len(db._SCHEMA_DDL)
