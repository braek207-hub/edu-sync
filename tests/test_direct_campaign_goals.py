"""Цели конверсий EDU по кампаниям: внутри кабинета vuz у школы и заграницы свои цели.

Директ разворачивает Conversions в колонку на цель (Conversions_<цель>_LSCCD), поэтому
отчёт читается по заголовку, а кампании засчитываются только колонки её целей.
"""
from sync import direct as D

HEADER = "\t".join(D.FIELD_NAMES + [
    "Conversions_360811375_LSCCD", "Conversions_596548027_LSCCD", "Conversions_670497292_LSCCD",
])


def _line(cid, name, conv):
    return "\t".join(["2026-10-20", cid, name, "100", "10", "500.5", "12,5", "80", "1.5", "2"]
                     + [str(c) for c in conv])


def test_cabinet_goals_include_landing_goals_for_vuz_only():
    assert D.cabinet_goals("vuz") == ["360811375", "596548027", "670497292"]
    assert D.cabinet_goals("vse") == ["330070378"]
    assert D.cabinet_goals(None, ["7", "8"]) == ["7", "8"]


def test_campaign_goals_by_landing():
    assert D.campaign_goals("vuz", "vuz / Онлайн-школа / Школа / Поиск / РФ (2026-08)") == ["596548027"]
    assert D.campaign_goals("vuz", "vuz / Обучение в Дубае / ВПО / Поиск / МСК (2026-10)") == ["670497292"]
    assert D.campaign_goals("vuz", "vuz / Бакалавриат / Поиск / РФ") == ["360811375"]
    # Переопределения действуют только внутри кабинета vuz.
    assert D.campaign_goals("vse", "vse / Обучение в Дубае") == ["330070378"]


def test_parse_counts_only_campaign_goal_columns():
    text = "\n".join([
        HEADER,
        _line("1", "vuz / Онлайн-школа / Школа / Поиск / РФ (2026-08)", [9, 4, 0]),
        _line("2", "vuz / Обучение в Дубае / ВПО / Поиск / МСК (2026-10)", [5, 0, 2]),
        _line("3", "vuz / Бакалавриат / Поиск / РФ", [7, 1, 1]),
    ])
    rows = {r["campaign_id"]: r for r in D._parse_report_tsv(text, "vuz")}
    assert rows["1"]["conversions"] == 4
    assert rows["1"]["project"] == "eduschool"
    assert rows["2"]["conversions"] == 2
    assert rows["3"]["conversions"] == 7
    assert rows["3"]["cost"] == 500.5
    assert rows["3"]["w_avg_eff_bid"] == 125.0
    assert rows["3"]["w_avg_impr_pos"] == 150.0


def test_parse_single_conversions_column_and_no_goals():
    one = "\t".join(D.FIELD_NAMES + ["Conversions"])
    rows = D._parse_report_tsv("\n".join([one, _line("1", "vse_x", [3])]), "vse")
    assert rows[0]["conversions"] == 3
    bare = "\t".join(D.FIELD_NAMES)
    rows = D._parse_report_tsv("\n".join([bare, _line("1", "vse_x", [])]), None)
    assert rows[0]["conversions"] == 0


def test_parse_fallback_goals_sum_all_columns():
    text = "\n".join([HEADER, _line("1", "кампания", [1, 2, 3])])
    rows = D._parse_report_tsv(text, None, ["360811375", "596548027", "670497292"])
    assert rows[0]["conversions"] == 6


SYNERGY = {"login": "vpo-goroda-316088-c0ts", "project": "synergy_school", "direction": "school"}


def test_own_token_cabinet_sets_project_and_direction_from_cabinet():
    """Имя «ОШ Синергия РСЯ - …» проекта не называет: весь кабинет — synergy_school."""
    one = "\t".join(D.FIELD_NAMES + ["Conversions_317516886_LSCCD"])
    text = "\n".join([one, _line("715663399", "ОШ Синергия РСЯ - Москва", [3])])
    rows = D._parse_report_tsv(text, "synergy_school", [], SYNERGY)
    assert rows[0]["project"] == "synergy_school"
    assert rows[0]["direction"] == "school"
    assert rows[0]["conversions"] == 3
    assert D.cabinet_goals("synergy_school") == ["317516886"]


def test_own_token_cabinet_uses_its_token(monkeypatch):
    monkeypatch.setenv("DIRECT_TOKEN", "edu")
    monkeypatch.setenv("APPROVE_AGENCY_TOKEN", "agency")
    cab = D._own_token_cabinets()[0]
    assert cab["login"] == "vpo-goroda-316088-c0ts" and cab["token"] == "agency"
    assert D._report_headers(cab["login"], cab["token"])["Authorization"] == "Bearer agency"
    assert D._report_headers("account3")["Authorization"] == "Bearer edu"


def test_own_token_cabinet_without_token_fails_run(monkeypatch):
    import sys
    import types

    import pytest

    monkeypatch.setitem(sys.modules, "sync.db", types.SimpleNamespace(
        upsert_direct_stats=lambda rows: len(rows), delete_direct_stats_from=lambda d: 0))
    monkeypatch.delenv("APPROVE_AGENCY_TOKEN", raising=False)
    monkeypatch.setattr(D, "_direct_clients", lambda: [
        {"login": "a", "goal_ids": [], "project": "vuz", "sheet_name": ""}])
    monkeypatch.setattr(D, "_fetch_report", lambda *a, **k: [{"date": "2026-11-10", "cost": 1}])
    with pytest.raises(RuntimeError, match="APPROVE_AGENCY_TOKEN"):
        D.sync_direct_api_range("2026-11-10", "2026-11-16")
