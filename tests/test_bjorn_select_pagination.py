"""select_date_range листает PostgREST по 1000 строк — порядок обязан быть полным.

17.10.2026 перезалив BJORN за 26 дней (3204 строки staging) удвоил в витрине строки
15.10: при order=date.asc строки внутри дня идут в произвольном порядке, и offset
отдаёт одну строку на двух страницах, а другую — ни на одной. Ночной 7-дневный
прогон умещался в одну страницу и бага не видел.
"""

import pytest

from sync.bjorn import common


class Resp:
    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return self._rows


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "k")
    return common.SupabaseRest()


def test_order_is_full_primary_key(client, monkeypatch):
    seen = []
    monkeypatch.setattr(client, "request", lambda method, path, *, params: seen.append(params) or Resp([]))

    client.select_date_range("stg_metrica_daily_campaign", "2026-09-21", "2026-10-16")

    assert seen[0]["order"] == "date.asc,attribution.asc,source.asc,source_detail.asc,utm_campaign.asc"


def test_every_caller_table_has_a_key(client, monkeypatch):
    monkeypatch.setattr(client, "request", lambda method, path, *, params: Resp([]))
    for table in ("stg_metrica_daily_campaign", "stg_direct_daily_campaign", "marketing_daily_fact", "bjorn_product_daily"):
        client.select_date_range(table, "2026-10-01", "2026-10-02")


def test_unknown_table_refused(client):
    with pytest.raises(KeyError):
        client.select_date_range("some_new_table", "2026-10-01", "2026-10-02")
