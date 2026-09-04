# -*- coding: utf-8 -*-
"""sync/agent/alerts.py — правила внутридневных тревог.

Числа в тестах взяты из замера probe_intraday_spend (run 33857247004,
04.09.2026, 12:13 МСК): четыре кабинета EDU, доли вчерашнего дня 0,2855 /
0,3042 / 0,3004 / 0,2332, молчащих кампаний ноль при 84 активных. Сторож
обязан на этом снимке МОЛЧАТЬ — тревога без события учит не читать бота.
"""
from sync.agent import alerts


# Снимок с прода: доли четырёх кабинетов в один час.
REAL_SHARES = {
    "account10": {"share": 0.2855, "today_cost": 196983.06, "yesterday_cost": 689920.0},
    "account1": {"share": 0.3042, "today_cost": 10619.27, "yesterday_cost": 34910.04},
    "account3": {"share": 0.3004, "today_cost": 219694.54, "yesterday_cost": 731249.26},
    "account4": {"share": 0.2332, "today_cost": 9481.2, "yesterday_cost": 40659.99},
}


def test_real_snapshot_is_quiet():
    # Главный тест сторожа: на нормальном дне он не говорит ничего.
    assert alerts.collapsed_accounts(REAL_SHARES) == []


def test_collapsed_account_is_caught():
    shares = dict(REAL_SHARES)
    shares["account3"] = {"share": 0.04, "today_cost": 29000.0,
                          "yesterday_cost": 731249.26}
    out = alerts.collapsed_accounts(shares)
    assert len(out) == 1
    assert out[0]["account"] == "account3"
    assert out[0]["rule"] == alerts.RULE_ACCOUNT_COLLAPSE
    assert "4 %" in out[0]["text"]


def test_collapse_median_excludes_the_suspect():
    # Кабинет, попавший в собственный контроль, тянет медиану к себе. Здесь
    # обвалившийся — крупнейший из трёх: по медиане ВСЕХ он бы себя и спрятал.
    shares = {
        "big": {"share": 0.05, "today_cost": 5.0, "yesterday_cost": 800000.0},
        "a": {"share": 0.30, "today_cost": 3.0, "yesterday_cost": 100000.0},
        "b": {"share": 0.28, "today_cost": 3.0, "yesterday_cost": 100000.0},
    }
    out = alerts.collapsed_accounts(shares)
    assert [a["account"] for a in out] == ["big"]


def test_small_account_is_not_judged_by_share():
    # У кабинета на 5 тысяч рублей одна кампания двигает долю вдвое.
    shares = dict(REAL_SHARES)
    shares["tiny"] = {"share": 0.01, "today_cost": 50.0, "yesterday_cost": 5000.0}
    assert [a["account"] for a in alerts.collapsed_accounts(shares)] == []


def test_collapse_needs_enough_peers():
    # Медиана «остальных» из одного наблюдения — не медиана.
    shares = {
        "a": {"share": 0.02, "today_cost": 1.0, "yesterday_cost": 100000.0},
        "b": {"share": 0.30, "today_cost": 1.0, "yesterday_cost": 100000.0},
    }
    assert alerts.collapsed_accounts(shares) == []


def test_stopped_campaign_is_caught():
    today = {"1": {"name": "ВУЗ_Поиск", "cost": 4000.0}}
    yday = {"1": {"name": "ВУЗ_Поиск", "cost": 9000.0},
            "2": {"name": "СПО_РСЯ", "cost": 5100.0}}
    out = alerts.stopped_campaigns("acc", today, yday)
    assert len(out) == 1
    assert out[0]["subject"] == "2"
    assert out[0]["severity"] == alerts.SEVERITY_HIGH
    assert "5 100 ₽" in out[0]["text"]
    assert "ни рубля, вчера" in out[0]["text"]


def test_yesterday_crumbs_are_not_events():
    # Хвост открутки по остаткам — не работавшая кампания.
    yday = {"2": {"name": "х", "cost": 120.0}}
    assert alerts.stopped_campaigns("acc", {}, yday) == []


def test_new_campaign_today_is_not_an_event():
    # Кампания, которой вчера не было, молчанием не считается ни при каком
    # сегодняшнем расходе: правило смотрит только на вчера активных.
    assert alerts.stopped_campaigns("acc", {"9": {"cost": 500.0}}, {}) == []


def test_stopped_sorted_by_money_lost():
    yday = {"1": {"name": "a", "cost": 1000.0}, "2": {"name": "b", "cost": 9000.0}}
    out = alerts.stopped_campaigns("acc", {}, yday)
    assert [a["subject"] for a in out] == ["2", "1"]


def test_alert_key_is_per_day_not_per_hour():
    # Кампания, вставшая утром, молчит до вечера: почасовой сторож без этого
    # прислал бы восемь одинаковых сообщений.
    alert = {"rule": "campaign_stopped", "account": "acc", "subject": "42"}
    assert (alerts.alert_key(alert, "2026-09-04")
            == alerts.alert_key(alert, "2026-09-04"))
    assert alerts.alert_key(alert, "2026-09-04") != alerts.alert_key(alert, "2026-09-05")


def test_summary_shows_top_and_counts_the_rest():
    found = [{"rule": "campaign_stopped", "severity": "high", "account": "a",
              "subject": str(i), "text": f"кампания {i} встала", "evidence": {}}
             for i in range(8)]
    text = alerts.summary(found, 14)
    assert "14:00 МСК" in text
    assert "Событий: 8." in text
    assert "кампания 0 встала" in text
    assert "и ещё 3" in text
    # Сторож обязан сказать, что он ничего не чинит сам.
    assert "не включает" in text
