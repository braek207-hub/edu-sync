import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.lime_gcc import COLUMNS, merge_rows

COLS = ["date", "data_source", "region", "country", "channel", "subchannel", "traffic_type", "campaign_id",
        "campaign_name", "cost", "clicks", "impressions", "sessions", "users", "clients",
        "purchases_count", "purchases_revenue", "customers", "new_users", "new_customers", "new_customers_revenue",
        "bounce_rate", "page_depth"]


def test_merge_joins_by_channel_and_converts():
    metrika = [{"date": "2026-07-17", "traffic_source": "ad", "source_engine": "Google Ads", "visits": 500, "users": 400}]
    orders = [{"date": "2026-07-17", "channel": "SEM", "subchannel": "Google.Adwords", "traffic_type": "Платный", "orders": 10, "revenue": 1000.0}]
    spend = [{"date": "2026-07-17", "channel": "SEM", "subchannel": "Google.Adwords", "traffic_type": "Платный", "cost": 200.0}]
    rows = merge_rows(metrika, orders, spend, 20.0, "2026-07-17")
    assert len(rows) == 1
    r = dict(zip(COLS, rows[0]))
    assert r["region"] == "gcc" and r["data_source"] == "web"
    assert r["channel"] == "SEM" and r["subchannel"] == "Google.Adwords"
    assert r["sessions"] == 500 and r["users"] == 400
    assert r["purchases_count"] == 10
    assert r["purchases_revenue"] == 20000.0   # 1000 * 20
    assert r["cost"] == 4000.0                  # 200 * 20


def test_merge_traffic_only_channel():
    metrika = [{"date": "2026-07-17", "traffic_source": "direct", "source_engine": None, "visits": 30, "users": 25}]
    rows = merge_rows(metrika, [], [], 20.0, "2026-07-17")
    assert len(rows) == 1
    r = dict(zip(COLS, rows[0]))
    assert r["channel"] == "Direct" and r["sessions"] == 30 and r["purchases_count"] == 0 and r["cost"] == 0

# === Дробление по странам Залива (T5) ===


def test_columns_match_test_expectation():
    """COLS в тестах = реальный порядок колонок INSERT (иначе dict(zip(...)) врёт)."""
    assert list(COLUMNS) == COLS


def test_merge_splits_same_channel_by_country():
    metrika = [
        {"date": "2026-07-17", "country": "ОАЭ", "traffic_source": "ad",
         "source_engine": "Google Ads", "visits": 500, "users": 400},
        {"date": "2026-07-17", "country": "Катар", "traffic_source": "ad",
         "source_engine": "Google Ads", "visits": 50, "users": 40},
    ]
    orders = [
        {"date": "2026-07-17", "country": "ОАЭ", "channel": "SEM", "subchannel": "Google.Adwords",
         "traffic_type": "Платный", "orders": 10, "revenue": 1000.0},
        {"date": "2026-07-17", "country": "Катар", "channel": "SEM", "subchannel": "Google.Adwords",
         "traffic_type": "Платный", "orders": 1, "revenue": 300.0},
    ]
    rows = [dict(zip(COLS, r)) for r in merge_rows(metrika, orders, [], 20.0, "2026-07-17")]
    assert len(rows) == 2
    ae = [r for r in rows if r["country"] == "ОАЭ"][0]
    qa = [r for r in rows if r["country"] == "Катар"][0]
    assert ae["sessions"] == 500 and ae["purchases_count"] == 10 and ae["purchases_revenue"] == 20000.0
    assert qa["sessions"] == 50 and qa["purchases_count"] == 1 and qa["purchases_revenue"] == 6000.0
    assert all(r["channel"] == "SEM" for r in rows)


def test_merge_country_none_stays_separate_row():
    """Источник без гео-разбивки (расход Meta из TW summary) → country=None, идёт в GCC-тотал."""
    metrika = [{"date": "2026-07-17", "country": "ОАЭ", "traffic_source": "ad",
                "source_engine": "Instagram", "visits": 100, "users": 90}]
    spend = [{"date": "2026-07-17", "channel": "SMM paid", "subchannel": "Meta Ads",
              "traffic_type": "Платный", "cost": 500.0}]
    rows = [dict(zip(COLS, r)) for r in merge_rows(metrika, [], spend, 20.0, "2026-07-17")]
    assert len(rows) == 2
    ae = [r for r in rows if r["country"] == "ОАЭ"][0]
    total_only = [r for r in rows if r["country"] is None][0]
    assert ae["sessions"] == 100 and ae["cost"] == 0
    assert total_only["cost"] == 10000.0 and total_only["sessions"] == 0
    # GCC-тотал не пострадал: сумма по строкам = вся выручка/расход
    assert sum(r["cost"] for r in rows) == 10000.0


