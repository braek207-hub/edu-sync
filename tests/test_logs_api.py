from sync.logs_api import parse_tsv, bucket_topn, _extract_part_numbers


def test_parse_tsv_splits_header_and_rows():
    tsv = "ym:s:dateTime\tym:s:clientID\n2026-07-20 13:59:21\t123\n2026-07-20 14:00:00\t456\n"
    header, rows = parse_tsv(tsv)
    assert header == ["ym:s:dateTime", "ym:s:clientID"]
    assert rows == [["2026-07-20 13:59:21", "123"], ["2026-07-20 14:00:00", "456"]]


def test_bucket_topn_keeps_allowed_else_other():
    allowed = {"поиск", "сети"}
    assert bucket_topn("поиск", allowed) == "поиск"
    assert bucket_topn("редкая_фраза_xyz", allowed) == "other"
    assert bucket_topn("", allowed) == "other"


def test_extract_part_numbers_from_dict_parts():
    status_json = {
        "log_request": {
            "status": "processed",
            "parts": [{"part_number": 0, "size": 123}, {"part_number": 1, "size": 45}],
        }
    }
    assert _extract_part_numbers(status_json) == [0, 1]
