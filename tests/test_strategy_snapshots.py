# -*- coding: utf-8 -*-
"""Снимок настроек для журнала изменений Panda-BI — у всех дашбордов, не только EDU.

Главный риск — чужие кампании под чужим именем: пока в strategy_snapshots нет колонки
dashboard, вся таблица — EDU, и снимок LIME/BJORN туда лечь не должен. Второй — снимок
казахстанского кабинета LIME в тенге — он пересчитывается в рубли при записи.
Третий — стратегия РСЯ-кампаний, терявшаяся за выключенным поиском (SERVING_OFF).
"""

import os

import pytest

from sync import direct_settings_cabinets as C
from sync import edu_direct_settings as eds
from sync import lime_direct
from sync import strategy_snapshots as SS


# Формы витрин — срезы боевых строк 08.10.2026 (settings->'strategy' / 'meta').
def _ch(t, **kw):
    base = {"biddingStrategyType": t, "weeklyBudget": None, "targetCpa": None, "targetDrr": None}
    base.update(kw)
    return base


LIME_NETWORK_ONLY_MULTI = {  # 703799929: поиск выключен, РСЯ «несколько целей» + пакет
    "meta": {"state": "ON", "status": "ACCEPTED", "campaignType": "TEXT_CAMPAIGN"},
    "strategy": {
        "search": _ch("SERVING_OFF"),
        "network": _ch("AVERAGE_CPA_MULTIPLE_GOALS", weeklyBudget=55000.0),
        "package": {"id": 703172483, "type": "AVERAGE_CPA_MULTIPLE_GOALS", "weeklyBudget": None,
                    "targetCpa": None, "targetDrr": None},
        "priorityGoalsDetails": [
            {"goalId": 1900016999, "bidKind": "cpa", "bidValue": 1200.0},
            {"goalId": 3023504302, "bidKind": "cpa", "bidValue": 350.0},
        ],
    },
}
BJORN_NETWORK_CRR = {  # 713525080: РСЯ, доля рекламных расходов 10%
    "meta": {"state": "SUSPENDED", "status": "ACCEPTED"},
    "strategy": {
        "search": _ch("SERVING_OFF"),
        "network": _ch("AVERAGE_CRR", weeklyBudget=5000.0, targetDrr=10.0),
        "package": None,
        "priorityGoalsDetails": [{"goalId": 341172800, "bidKind": "cpa"}],
    },
}
EDU_SEARCH_MULTI = {  # 712948570: поиск «несколько целей», цены 5000 / 500
    "meta": {"state": "ON", "status": "ACCEPTED"},
    "strategy": {
        "search": _ch("AVERAGE_CPA_MULTIPLE_GOALS", weeklyBudget=50000.0),
        "network": _ch("SERVING_OFF"),
        "priorityGoalsDetails": [
            {"goalId": 541664134, "bidKind": "cpa", "bidValue": 5000.0},
            {"goalId": 360811375, "bidKind": "cpa", "bidValue": 500.0},
        ],
    },
}


def test_network_only_campaign_takes_network_strategy_not_serving_off():
    row = SS.snapshot_row("703799929", "16 APP", LIME_NETWORK_ONLY_MULTI)
    assert row["strategy_type"] == "AVERAGE_CPA_MULTIPLE_GOALS"
    assert row["weekly_budget"] == 55000.0
    assert row["target_cpa"] == 1200.0  # самая дорогая цель
    assert row["target_crr"] is None
    assert (row["state"], row["status"]) == ("ON", "ACCEPTED")


def test_crr_goes_to_target_crr_never_to_rubles():
    row = SS.snapshot_row("713525080", "b", BJORN_NETWORK_CRR)
    assert row["strategy_type"] == "AVERAGE_CRR"
    assert row["target_crr"] == 10.0
    assert row["target_cpa"] is None
    assert row["weekly_budget"] == 5000.0