def test_merge_totals_preserved_across_countries():
    metrika = [
        {"date": "2026-07-17", "country": c, "traffic_source": "organic",
         "source_engine": "Google", "visits": v, "users": v}
        for c, v in (("ОАЭ", 300), ("Саудовская Аравия", 200), (None, 10))
    ]
    rows = [dict(zip(COLS, r)) for r in merge_rows(metrika, [], [], 20.0, "2026-07-17")]
    assert len(rows) == 3
    assert sum(r["sessions"] for r in rows) == 510
    assert {r["country"] for r in rows} == {"ОАЭ", "Саудовская Аравия", None}


def test_merge_backward_compatible_without_country_key():
    """Старые строки без ключа country (паритет) не падают — трактуются как тотал."""
    metrika = [{"date": "2026-07-17", "traffic_source": "direct", "source_engine": None,
                "visits": 30, "users": 25}]
    rows = [dict(zip(COLS, r)) for r in merge_rows(metrika, [], [], 20.0, "2026-07-17")]
    assert len(rows) == 1 and rows[0]["country"] is None and rows[0]["sessions"] == 30


# === Гео-расход Google: рублёвые строки не конвертируются повторно (T4) ===


def test_merge_rub_spend_rows_not_reconverted():
    """Расход Google уже в ₽ (конвертирован читателем) — курс к нему не применяем."""
    rub_spend = [{"date": "2026-07-17", "country": "ОАЭ", "channel": "SEM",
                  "subchannel": "Google.Adwords", "traffic_type": "Платный", "cost": 1000.0}]
    rows = [dict(zip(COLS, r)) for r in merge_rows([], [], [], 20.0, "2026-07-17",
                                                   rub_spend_rows=rub_spend)]
    assert len(rows) == 1
    assert rows[0]["cost"] == 1000.0        # не 20000 — повторной конвертации нет
    assert rows[0]["country"] == "ОАЭ"


def test_merge_rub_spend_joins_country_bucket_with_traffic():
    """Гео-расход склеивается с трафиком той же страны и канала в одну строку."""
    metrika = [{"date": "2026-07-17", "country": "ОАЭ", "traffic_source": "ad",
                "source_engine": "Google Ads", "visits": 500, "users": 400}]
    rub_spend = [{"date": "2026-07-17", "country": "ОАЭ", "channel": "SEM",
                  "subchannel": "Google.Adwords", "traffic_type": "Платный", "cost": 1000.0}]
    rows = [dict(zip(COLS, r)) for r in merge_rows(metrika, [], [], 20.0, "2026-07-17",
                                                   rub_spend_rows=rub_spend)]
    assert len(rows) == 1
    assert rows[0]["sessions"] == 500 and rows[0]["cost"] == 1000.0


def test_merge_aed_and_rub_spend_coexist():
    """Meta (AED из TW) и Google (₽ из кабинета) в одном дне считаются каждый по-своему."""
    aed_spend = [{"date": "2026-07-17", "channel": "SMM paid", "subchannel": "Meta Ads",
                  "traffic_type": "Платный", "cost": 100.0}]
    rub_spend = [{"date": "2026-07-17", "country": "Катар", "channel": "SEM",
                  "subchannel": "Google.Adwords", "traffic_type": "Платный", "cost": 500.0}]
    rows = [dict(zip(COLS, r)) for r in merge_rows([], [], aed_spend, 20.0, "2026-07-17",
                                                   rub_spend_rows=rub_spend)]
    meta = [r for r in rows if r["subchannel"] == "Meta Ads"][0]
    google = [r for r in rows if r["subchannel"] == "Google.Adwords"][0]
    assert meta["cost"] == 2000.0           # 100 AED × 20
    assert google["cost"] == 500.0          # уже ₽


# === Кампании: склейка трафика, заказов и расхода по id (T6) ===


def test_merge_joins_three_sources_on_campaign_id():
    """Метрика (визиты), TW (заказы) и кабинет (расход) сходятся в одну строку по id."""
    metrika = [{"date": "2026-07-17", "country": "ОАЭ", "campaign": "21087796023",
                "traffic_source": "ad", "source_engine": "Google Ads",
                "visits": 500, "users": 400}]
    orders = [{"date": "2026-07-17", "country": "ОАЭ", "campaign": "21087796023",
               "channel": "SEM", "subchannel": "Google.Adwords", "traffic_type": "Платный",
               "orders": 10, "revenue": 1000.0}]
    rub_spend = [{"date": "2026-07-17", "country": "ОАЭ", "campaign_id": "21087796023",
                  "campaign_name": "Поиск. Бренд. AE", "channel": "SEM",
                  "subchannel": "Google.Adwords", "traffic_type": "Платный", "cost": 2000.0}]
    rows = [dict(zip(COLS, r)) for r in merge_rows(metrika, orders, [], 20.0, "2026-07-17",
                                                   rub_spend_rows=rub_spend)]
    assert len(rows) == 1
    r = rows[0]
    assert r["campaign_id"] == "21087796023" and r["campaign_name"] == "Поиск. Бренд. AE"
    assert r["sessions"] == 500 and r["purchases_count"] == 10
    assert r["purchases_revenue"] == 20000.0 and r["cost"] == 2000.0


