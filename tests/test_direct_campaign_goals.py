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
