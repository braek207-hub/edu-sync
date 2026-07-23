import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# googleapiclient optional locally
sys.modules.setdefault("googleapiclient", MagicMock())
sys.modules.setdefault("googleapiclient.discovery", MagicMock())

from sync.crm import _sync_leads_raw

HEADERS = [
    "ID", "date created", "UTM Campaign", "Ленд", "UTM Term",
    "Этап", "Ответственный", "Диспетчер", "Подразделение",
    "Город (IP)", "Б24 год выпуска", "Б24 уровень образования",
    "Yandex Client ID", "Родитель", "connect", "Сделка", "Оплаты",
    "Б24 дата соединения",
]

VALUES = [
    HEADERS,
    # A: оплата по join, соединение+сделка, родитель, Москва, eff-статус
    ["31995795", "01.06.2026", "705494889", "vsekolledzhi_postupi", "колледж оренбург",
     "Сделка создана", "Иванов И.", "Петров П.", "КЦ1",
     "Москва", "2008", "университеты 9 класс", "1767172592923760252", "Да",
     "1", "1", "0", "05.06.2026"],
    # B: не соединён/сделка/оплата, junk-статус (Повтор), Симферополь, без client_id
    ["31995860", "01.06.2026", "705494889", "vsekolledzhi_postupi", "---autotargeting",
     "Повтор", "Сидоров С.", "", "КЦ2",
     "Симферополь", "", "высшее образование", "", "",
     "0", "0", "0", ""],
    # C: соединён, без сделки, без оплаты (нет join; флаг «Оплаты» агрегат игнорирует), eff
    ["31996115", "02.06.2026", "705494889", "vuz", "заочное обучение",
     "Недозвон", "Иванов И.", "", "МТИ",
     "Одинцово", "2026", "колледж", "1767221857108928672", "Нет",
     "1", "0", "0", "06.06.2026"],
]

PAID = {
    "31995795": {
        "count": 1, "revenue": 205000.0, "pay_date": "2026-06-17",
        "amount_turnover": 205000.0, "deal_id": "10133519",
        "payment_stage": "Получена оплата", "utm_source": "yandex_s",
        "product": "СПО/СПЦ/Строительство", "product_group": "ВО/СПО",
        "cert_date": "2026-06-17",
    },
}


def _by_id(details):
    return {d["lead_id"]: d for d in details}


def test_lead_details_extracted_with_all_fields():
    _agg, _dims, details = _sync_leads_raw(HEADERS, VALUES, paid_by_lead_id=PAID)
    assert len(details) == 3
    d = _by_id(details)

    a = d["31995795"]
    assert a["client_id"] == "1767172592923760252"
    assert a["campaign_id"] == "705494889"
    assert a["land"] == "vsekolledzhi_postupi"
    assert a["utm_term"] == "колледж оренбург"
    assert a["created_date"] == "2026-06-01"
    assert a["connection_date"] == "2026-06-05"
    assert a["stage"] == "Сделка создана"
    assert a["responsible"] == "Иванов И."
    assert a["dispatcher"] == "Петров П."
    assert a["subdivision"] == "КЦ1"
    assert a["city_raw"] == "Москва"
    assert a["city_ip_segment"] == "msk_mo"
    assert a["audience"] == "parent"
    assert a["is_eff"] is True
    assert a["is_connected"] is True
    assert a["is_deal"] is True
    assert a["is_paid"] is True
    # поля сделки/оплаты из join
    assert a["payment_date"] == "2026-06-17"
    assert a["amount"] == 205000.0
    assert a["amount_turnover"] == 205000.0
    assert a["deal_id"] == "10133519"
    assert a["payment_stage"] == "Получена оплата"
    assert a["utm_source"] == "yandex_s"
    assert a["product"] == "СПО/СПЦ/Строительство"
    assert a["product_group"] == "ВО/СПО"
    assert a["cert_date"] == "2026-06-17"

    b = d["31995860"]
    assert b["client_id"] is None
    assert b["city_ip_segment"] == "rf"
    assert b["audience"] == "unknown"
    assert b["is_eff"] is False   # junk-статус «Повтор»
    assert b["is_connected"] is False
    assert b["is_deal"] is False
    assert b["is_paid"] is False
    assert b["amount"] is None     # нет join

    c = d["31996115"]
    assert c["city_ip_segment"] == "msk_mo"  # Одинцово → МСК+МО
    assert c["audience"] == "pupil"          # «Нет» → школьник
    assert c["is_connected"] is True
    assert c["is_deal"] is False
    # Оплаты берутся только из join (лист «Оплаты»); флаг «Оплаты» в «Лидах» агрегат
    # игнорирует (alias payment_flag не матчит «Оплаты») → паритет с ячейкой дашборда.
    assert c["is_paid"] is False
    assert c["amount"] is None


def test_lead_details_ts_columns():
    """Ф2: created_ts/connected_ts несут время (не только дату), в отличие от
    created_date/connection_date. Дата-без-времени → T00:00:00 (паритет со старым полем)."""
    values_with_time = [
        HEADERS,
        # строка со временем в обеих датах
        ["40000001", "01.06.2026 10:24", "705494889", "vuz", "заочное обучение",
         "Сделка создана", "Иванов И.", "Петров П.", "КЦ1",
         "Москва", "2008", "университеты 9 класс", "", "Да",
         "1", "1", "0", "05.06.2026 11:30"],
    ]
    _agg, _dims, details = _sync_leads_raw(values_with_time[0], values_with_time)
    d = _by_id(details)
    row = d["40000001"]
    assert row["created_ts"] == "2026-06-01T10:24:00"
    assert row["connected_ts"] == "2026-06-05T11:30:00"

    # дата-без-времени (существующая фикстура VALUES: строка A) → T00:00:00
    _agg2, _dims2, details2 = _sync_leads_raw(HEADERS, VALUES, paid_by_lead_id=PAID)
    d2 = _by_id(details2)
    assert d2["31995795"]["created_ts"] == "2026-06-01T00:00:00"
    assert d2["31995795"]["connected_ts"] == "2026-06-05T00:00:00"

    # без даты соединения (строка B) → None
    assert d2["31995860"]["connected_ts"] is None


def test_lead_details_flags_parity_with_aggregate():
    """Σ per-lead флагов == агрегат по бакетам (гейт корректности drill-down)."""
    agg, _dims, details = _sync_leads_raw(HEADERS, VALUES, paid_by_lead_id=PAID)

    agg_leads = sum(b["leads"] for b in agg.values())
    agg_conn = sum(b["connections"] for b in agg.values())
    agg_deals = sum(b["deals"] for b in agg.values())
    agg_pay = sum(b["payments_from_leads"] for b in agg.values())
    agg_eff = sum(b["eff_leads"] for b in agg.values())

    assert len(details) == agg_leads
    assert sum(1 for x in details if x["is_connected"]) == agg_conn
    assert sum(1 for x in details if x["is_deal"]) == agg_deals
    assert sum(1 for x in details if x["is_paid"]) == agg_pay
    assert sum(1 for x in details if x["is_eff"]) == agg_eff
