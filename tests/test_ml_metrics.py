from sync.ml.metrics import lift_at_decile, brier

def test_lift_perfect_ranking():
    # 10 объектов, 1 позитив, он на вершине по скору → топ-10% (1 шт) весь позитив
    y = [0,0,0,0,0,0,0,0,0,1]
    s = [0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.1,0.9]
    # base rate = 0.1; топ-1 из 10 = 100% позитив → lift = 1.0/0.1 = 10
    assert lift_at_decile(y, s, decile=1) == 10.0

def test_lift_no_positive_returns_zero():
    assert lift_at_decile([0,0,0], [0.5,0.1,0.2]) == 0.0

def test_brier_perfect():
    assert brier([1,0], [1.0,0.0]) == 0.0

def test_brier_worst():
    assert brier([1,0], [0.0,1.0]) == 1.0
