from sync.edu_demand import EDU_DEMAND_PHRASES, EDU_DEMAND_REGIONS, aggregate_weekly_by_phrase


def test_phrases_expanded_no_duplicates():
    # Расширенный набор: новые уровневые фразы присутствуют, дублей нет.
    assert "магистратура" in EDU_DEMAND_PHRASES
    assert "аспирантура" in EDU_DEMAND_PHRASES
    assert "переподготовка" in EDU_DEMAND_PHRASES
    assert "заочное обучение" in EDU_DEMAND_PHRASES
    assert len(EDU_DEMAND_PHRASES) == len(set(EDU_DEMAND_PHRASES))


def test_phrases_are_roots_no_poostupit_nesting():
    # Корни, а не вложенные «поступить в …» (широкое соответствие ловит вложенное само).
    # NB: «заочное обучение» — самостоятельный корень (не вложенное «… заочно»), допустимо.
    assert all("поступить" not in p for p in EDU_DEMAND_PHRASES)


def test_two_regions_ru_and_msk():
    keys = [k for k, _ in EDU_DEMAND_REGIONS]
    assert keys == ["ru", "msk"]
    assert EDU_DEMAND_REGIONS[0][1] == ["225"]
    assert EDU_DEMAND_REGIONS[1][1] and EDU_DEMAND_REGIONS[1][1][0] != "225"


def test_aggregate_weekly_by_phrase_sums_by_monday():
    resp = {"results": [{"date": "2026-06-01", "count": "10"}, {"date": "2026-06-03", "count": "5"}]}
    out = aggregate_weekly_by_phrase("вуз", resp)
    assert out["2026-06-01"] == 15  # оба дня в неделе Пн 2026-06-01


def test_aggregate_reduces_midweek_date_to_monday():
    resp = {"results": [{"date": "2025-06-04", "count": "5", "share": "1.0"}]}  # среда
    out = aggregate_weekly_by_phrase("вуз", resp)
    assert out == {"2025-06-02": 5}  # понедельник той недели


def test_entry_module_imports():
    import importlib
    mod = importlib.import_module("sync_edu_demand")
    assert hasattr(mod, "main")
