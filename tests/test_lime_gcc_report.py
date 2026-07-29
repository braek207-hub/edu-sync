# -*- coding: utf-8 -*-
"""GCC-отчёт: агрегация web по срезам (GCC+страны) и построение строки широкого формата."""
from sync.lime_gcc_report import (
    _CODES, _GCC, _date_label, _header, _row_values, _scope_cols, fetch_web,
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


def test_fetch_web_splits_scopes_and_paid_organic():
    rows = [
        # date, data_source, country, traffic_type, sessions, orders
        ("2026-07-28", "web", "ОАЭ", "Платный", 100, 5),
        ("2026-07-28", "web", "ОАЭ", "Бесплатный", 40, 2),
        ("2026-07-28", "web", "Саудовская Аравия", "Платный", 30, 1),
        ("2026-07-28", "web", None, "Бесплатный", 7, 3),   # без страны → только GCC
        # app-строки: sessions=0, заказы → app_org/app_paid ЗАКАЗОВ (не трафика)
        ("2026-07-28", "app", "ОАЭ", "Платный", 0, 4),
        ("2026-07-28", "app", "ОАЭ", "Бесплатный", 0, 6),
    ]
    traffic, orders = fetch_web(FakeConn(rows), ["2026-07-28"])
    t = traffic["2026-07-28"]
    assert t[_GCC]["web_paid"] == 130      # 100 + 30
    assert t[_GCC]["web_org"] == 47        # 40 + 7(null)
    assert t["UAE"]["web_paid"] == 100
    assert t["UAE"]["app_org"] == 0        # app-трафик из lime_stats НЕ берём (только AppMetrica)
    o = orders["2026-07-28"]
    assert o[_GCC]["web_paid"] == 6        # 5 + 1
    assert o[_GCC]["web_org"] == 5         # 2 + 3(null)
    assert o["UAE"]["web_org"] == 2
    # app-ЗАКАЗЫ из data_source='app'
    assert o["UAE"]["app_paid"] == 4
    assert o["UAE"]["app_org"] == 6
    assert o[_GCC]["app_paid"] == 4
    assert o[_GCC]["app_org"] == 6


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
    traffic, _ = fetch_web(FakeConn([]), ["2026-07-28"])
    d = traffic["2026-07-28"]
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
