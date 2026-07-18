import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.gcc_metrika import parse_metrika_traffic


def test_parse_metrika_traffic():
    p = os.path.join(os.path.dirname(__file__), "fixtures", "metrika_traffic_sample.json")
    with open(p, encoding="utf-8") as f:
        resp = json.load(f)
    rows = parse_metrika_traffic(resp)
    assert len(rows) == len(resp["data"])
    first = rows[0]
    assert first["date"] == "2026-07-17"
    assert first["traffic_source"] == "ad"
    assert first["source_engine"] == "Google Ads"
    assert first["visits"] == 1392.0 and first["users"] == 1024.0
    # строка direct: engine None
    direct = [r for r in rows if r["traffic_source"] == "direct"][0]
    assert direct["source_engine"] is None