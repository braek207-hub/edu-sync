import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.fx import CBR_IDS, parse_cbr_rate


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


def test_parse_cbr_rate_kzt_divides_by_nominal():
    """ЦБ публикует тенге за 100 единиц — курс за 1 тенге = Value/Nominal."""
    xml = """<?xml version="1.0" encoding="windows-1251"?>
    <ValCurs Date="17.07.2026" name="Foreign Currency Market">
      <Valute ID="R01335">
        <NumCode>398</NumCode><CharCode>KZT</CharCode>
        <Nominal>100</Nominal><Name>Казахстанских тенге</Name><Value>16,2000</Value>
      </Valute>
    </ValCurs>"""
    assert parse_cbr_rate(xml, "R01335") == 0.162


def test_kzt_registered_in_cbr_ids():
    assert CBR_IDS["KZT"] == "R01335"
