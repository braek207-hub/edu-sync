"""Курс валют→RUB с cbr.ru для конвертации GCC-денег (Triple Whale) в рубли.

Контракт cbr.ru: XML_daily.asp?date_req=DD/MM/YYYY, windows-1251, десятичная запятая.
Курс на выходной/праздник = курс последнего рабочего дня (штатное поведение ЦБ).
Поддерживает USD, AED и KZT (и любые другие валюты в CBR_IDS).
"""
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
CBR_IDS = {"USD": "R01235", "AED": "R01230", "KZT": "R01335"}
_CACHE: dict[tuple[str, str], float] = {}


def parse_cbr_rate(xml_text: str, valute_id: str) -> float:
    """Извлечь курс из XML CBR. Возвращает Value/Nominal (т.е. за 1 единицу)."""
    root = ET.fromstring(xml_text)
    for val in root.findall("Valute"):
        if val.get("ID") == valute_id:
            raw = val.findtext("Value", "").replace(",", ".").strip()
            nominal = float(val.findtext("Nominal", "1").replace(",", ".") or "1")
            return float(raw) / nominal
    raise ValueError(f"{valute_id} not found in CBR response")


def to_rub(currency: str, date_iso: str) -> float:
    """Получить курс валюты к RUB на дату. currency должна быть в CBR_IDS."""
    if currency not in CBR_IDS:
        raise ValueError(f"Unsupported currency {currency}; available: {list(CBR_IDS.keys())}")

    cache_key = (currency, date_iso)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    d = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    resp = requests.get(CBR_URL, params={"date_req": d}, timeout=30)
    resp.encoding = "windows-1251"
    rate = parse_cbr_rate(resp.text, CBR_IDS[currency])
    _CACHE[cache_key] = rate
    return rate
