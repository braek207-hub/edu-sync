# -*- coding: utf-8 -*-
"""GCC-отчёт: агрегация web по срезам (GCC+страны) и построение строки широкого формата."""
from sync.lime_gcc_report import (
    _CODES, _GCC, _date_label, _header, _row_values, _scope_cols,
    aggregate_orders_linear, fetch_web,
)


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


def test_header_has_gcc_block_and_five_countries():
    h = _header()
    assert h[:4] == ["Дата", "Год", "Месяц", "Неделя"]
    # 4 меты + 6 срезов × 7 колонок
    assert len(h) == 4 + 6 * 7
    assert "Web Org" in h and "Общий" in h          # блок GCC
    for code in _CODES:
        assert f"{code} Web Paid" in h
        assert f"{code} Total" in h
    assert "BH Total" not in h                        # Бахрейна нет


def test_scope_cols_order():
    assert _scope_cols(_GCC) == ["Web Org", "Web Paid", "App Org", "App Paid",
                                 "WEB Total", "APP Total", "Общий"]
    assert _scope_cols("UAE")[0] == "UAE Web Org"
    assert _scope_cols("UAE")[-1] == "UAE Total"


def test_fetch_web_traffic_only():
    """fetch_web теперь только web-трафик (sessions); заказы считает fetch_orders_linear."""
    rows = [
        # date, country, traffic_type, sessions
        ("2026-07-28", "ОАЭ", "Платный", 100),
        ("2026-07-28", "ОАЭ", "Бесплатный", 40),
        ("2026-07-28", "Саудовская Аравия", "Платный", 30),
        ("2026-07-28", None, "Бесплатный", 7),   # без страны → только GCC
    ]
    t = fetch_web(FakeConn(rows), ["2026-07-28"])["2026-07-28"]
    assert t[_GCC]["web_paid"] == 130      # 100 + 30
    assert t[_GCC]["web_org"] == 47        # 40 + 7(null)
    assert t["UAE"]["web_paid"] == 100
    assert t["KSA"]["web_paid"] == 30
    assert t["UAE"]["app_org"] == 0        # app-трафик из AppMetrica, не тут


def test_aggregate_orders_linear_reconciles_total():
    """linearAll: paid=round(доли), organic=число заказов−paid → тотал точный 1-в-1."""
    orders_by_date = {"2026-07-28": [
        {"order_id": "1", "attribution": {"linearAll": [{"source": "facebook-ads"}]}},  # 1.0 paid
        {"order_id": "2", "attribution": {"linearAll": [{"source": "google-ads"},
                                                        {"source": "direct"}]}},         # 0.5 paid
        {"order_id": "3", "attribution": {"linearAll": []}},                             # 0 (органика)
        {"order_id": "9", "attribution": {"linearAll": [{"source": "google-ads"}]}},     # app-заказ
    ]}
    country_map = {"1": "ОАЭ", "2": "ОАЭ", "3": "ОАЭ", "9": "ОАЭ"}
    out = aggregate_orders_linear(orders_by_date, country_map, {"9"}, ["2026-07-28"])
    g = out["2026-07-28"][_GCC]
    # web: заказы 1,2,3 → тотал 3; paid_frac=1.0+0.5+0=1.5 → round=2, org=1
    assert g["web_paid"] + g["web_org"] == 3
    assert g["web_paid"] == 2 and g["web_org"] == 1
    # app: заказ 9 → тотал 1; paid_frac=1.0 → paid=1, org=0
    assert g["app_paid"] + g["app_org"] == 1
    assert g["app_paid"] == 1 and g["app_org"] == 0
    # страна ОАЭ = UAE, те же числа (все доставки ОАЭ)
    u = out["2026-07-28"]["UAE"]
    assert u["web_paid"] == 2 and u["app_paid"] == 1


def test_aggregate_orders_linear_country_none_only_gcc():
    """Заказ без страны Залива (не в country_map) → только GCC-срез, не в страну."""
    orders_by_date = {"2026-07-28": [
        {"order_id": "5", "attribution": {"linearAll": [{"source": "google-ads"}]}},
    ]}
    out = aggregate_orders_linear(orders_by_date, {}, set(), ["2026-07-28"])
    assert out["2026-07-28"][_GCC]["web_paid"] == 1
    assert all(out["2026-07-28"][c]["web_paid"] == 0 for c in _CODES)


def test_row_values_derives_totals():
    day = {s: {"web_org": 0, "web_paid": 0, "app_org": 0, "app_paid": 0}
           for s in (_GCC,) + _CODES}
    day[_GCC] = {"web_org": 47, "web_paid": 130, "app_org": 3, "app_paid": 9}
    day["UAE"] = {"web_org": 40, "web_paid": 100, "app_org": 1, "app_paid": 4}
    rv = _row_values("2026-07-28", day)
    assert rv["Web Org"] == 47 and rv["Web Paid"] == 130
    assert rv["WEB Total"] == 177          # 47 + 130
    assert rv["APP Total"] == 12           # 3 + 9
    assert rv["Общий"] == 189              # 177 + 12
    assert rv["UAE WEB Total"] == 140
    assert rv["UAE Total"] == 145          # 140 + (1+4)
    assert rv["Дата"] == "Вт 28.07.2026"
    assert rv["Неделя"] == 31


def test_fetch_web_empty_day_all_zero():
    d = fetch_web(FakeConn([]), ["2026-07-28"])["2026-07-28"]
    assert all(d[s]["web_org"] == 0 and d[s]["web_paid"] == 0 for s in (_GCC,) + _CODES)


def test_row_values_covers_every_header_column():
    """Каждая колонка заголовка получает значение из _row_values (иначе пустоты при build)."""
    day = {s: {"web_org": 1, "web_paid": 1, "app_org": 0, "app_paid": 0}
           for s in (_GCC,) + _CODES}
    rv = _row_values("2026-07-28", day)
    for col in _header():
        assert col in rv, f"колонка {col} не заполнена"


def test_date_label():
    assert _date_label("2026-07-28") == "Вт 28.07.2026"
