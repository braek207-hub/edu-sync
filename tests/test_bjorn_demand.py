from sync.bjorn_demand import BJORN_DEMAND_PHRASES


def test_phrases_are_category_roots_no_brand_no_dupes():
    # Категорийные корни каталога, без бренда/моделей (Bjorn Larsen / Репино / Комарово), без дублей.
    assert "пуховик" in BJORN_DEMAND_PHRASES
    assert all("bjorn" not in p.lower() and "ларсен" not in p.lower() for p in BJORN_DEMAND_PHRASES)
    # голую «куртка» не берём (поглотила бы срезы и двоила Σ); «парка» отдельно не берём
    # (Wordstat broad-match ловит «парк/парковка») — только «куртка парка».
    assert "куртка" not in BJORN_DEMAND_PHRASES
    assert "парка" not in BJORN_DEMAND_PHRASES
    assert "куртка парка" in BJORN_DEMAND_PHRASES
    assert len(BJORN_DEMAND_PHRASES) == len(set(BJORN_DEMAND_PHRASES))


def test_entry_module_imports():
    import importlib
    mod = importlib.import_module("sync_bjorn_demand")
    assert hasattr(mod, "main")
