import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.lime_kz_cabinet import _to_rub, build_rows


def test_to_rub_rub_passthrough():
    assert _to_rub(478248.0, "RUB", "2026-07-10") == 478248.0
    assert _to_rub(100.0, "", "2026-07-10") == 100.0  # пустая валюта = рубли (Директ)
    # неизвестная валюта не гадается (возврат как есть)
    assert _to_rub(50.0, "EUR", "2026-07-10") == 50.0


def test_build_rows_maps_subchannels_and_structure():
    yandex = [{"date": "2026-07-10", "campaign_id": "709305529", "campaign_name": "ya.direct KZ (Видео)",
               "cost": 32355.0, "clicks": 1264, "impressions": 231153}]
    # currency=RUB чтобы не дёргать сеть (usd_to_rub) в тесте
    google = [{"date": "2026-07-10", "campaign_id": "111", "campaign_name": "Бренд. Поиск KZ",
               "currency": "RUB", "cost": 2177.0, "clicks": 20981, "impressions": 45587}]
    rows = build_rows(yandex, google)
    assert len(rows) == 2
    ya, go = rows
    # (date, data_source, region, channel, subchannel, traffic_type, campaign_id, campaign_name, cost, clicks, impressions, ...)
    assert ya[2] == "kz" and ya[3] == "SEM" and ya[4] == "Яндекс.Директ" and ya[5] == "Платный"
    assert ya[6] == "709305529" and ya[8] == 32355.0 and ya[9] == 1264.0 and ya[10] == 231153.0
    assert go[4] == "Google.Adwords" and go[8] == 2177.0
    # заказы/визиты нулевые (реклама-only слой)
    assert ya[11] == 0 and ya[14] == 0 and ya[15] == 0.0