def test_merge_splits_campaigns_within_country():
    metrika = [
        {"date": "2026-07-17", "country": "ОАЭ", "campaign": "111", "traffic_source": "ad",
         "source_engine": "Google Ads", "visits": 300, "users": 250},
        {"date": "2026-07-17", "country": "ОАЭ", "campaign": "222", "traffic_source": "ad",
         "source_engine": "Google Ads", "visits": 100, "users": 90},
    ]
    rows = [dict(zip(COLS, r)) for r in merge_rows(metrika, [], [], 20.0, "2026-07-17")]
    assert len(rows) == 2
    assert {(r["campaign_id"], r["sessions"]) for r in rows} == {("111", 300), ("222", 100)}


def test_merge_rows_without_campaign_stay_empty():
    """Органика/директ кампаний не имеют — пустая строка кампании, а не выдуманная."""
    metrika = [{"date": "2026-07-17", "country": "ОАЭ", "campaign": None,
                "traffic_source": "direct", "source_engine": None, "visits": 50, "users": 45}]
    rows = [dict(zip(COLS, r)) for r in merge_rows(metrika, [], [], 20.0, "2026-07-17")]
    assert rows[0]["campaign_id"] == "" and rows[0]["campaign_name"] == ""


def test_merge_totals_survive_campaign_grain():
    """Появление кампании в ключе не меняет суммы дня."""
    metrika = [
        {"date": "2026-07-17", "country": "ОАЭ", "campaign": c, "traffic_source": "ad",
         "source_engine": "Google Ads", "visits": v, "users": v}
        for c, v in (("111", 300), ("222", 100), (None, 20))
    ]
    rows = [dict(zip(COLS, r)) for r in merge_rows(metrika, [], [], 20.0, "2026-07-17")]
    assert sum(r["sessions"] for r in rows) == 420


# === Воронка: новые пользователи, отказы, глубина (T5) ===
# Конвенция Polina: в БД bounce_rate в процентах, page_depth — среднее по строке;
# взвешивание на визиты делает хендлер (SUM(bounce_rate * visits)).


def test_merge_writes_funnel_metrics():
    metrika = [{"date": "2026-07-17", "country": "ОАЭ", "campaign": None, "traffic_source": "ad",
                "source_engine": "Google Ads", "visits": 100, "users": 80,
                "new_users": 60, "bounce_w": 30.0, "depth_w": 400.0}]
    r = dict(zip(COLS, merge_rows(metrika, [], [], 20.0, "2026-07-17")[0]))
    assert r["new_users"] == 60
    assert r["bounce_rate"] == 30.0    # 30 отказов на 100 визитов → 30%
    assert r["page_depth"] == 4.0      # 400 / 100 визитов


def test_merge_funnel_weighted_across_rows():
    """Две строки одного среза усредняются ПО ВИЗИТАМ, а не арифметически."""
    metrika = [
        {"date": "2026-07-17", "country": "ОАЭ", "campaign": None, "traffic_source": "ad",
         "source_engine": "Google Ads", "visits": 900, "users": 800,
         "new_users": 500, "bounce_w": 90.0, "depth_w": 3600.0},
        {"date": "2026-07-17", "country": "ОАЭ", "campaign": None, "traffic_source": "ad",
         "source_engine": "Google Ads", "visits": 100, "users": 90,
         "new_users": 50, "bounce_w": 50.0, "depth_w": 100.0},
    ]
    r = dict(zip(COLS, merge_rows(metrika, [], [], 20.0, "2026-07-17")[0]))
    assert r["new_users"] == 550
    assert r["bounce_rate"] == 14.0    # (90+50)/1000 → 14%, а не (10%+50%)/2
    assert r["page_depth"] == 3.7      # (3600+100)/1000


def test_merge_funnel_absent_without_traffic():
    """Строка только с расходом (нет визитов) — деления на ноль нет, поля пустые."""
    spend = [{"date": "2026-07-17", "channel": "SMM paid", "subchannel": "Meta Ads",
              "traffic_type": "Платный", "cost": 100.0}]
    r = dict(zip(COLS, merge_rows([], [], spend, 20.0, "2026-07-17")[0]))
    assert r["bounce_rate"] is None and r["page_depth"] is None and r["new_users"] == 0