def test_search_strategy_wins_while_search_serves():
    row = SS.snapshot_row("712948570", "e", EDU_SEARCH_MULTI)
    assert row["strategy_type"] == "AVERAGE_CPA_MULTIPLE_GOALS"
    assert (row["weekly_budget"], row["target_cpa"]) == (50000.0, 5000.0)
    network_default = {"meta": {"state": "ON"}, "strategy": {
        "search": _ch("AVERAGE_CPA", weeklyBudget=30000.0, targetCpa=900.0),
        "network": _ch("NETWORK_DEFAULT"),
    }}
    row = SS.snapshot_row("1", "", network_default)
    assert (row["strategy_type"], row["target_cpa"]) == ("AVERAGE_CPA", 900.0)


def test_both_channels_off_is_serving_off():
    s = {"meta": {"state": "OFF"}, "strategy": {"search": _ch("SERVING_OFF"), "network": _ch("SERVING_OFF")}}
    assert SS.snapshot_row("1", "", s)["strategy_type"] == "SERVING_OFF"


def test_single_goal_cpa_ignores_priority_goal_prices():
    s = {"meta": {"state": "ON"}, "strategy": {
        "search": _ch("WB_MAXIMUM_CONVERSION_RATE", weeklyBudget=10000.0),
        "priorityGoalsDetails": [{"goalId": 1, "bidValue": 700.0}],
    }}
    assert SS.snapshot_row("1", "", s)["target_cpa"] is None


def test_package_values_fill_empty_channel():
    s = {"meta": {"state": "ON"}, "strategy": {
        "search": _ch("SERVING_OFF"),
        "network": _ch("AVERAGE_CPA"),
        "package": {"type": "AVERAGE_CPA", "weeklyBudget": 70000.0, "targetCpa": 800.0},
    }}
    row = SS.snapshot_row("1", "", s)
    assert (row["weekly_budget"], row["target_cpa"]) == (70000.0, 800.0)


def test_campaign_without_state_is_not_snapshotted():
    # РМП LIME: API кампанию не отдаёт, витрина знает только имя из отчёта.
    assert SS.snapshot_row("119098375", "APP. Конверсии. Android", {"meta": {}, "strategy": {}}) is None


def test_money_factor_converts_rubles_but_not_drr():
    row = SS.snapshot_row("1", "", LIME_NETWORK_ONLY_MULTI, money_factor=0.2)
    assert (row["weekly_budget"], row["target_cpa"]) == (11000.0, 240.0)
    assert SS.snapshot_row("1", "", BJORN_NETWORK_CRR, money_factor=0.2)["target_crr"] == 10.0


def test_insert_sql_columns_follow_schema():
    legacy = SS.insert_sql(with_dashboard_column=False, with_crr_column=False)
    assert "dashboard" not in legacy and "target_crr" not in legacy
    assert "ON CONFLICT (date, campaign_id)" in legacy
    full = SS.insert_sql(with_dashboard_column=True, with_crr_column=True)
    assert "INSERT INTO strategy_snapshots (dashboard, date, campaign_id" in full
    assert "target_crr = EXCLUDED.target_crr" in full
    assert "dashboard = EXCLUDED.dashboard" in full
    assert "date = EXCLUDED" not in full


def test_select_sql_reads_dashboard_table_and_optionally_ids():
    assert "FROM bjorn_campaign_settings" in SS.select_sql("bjorn", only_ids=False)
    assert "ANY(" not in SS.select_sql("bjorn", only_ids=False)
    assert "WHERE campaign_id = ANY(%(ids)s)" in SS.select_sql("lime", only_ids=True)


