# -*- coding: utf-8 -*-
"""GCC-отчёт: агрегация web по странам/paid-organic и запись по именам колонок."""
from sync.lime_gcc_report import _CODES, _date_label, _slice_cells, fetch_web


class FakeCur:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return FakeCur(self._rows)


def test_fetch_web_maps_country_and_paid_organic():
    rows = [
        # d, country, traffic_type, sessions, orders
        ("2026-07-28", "ОАЭ", "Платный", 100, 5),
        ("2026-07-28", "ОАЭ", "Бесплатный", 40, 2),
        ("2026-07-28", "Саудовская Аравия", "Платный", 30, 1),
        ("2026-07-28", None, "Бесплатный", 7, 3),   # без страны → только в Total
    ]
    traffic, orders = fetch_web(FakeConn(rows), ["2026-07-28"])
    t = traffic["2026-07-28"]
    # Total = все страны + null
    assert t["paid"]["Total"] == 130          # 100 + 30
    assert t["org"]["Total"] == 47            # 40 + 7(null)
    assert t["paid"]["UAE"] == 100
    assert t["org"]["UAE"] == 40
    assert t["paid"]["KSA"] == 30
    o = orders["2026-07-28"]
    assert o["paid"]["Total"] == 6            # 5 + 1
    assert o["org"]["Total"] == 5             # 2 + 3(null)
    assert o["org"]["UAE"] == 2


def test_fetch_web_zero_for_absent_day():
    traffic, orders = fetch_web(FakeConn([]), ["2026-07-28"])
    assert traffic["2026-07-28"]["paid"]["Total"] == 0
    assert all(traffic["2026-07-28"]["org"][c] == 0 for c in _CODES)


def test_slice_cells_writes_by_named_columns():
    """Запись идёт в колонки ПО ИМЕНИ (ORG UAE / PAID Total / …), не по позиции."""
    name_to_col = {"Дата": 1, "Месяц": 2, "Год": 3, "ORG Total": 4, "PAID Total": 5,
                   "Total": 6, "ORG UAE": 7, "PAID UAE": 8, "Total UAE": 9}
    slc = {"org": {"Total": 47, "UAE": 40, "KSA": 0, "QA": 0, "KW": 0, "OM": 0, "BH": 0},
           "paid": {"Total": 130, "UAE": 100, "KSA": 0, "QA": 0, "KW": 0, "OM": 0, "BH": 0}}
    ups = _slice_cells("Fact Traffic GCC", "2026-07-28", slc, name_to_col,
                       formulas=[], ri0=10, is_new=False)
    cells = dict(ups)
    # ri0=10 → строка 11; ORG Total=E(idx4), PAID Total=F, ORG UAE=H, PAID UAE=I
    assert cells["Fact Traffic GCC!E11"] == [[47]]
    assert cells["Fact Traffic GCC!F11"] == [[130]]
    assert cells["Fact Traffic GCC!H11"] == [[40]]
    assert cells["Fact Traffic GCC!I11"] == [[100]]
    assert cells["Fact Traffic GCC!G11"] == [[177]]  # Total = 47+130 (литерал)


def test_slice_cells_skips_formula_total_on_existing_row():
    name_to_col = {"Дата": 1, "ORG Total": 4, "PAID Total": 5, "Total": 6}
    slc = {"org": {"Total": 47, "UAE": 0, "KSA": 0, "QA": 0, "KW": 0, "OM": 0, "BH": 0},
           "paid": {"Total": 130, "UAE": 0, "KSA": 0, "QA": 0, "KW": 0, "OM": 0, "BH": 0}}
    formulas = [[""] * 7 for _ in range(12)]
    formulas[10][6] = "=E11+F11"   # Total — формула
    ups = _slice_cells("Fact Traffic GCC", "2026-07-28", slc, name_to_col,
                       formulas=formulas, ri0=10, is_new=False)
    cells = dict(ups)
    assert "Fact Traffic GCC!G11" not in cells     # формулу не трогаем
    assert cells["Fact Traffic GCC!E11"] == [[47]]


def test_slice_cells_new_row_fills_date():
    name_to_col = {"Дата": 1, "Месяц": 2, "Год": 3, "ORG Total": 4, "PAID Total": 5}
    slc = {"org": {k: 0 for k in ("Total",) + _CODES},
           "paid": {k: 0 for k in ("Total",) + _CODES}}
    ups = _slice_cells("Fact Traffic GCC", "2026-07-28", slc, name_to_col,
                       formulas=[], ri0=10, is_new=True)
    cells = dict(ups)
    assert cells["Fact Traffic GCC!B11"] == [["Вт 28.07.2026"]]
    assert cells["Fact Traffic GCC!C11"] == [[7]]     # Месяц
    assert cells["Fact Traffic GCC!D11"] == [[2026]]  # Год


def test_date_label():
    assert _date_label("2026-07-28") == "Вт 28.07.2026"
