from unittest.mock import patch, MagicMock
from sync import appmetrica_logs as al


def _resp(status, json_body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_body or {}
    return r


def test_export_polls_until_200():
    # 202 (готовится) → 200 (данные)
    seq = [_resp(202), _resp(200, {"data": [{"appmetrica_device_id": "d1"}]})]
    with patch.object(al.requests, "get", side_effect=seq) as g, \
         patch.object(al.time, "sleep"):
        rows = al._export("installations", {"application_id": "1"}, "tok")
    assert rows == [{"appmetrica_device_id": "d1"}]
    assert g.call_count == 2
    # OAuth-заголовок присутствует
    assert g.call_args.kwargs["headers"]["Authorization"] == "OAuth tok"


def test_fetch_installations_sets_fields_and_range():
    with patch.object(al, "_export", return_value=[{"appmetrica_device_id": "d1"}]) as ex:
        rows = al.fetch_installations("4415407", "tok", "2026-01-01", "2026-01-31")
    assert rows == [{"appmetrica_device_id": "d1"}]
    params = ex.call_args.args[1]
    assert params["application_id"] == "4415407"
    assert params["date_since"].startswith("2026-01-01")
    assert params["date_until"].startswith("2026-01-31")
    assert "publisher_name" in params["fields"]
    assert "install_datetime" in params["fields"]


def test_export_waits_on_429_queue_full():
    # 429 — очередь приложения занята соседними запросами: ждём, а не падаем
    seq = [_resp(429), _resp(202), _resp(200, {"data": [{"appmetrica_device_id": "d1"}]})]
    with patch.object(al.requests, "get", side_effect=seq) as g, \
         patch.object(al.time, "sleep"):
        rows = al._export("installations", {"application_id": "1"}, "tok")
    assert rows == [{"appmetrica_device_id": "d1"}]
    assert g.call_count == 3


def test_fetch_installations_one_file_per_month():
    with patch.object(al, "_export", side_effect=[[{"d": 1}], [{"d": 2}], [{"d": 3}]]) as ex:
        rows = al.fetch_installations("4415407", "tok", "2026-08-15", "2026-10-06")
    assert rows == [{"d": 1}, {"d": 2}, {"d": 3}]
    ranges = [(c.args[1]["date_since"], c.args[1]["date_until"]) for c in ex.call_args_list]
    assert ranges == [
        ("2026-08-15 00:00:00", "2026-08-31 23:59:59"),
        ("2026-09-01 00:00:00", "2026-09-30 23:59:59"),
        ("2026-10-01 00:00:00", "2026-10-06 23:59:59"),
    ]
