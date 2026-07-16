from sync.edu_demand import aggregate_weekly_by_phrase, EDU_DEMAND_PHRASES


def test_phrases_are_root_terms_no_nesting():
    # Крупные корни, нет вложенных «поступить в …» / «… заочно».
    assert "колледж" in EDU_DEMAND_PHRASES
    assert "вуз" in EDU_DEMAND_PHRASES
    assert all("поступить" not in p and "заочно" not in p for p in EDU_DEMAND_PHRASES)
    assert len(EDU_DEMAND_PHRASES) == len(set(EDU_DEMAND_PHRASES))  # без дублей


def test_aggregate_snaps_to_iso_monday():
    # date из API — понедельник недели; count приходит строкой (proto int64).
    resp = {"results": [
        {"date": "2025-06-02", "count": "12000", "share": "1.0"},
        {"date": "2025-06-09", "count": "9000", "share": "1.0"},
    ]}
    out = aggregate_weekly_by_phrase("колледж", resp)
    assert out == {"2025-06-02": 12000, "2025-06-09": 9000}


def test_aggregate_reduces_midweek_date_to_monday():
    resp = {"results": [{"date": "2025-06-04", "count": "5", "share": "1.0"}]}  # среда
    out = aggregate_weekly_by_phrase("вуз", resp)
    assert out == {"2025-06-02": 5}  # понедельник той недели


def test_entry_module_imports():
    import importlib
    mod = importlib.import_module("sync_edu_demand")
    assert hasattr(mod, "main")
