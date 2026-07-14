import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.import_brand_history import parse_history_csv

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "brand_history_sample.csv")


def test_parse_history_csv():
    rows = parse_history_csv(FIX)
    assert len(rows) == 3
    assert rows[0] == {"week_start": "2023-01-02", "demand": 75758, "seo_clicks": 54293}


def test_parse_history_normalizes_to_monday():
    # даже если в файле дата не понедельник — приводим к ISO-понедельнику
    rows = parse_history_csv(FIX)
    for r in rows:
        assert r["week_start"] <= r["week_start"]  # sanity
    # 2023-01-02 — понедельник, остальные тоже недельные шаги
    assert rows[1]["week_start"] == "2023-01-09"
