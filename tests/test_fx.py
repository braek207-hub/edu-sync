import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.fx import parse_cbr_rate


def test_parse_cbr_usd_via_generic():
    path = os.path.join(os.path.dirname(__file__), "fixtures", "cbr_usd_20260716.xml")
    with open(path, "r", encoding="windows-1251") as f:
        xml = f.read()
    assert parse_cbr_rate(xml, "R01235") == 78.5


def test_parse_cbr_aed_nominal_division():
    path = os.path.join(os.path.dirname(__file__), "fixtures", "cbr_aed_20260716.xml")
    with open(path, "r", encoding="windows-1251") as f:
        xml = f.read()
    assert parse_cbr_rate(xml, "R01230") == 21.35
