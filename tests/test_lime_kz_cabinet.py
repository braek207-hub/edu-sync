import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.lime_kz_cabinet import _to_rub, build_rows


def test_to_rub_rub_passthrough():
    assert _to_rub(478248.0, "RUB", "2026-07-10") == 478248.0
    assert _to_rub(100.0, "", "2026-07-10") == 100.0  # пустая валюта = рубли
    assert _to_rub(50.0, "ZZZ", "2026-07-10") == 50.0  # неизвестная не гадается


def test_build_rows_google_only_structure():
    # Google KZ → строки lime_stats (region=kz, subchannel=Google.Adwords, currency=RUB чтобы без сети)
    google = [{"date": "2026-07-10", "campaign_id": "111", "campaign_name": "Бренд. Поиск KZ",
               "currency": "RUB", "cost": 2177.0, "clicks": 20981, "impressions": 45587}]
    rows = build_rows(google)
    assert len(rows) == 1
    r = rows[0]
    # (date, data_source, region, channel, subchannel, traffic_type, campaign_id, campaign_name, cost, clicks, impressions, sessions, users, ...)
    assert r[2] == "kz" and r[3] == "SEM" and r[4] == "Google.Adwords" and r[5] == "Платный"
    assert r[6] == "111" and r[7] == "Бренд. Поиск KZ"
    assert r[8] == 2177.0 and r[9] == 20981.0 and r[10] == 45587.0
    assert r[12] == 0 and r[13] == 0  # users, clients = 0 (нет MySQL-атрибуции у Google KZ)
