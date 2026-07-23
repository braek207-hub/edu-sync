import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.utils import normalize_campaign_id, to_iso_date, to_iso_datetime, to_num


def test_normalize_campaign_id():
    assert normalize_campaign_id(" 12345.0 ") == "12345"
    assert normalize_campaign_id("abc") == "abc"
    assert normalize_campaign_id("") == ""


def test_to_iso_date_formats():
    assert to_iso_date("2024-03-15") == "2024-03-15"
    assert to_iso_date("15.03.2024") == "2024-03-15"
    assert to_iso_date("15.03.2024 14:30:00") == "2024-03-15"
    assert to_iso_date("20240315") == "2024-03-15"


def test_pick_index_prefers_longer_header():
    from sync.utils import pick_index_loose

    h = ["ID", "Дата создания", "Б24 дата соединения на ОП", "UTM Campaign"]
    assert pick_index_loose(h, ["date created", "дата создания", "дата"]) == 1


def test_to_num():
    assert to_num("1 234,56") == 1234.56
    assert to_num("") == 0


def test_iso_datetime_dotted():
    assert to_iso_datetime("01.07.2026 10:24") == "2026-07-01T10:24:00"


def test_iso_datetime_dotted_seconds():
    assert to_iso_datetime("01.07.2026 10:24:35") == "2026-07-01T10:24:35"


def test_iso_datetime_date_only():
    assert to_iso_datetime("01.07.2026") == "2026-07-01T00:00:00"


def test_iso_datetime_empty():
    assert to_iso_datetime("") == "" and to_iso_datetime(None) == ""
