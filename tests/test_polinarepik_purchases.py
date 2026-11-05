"""Покупки Метрики Polina: модель automatic и рекламная система вторым запросом (с clientID несовместима)."""
from sync import polinarepik


def _dims(*names):
    return [{"name": n} for n in names]


def test_purchases_use_automatic_and_merge_adv_engine(monkeypatch):
    monkeypatch.setenv("POLINAREPIK_YANDEX_TOKEN", "t")
    monkeypatch.delenv("POLINAREPIK_PURCHASES_ATTRIBUTION", raising=False)
    sent = []

    def fake_get(params, token):
        sent.append(params)
        if "ym:s:clientID" in params["dimensions"]:
            return {"data": [
                {"dimensions": _dims("2026-10-09", "1627", "cid1", "Переходы по рекламе", None, None, None),
                 "metrics": [1, 7495]},
                {"dimensions": _dims("2026-10-05", "1608", "cid2", "Переходы по рекламе", "yandex", "cpc", "709242097"),
                 "metrics": [1, 6375]},
            ]}
        return {"data": [
            {"dimensions": _dims("2026-10-09", "1627", "Яндекс Директ: Не определено"), "metrics": [1, 7495]},
            # Другая дата того же номера (тестовый стенд) — к покупке не цепляется.
            {"dimensions": _dims("2026-08-18", "1608", "Google Ads"), "metrics": [1, 1]},
        ]}

    monkeypatch.setattr(polinarepik, "_metrica_get", fake_get)
    rows = {r["order_id"]: r for r in polinarepik.fetch_metrica_purchases("2026-10-01", "2026-10-31")}

    assert {p["attribution"] for p in sent} == {"automatic"}
    assert all("<attribution>" in p["dimensions"] for p in sent)
    assert "ym:s:clientID" not in sent[1]["dimensions"]
    assert rows["1627"]["adv_engine"] == "Яндекс Директ: Не определено"
    assert rows["1627"]["utm_campaign"] == ""
    assert rows["1608"]["adv_engine"] == ""
    assert rows["1608"]["utm_campaign"] == "709242097"
