import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.fx import parse_cbr_usd


def test_parse_cbr_usd():
    path = os.path.join(os.path.dirname(__file__), "fixtures", "cbr_usd_20260716.xml")
    with open(path, "r", encoding="windows-1251") as f:
        xml = f.read()
    assert parse_cbr_usd(xml) == 78.5
