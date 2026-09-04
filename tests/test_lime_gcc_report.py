# -*- coding: utf-8 -*-
"""GCC-отчёт: агрегация web по срезам (GCC+страны) и построение строки широкого формата."""
from sync.lime_gcc_report import (
    _CODES, _GCC, _apply_app_total, _date_label, _ga4_row, _header, _row_values, _scope_cols,
    _split_app_by_logs, aggregate_orders_linear, fetch_metrika_web, fetch_web,
)


def _empty_traffic(dates):
    from sync.lime_gcc_report import _SCOPES, _empty_day
    return {d: _empty_day() for d in dates}


def test_write_traffic_partial_preserves_other_side(monkeypatch):
    """owns='web' → сохраняем app с листа; owns='app' → сохраняем web (не обнуляем)."""
    import sync.lime_gcc_report as rep
    calls = {}
    monkeypatch.setattr(rep, "_fill_from_sheet",
                        lambda svc, data, dates, fields: calls.__setitem__("keep", tuple(fields)))
    monkeypatch.setattr(rep, "_refresh_tab", lambda *a, **k: calls.__setitem__("wrote", True))

    rep._write_traffic_partial(None, ["2026-07-01"], {}, "web")
    assert calls["keep"] == ("app_org", "app_paid") and calls["wrote"]

    calls.clear()
    rep._write_traffic_partial(None, ["2026-07-01"], {}, "app")
    assert calls["keep"] == ("web_org", "web_paid")


def test_ga4_row_and_header():
    from sync.lime_gcc_report import _ga4_header, _ga4_row
    h = _ga4_header()
    assert h[:7] == ["Дата", "Год", "Месяц", "Неделя", "ORG Total", "PAID Total", "Total"]
    assert "ORG UAE" in h and "Total OM" in h
    day = {"GCC": {"org": 3387, "paid": 5170}, "UAE": {"org": 1646, "paid": 1478},
           "KSA": {"org": 0, "paid": 0}, "QA": {"org": 0, "paid": 0},
           "KW": {"org": 0, "paid": 0}, "OM": {"org": 0, "paid": 0}}
    rv = _ga4_row("2025-09-01", day)
    assert rv["ORG Total"] == 3387 and rv["PAID Total"] == 5170 and rv["Total"] == 8557
    assert rv["Total UAE"] == 3124  # 1646+1478
    assert rv["Год"] == 2025 and rv["Месяц"] == 9


def test_apply_app_total_no_split():
    """Reporting total кладётся весь в app_org, paid=0 (полный день без разбивки)."""
    tr = _empty_traffic(["2026-07-28"])
    _apply_app_total(tr, {"2026-07-28": {_GCC: 219, "UAE": 200}}, ["2026-07-28"])
    assert tr["2026-07-28"][_GCC]["app_org"] == 219
    assert tr["2026-07-28"][_GCC]["app_paid"] == 0
    assert tr["2026-07-28"]["UAE"]["app_org"] == 200


def test_split_app_by_logs_preserves_reporting_total():
    """Сплит: paid = round(total × доля_Logs), тотал остаётся Reporting-точным."""
    tr = _empty_traffic(["2026-07-28"])
    app_total = {"2026-07-28": {_GCC: 200}}
    _apply_app_total(tr, app_total, ["2026-07-28"])
    # Logs: 150 org / 50 paid → доля paid = 25% → от Reporting total 200: paid=50, org=150
    logs = {"2026-07-28": {_GCC: {"app_org": 150, "app_paid": 50}}}
    _split_app_by_logs(tr, app_total, logs, ["2026-07-28"])
    m = tr["2026-07-28"][_GCC]
    assert m["app_paid"] == 50 and m["app_org"] == 150
    assert m["app_org"] + m["app_paid"] == 200   # тотал = Reporting, не Logs


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
    assert rv["Дата"] == "Tue 28/07/2026"
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
    assert _date_label("2026-07-28") == "Tue 28/07/2026"


def test_aggregate_orders_lpc_whole_order_to_paid_or_org():
    """LPC: заказ целиком paid (последняя платная площадка) либо целиком org; без дробей."""
    from sync.lime_gcc_report import aggregate_orders_lpc

    def order(oid, source, referrer=None):
        tp = {"source": source}
        if referrer:
            tp["campaignId"] = referrer
        return {"order_id": oid, "attribution": {"lastPlatformClick": [tp] if source else []}}

    by_date = {"2026-07-01": [
        order("1", "google-ads"),            # платный → web_paid (страна UAE)
        order("2", "facebook-ads"),          # платный, app-заказ
        order("3", "organic_and_social", "google"),  # SEO → org
        order("4", None),                    # без атрибуции → org, страна не опознана
    ]}
    country = {"1": "ОАЭ", "2": "Саудовская Аравия", "3": "ОАЭ"}
    out = aggregate_orders_lpc(by_date, country, app_ids={"2"}, dates=["2026-07-01"])

    g = out["2026-07-01"]["GCC"]
    assert g == {"web_org": 2, "web_paid": 1, "app_org": 0, "app_paid": 1}
    assert out["2026-07-01"]["UAE"] == {"web_org": 1, "web_paid": 1, "app_org": 0, "app_paid": 0}
    assert out["2026-07-01"]["KSA"] == {"web_org": 0, "web_paid": 0, "app_org": 0, "app_paid": 1}
    # тотал заказов сходится: сумма всех полей GCC = 4
    assert sum(g.values()) == 4


def test_fetch_metrika_web_sums_five_countries():
    rows = [
        # date, country, traffic_type, users
        ("2026-08-14", "ОАЭ", "Платный", 1800),
        ("2026-08-14", "ОАЭ", "Бесплатный", 900),
        ("2026-08-14", "Саудовская Аравия", "Платный", 300),
        ("2026-08-14", "Бахрейн", "Платный", 55),   # не из пяти → мимо GCC-тотала
    ]
    d = fetch_metrika_web(FakeConn(rows), ["2026-08-14"])["2026-08-14"]
    assert d[_GCC] == {"org": 900, "paid": 2100}
    assert d["UAE"] == {"org": 900, "paid": 1800}
    assert d["KSA"] == {"org": 0, "paid": 300}
    # GCC = сумма пяти стран (в пайплайне Метрики строк без страны нет)
    assert d[_GCC]["paid"] == sum(d[c]["paid"] for c in _CODES)


def test_metrika_tab_row_matches_ga4_tab_shape():
    """Вкладка Метрики пишется формой GA4-вкладки — иначе формулы сверки не найдут колонки."""
    rows = [("2026-08-14", "ОАЭ", "Платный", 10), ("2026-08-14", "ОАЭ", "Бесплатный", 5)]
    day = fetch_metrika_web(FakeConn(rows), ["2026-08-14"])["2026-08-14"]
    r = _ga4_row("2026-08-14", day)
    assert r["Total"] == 15 and r["ORG Total"] == 5 and r["PAID Total"] == 10
    assert r["Total UAE"] == 15
