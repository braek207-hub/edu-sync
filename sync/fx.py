"""Курс USD→RUB с cbr.ru для конвертации GCC-денег (Triple Whale) в рубли.

Контракт cbr.ru: XML_daily.asp?date_req=DD/MM/YYYY, windows-1251, десятичная запятая.
Курс на выходной/праздник = курс последнего рабочего дня (штатное поведение ЦБ).
"""
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
USD_ID = "R01235"
_CACHE: dict[str, float] = {}


def parse_cbr_usd(xml_text: str) -> float:
    root = ET.fromstring(xml_text)
    for val in root.findall("Valute"):
        if val.get("ID") == USD_ID:
            raw = val.findtext("Value", "").replace(",", ".").strip()
            nominal = float(val.findtext("Nominal", "1").replace(",", ".") or "1")
            return float(raw) / nominal
    raise ValueError("USD (R01235) not found in CBR response")


def usd_to_rub(date_iso: str) -> float:
    if date_iso in _CACHE:
        return _CACHE[date_iso]
    d = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    resp = requests.get(CBR_URL, params={"date_req": d}, timeout=30)
    resp.encoding = "windows-1251"
    rate = parse_cbr_usd(resp.text)
    _CACHE[date_iso] = rate
    return rate