class _Cur:
    def __init__(self, columns, settings_rows):
        self.columns = columns
        self.settings_rows = settings_rows
        self.executed = []
        self._last = ""

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._last = sql

    def fetchall(self):
        if "information_schema" in self._last:
            return [(c,) for c in self.columns]
        return list(self.settings_rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self.cur = cur
        self.committed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_pg(monkeypatch, columns, settings_rows=()):
    cur = _Cur(columns, settings_rows)
    batches = []
    monkeypatch.setattr(SS.psycopg2, "connect", lambda url: _Conn(cur))
    monkeypatch.setattr(
        SS.psycopg2.extras, "execute_batch",
        lambda c, sql, rows, page_size=None: batches.append((sql, list(rows))),
    )
    return cur, batches


ROWS = [
    ("703799929", "16 APP", LIME_NETWORK_ONLY_MULTI),
    ("119098375", "APP. Конверсии. Android", {"meta": {}}),
]


def test_foreign_dashboard_skips_until_migration(monkeypatch, capsys):
    cur, batches = _fake_pg(monkeypatch, columns=[], settings_rows=ROWS)
    assert SS.write_snapshot("pg://x", "polinarepik") == 0
    assert len(cur.executed) == 1 and batches == []
    assert "::warning::" in capsys.readouterr().out


def test_edu_keeps_writing_before_migration(monkeypatch):
    cur, batches = _fake_pg(monkeypatch, columns=[], settings_rows=ROWS)
    assert SS.write_snapshot("pg://x", "edunetwork") == 1
    assert "FROM edu_campaign_settings" in cur.executed[-1][0]
    sql, rows = batches[0]
    assert "dashboard" not in sql and "target_crr" not in sql
    assert [r["campaign_id"] for r in rows] == ["703799929"]


def test_after_migration_every_dashboard_writes_its_slug(monkeypatch):
    cur, batches = _fake_pg(monkeypatch, columns=["dashboard"], settings_rows=ROWS)
    SS.write_snapshot("pg://x", "meshnflesh")
    assert "FROM mnf_campaign_settings" in cur.executed[-1][0]
    sql, rows = batches[0]
    assert "dashboard" in sql and "target_crr" not in sql
    assert rows[0]["dashboard"] == "meshnflesh"
    assert rows[0]["date"] == SS.msk_today()


def test_target_crr_written_once_column_exists(monkeypatch):
    _, batches = _fake_pg(
        monkeypatch, columns=["dashboard", "target_crr"], settings_rows=[("713525080", "b", BJORN_NETWORK_CRR)]
    )
    SS.write_snapshot("pg://x", "bjorn")
    sql, rows = batches[0]
    assert "target_crr" in sql and rows[0]["target_crr"] == 10.0


def test_only_ids_and_money_factor_reach_the_rows(monkeypatch):
    cur, batches = _fake_pg(monkeypatch, columns=["dashboard"], settings_rows=ROWS[:1])
    SS.write_snapshot("pg://x", "lime", ["703799929"], money_factor=0.5)
    sql, params = cur.executed[-1]
    assert "ANY(%(ids)s)" in sql and params["ids"] == ["703799929"]
    assert batches[0][1][0]["weekly_budget"] == 27500.0


def test_unknown_dashboard_is_an_error():
    with pytest.raises(ValueError):
        SS.write_snapshot("pg://x", "decortier")


def test_lime_snapshot_converts_kz_money_to_rubles(monkeypatch):
    calls = []
    monkeypatch.setattr(lime_direct, "write_snapshot", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(lime_direct, "_pg_url", lambda: "pg://x")
    monkeypatch.setattr(lime_direct, "fx_to_rub", lambda cur, d: {"KZT": 0.17}[cur])
    monkeypatch.setenv("LIME_DIRECT_SRC_CURRENCY", "KZT")
    monkeypatch.setenv("LIME_DIRECT_VAT_MULT", "1.16")
    lime_direct._snapshot_settings(["1"])
    (args, kw), = calls
    assert args == ("pg://x", "lime", ["1"])
    assert kw["money_factor"] == pytest.approx(0.17 * 1.16)
    # Рублёвый кабинет — без пересчёта.
    calls.clear()
    monkeypatch.delenv("LIME_DIRECT_SRC_CURRENCY")
    monkeypatch.delenv("LIME_DIRECT_VAT_MULT")
    lime_direct._snapshot_settings(["1", "2"])
    assert calls == [(("pg://x", "lime", ["1", "2"]), {"money_factor": 1.0})]


def test_parse_clients_json_same_forms_as_bjorn_sync():
    raw = '["a", {"login": "b", "token": "tb"}, {"client_login": "c"}, {"token": "x"}]'
    assert C.parse_clients_json(raw, "dflt") == [
        {"login": "a", "token": "dflt"},
        {"login": "b", "token": "tb"},
        {"login": "c", "token": "dflt"},
    ]
    with pytest.raises(RuntimeError):
        C.parse_clients_json("[]")


def _stub_cabinet_io(monkeypatch, fail_login=None):
    seen = []
    monkeypatch.setattr(eds, "_list_campaigns_for_login", lambda: {"7": "к"})

    def _sync(ids, names, table, extra_counter_ids):
        seen.append((eds._CURRENT_LOGIN, os.environ["DIRECT_TOKEN"], table, extra_counter_ids))
        if eds._CURRENT_LOGIN == fail_login:
            raise ValueError("boom")
        return len(ids)

    snaps = []
    monkeypatch.setattr(eds, "_sync_campaign_settings", _sync)
    monkeypatch.setattr(eds, "_pg_url", lambda: "pg://x")
    monkeypatch.setattr(C, "write_snapshot", lambda url, dash: snaps.append(dash))
    return seen, snaps


def test_bjorn_cabinet_walks_every_login_with_its_token(monkeypatch):
    monkeypatch.setenv(
        "BJORN_DIRECT_CLIENTS_JSON", '[{"login": "b1", "token": "t1"}, {"login": "b2", "token": "t2"}]'
    )
    monkeypatch.setenv("BJORN_METRICA_COUNTER_ID", "123")
    seen, snaps = _stub_cabinet_io(monkeypatch)
    assert C.sync_cabinet("bjorn") == 2
    assert seen == [
        ("b1", "t1", "bjorn_campaign_settings", [123]),
        ("b2", "t2", "bjorn_campaign_settings", [123]),
    ]
    assert snaps == ["bjorn"]


def test_polina_defaults_login_and_reads_yandex_token(monkeypatch):
    monkeypatch.delenv("POLINAREPIK_DIRECT_TOKEN", raising=False)
    monkeypatch.delenv("POLINAREPIK_DIRECT_CLIENT_LOGIN", raising=False)
    monkeypatch.setenv("POLINAREPIK_YANDEX_TOKEN", "py")
    seen, snaps = _stub_cabinet_io(monkeypatch)
    C.sync_cabinet("polinarepik")
    assert seen == [("polinarepik-wear", "py", "polinarepik_campaign_settings", [100764399])]
    assert snaps == ["polinarepik"]


def test_failed_login_makes_run_red_but_others_and_snapshot_go_through(monkeypatch):
    monkeypatch.setenv(
        "BJORN_DIRECT_CLIENTS_JSON", '[{"login": "b1", "token": "t1"}, {"login": "b2", "token": "t2"}]'
    )
    seen, snaps = _stub_cabinet_io(monkeypatch, fail_login="b1")
    with pytest.raises(RuntimeError) as err:
        C.sync_cabinet("bjorn")
    assert "b1" in str(err.value)
    assert [s[0] for s in seen] == ["b1", "b2"]
    assert snaps == ["bjorn"]


def test_meshnflesh_entrypoint_is_the_generic_cabinet(monkeypatch):
    monkeypatch.setenv("MESHNFLESH_DIRECT_TOKEN", "mt")
    seen, snaps = _stub_cabinet_io(monkeypatch)
    from sync import meshnflesh_direct_settings as M

    M.sync_meshnflesh_campaign_settings()
    assert seen == [("meshnflesh", "mt", "mnf_campaign_settings", [82116769])]
    assert snaps == ["meshnflesh"]
