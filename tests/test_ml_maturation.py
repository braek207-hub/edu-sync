from sync.ml.maturation import maturation_table

def test_cdf_monotonic_and_bounded():
    tbl = maturation_table([1, 4, 4, 30, 56], horizon=60)
    fracs = [f for _, f in tbl]
    assert fracs == sorted(fracs)             # монотонность
    assert all(0.0 <= f <= 1.0 for f in fracs)
    assert tbl[0][0] == 0 and tbl[-1][0] == 60

def test_cdf_values():
    # 4 оплаты в дни 1,1,10,20 → к дню 1: 2/4=0.5; к дню 10: 3/4=0.75; к дню 20: 1.0
    tbl = dict(maturation_table([1, 1, 10, 20], horizon=20))
    assert abs(tbl[0] - 0.0) < 1e-9
    assert abs(tbl[1] - 0.5) < 1e-9
    assert abs(tbl[10] - 0.75) < 1e-9
    assert abs(tbl[20] - 1.0) < 1e-9

def test_empty_input_returns_zeros():
    tbl = maturation_table([], horizon=5)
    assert all(f == 0.0 for _, f in tbl)
    assert len(tbl) == 6
