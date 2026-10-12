"""Вся выручка листа «Оплаты» доезжает в витрину (решение 12.10.2026).

Два класса денег, которые витрина теряла:
  1. Строки с orders=0 — доплаты по той же сделке. 617 строк на 65,8 млн ₽ отбрасывались
     целиком вместе с выручкой.
  2. Оплаты по лидам, которых нет в листах «Лиды». 57 лидов на 6,5 млн ₽ не доезжали
     вообще: приписывать оплату было не к чему.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync import crm
from sync.crm import CITY_UNKNOWN, _load_paid_by_lead_id, _payments_only_rows

HEADERS = [
    "ID", "ID Сделки в Битрикс", "ID лида в SCRM", "Дата создания", "Ленд",
    "Кампания (Utm Campaign)", "Группа продуктов", "Этап", "Дата оплаты",
    "Сумма (в оборот)", "Выручка", "orders", "date pay",
]


def _row(lead, created, pay, revenue, orders, land="vuz", cid="123456", stage="Получена оплата"):
    return ["1", "777", lead, created, land, cid, "МТИ ВО/СПО", stage, pay,
            revenue, revenue, orders, pay]


def _paid(rows, monkeypatch):
    monkeypatch.setattr(crm, "crm_payments_sheets", lambda: ["Оплаты"])
    monkeypatch.setattr(crm, "read_sheet", lambda *a, **k: [HEADERS] + rows)
    return _load_paid_by_lead_id(object(), "sid")


def test_dopllata_adds_money_not_a_payment(monkeypatch):
    paid = _paid(
        [
            _row("100", "2026-10-01", "2026-10-03", 22250, 1),
            _row("100", "2026-10-01", "2026-10-03", 54000, 0),
        ],
        monkeypatch,
    )
    assert paid["100"]["count"] == 1  # сделка одна
    assert paid["100"]["revenue"] == 22250 + 54000  # денег две строки


def test_pay_date_from_deal_row_not_from_dopllata(monkeypatch):
    """Доплата, датированная раньше сделки, не утаскивает когорту на свою дату."""
    paid = _paid(
        [
            _row("200", "2026-09-20", "2026-09-28", 150000, 1),
            _row("200", "2026-09-20", "2026-09-24", 50000, 0),
        ],
        monkeypatch,
    )
    assert paid["200"]["pay_date"] == "2026-09-28"
    assert paid["200"]["revenue"] == 200000


def test_descriptive_fields_come_from_deal_row(monkeypatch):
    paid = _paid(
        [
            _row("300", "2026-08-01", "2026-08-05", 90000, 1, stage="Получена оплата"),
            _row("300", "2026-08-01", "2026-08-02", 10000, 0, stage="Недозвон"),
        ],
        monkeypatch,
    )
    assert paid["300"]["payment_stage"] == "Получена оплата"


def test_lead_fields_captured_for_payments_only(monkeypatch):
    paid = _paid([_row("400", "2026-08-10", "2026-08-22", 185000, 1, land="vuz_new",
                       cid="714057545")], monkeypatch)
    assert paid["400"]["created_date"] == "2026-08-10"
    assert paid["400"]["campaign_id"] == "714057545"
    assert paid["400"]["land"] == "vuz_new"


def test_payments_only_builds_full_chain(monkeypatch):
    paid = _paid([_row("500", "2026-08-10", "2026-08-22", 185000, 1)], monkeypatch)
    agg, details = _payments_only_rows(paid, set())

    assert len(details) == 1
    d = details[0]
    assert (d["is_eff"], d["is_connected"], d["is_deal"], d["is_paid"]) == (True,) * 4
    assert d["created_date"] == "2026-08-10"
    assert d["payment_date"] == "2026-08-22"
    assert d["amount"] == 185000
    # Чего в листе оплат нет — «нет данных», а не выдуманное значение.
    assert d["city_ip_segment"] == CITY_UNKNOWN
    # Без этой пометки прогон принял бы строку за настоящую и перестал её пересобирать.
    assert d["is_synthetic"] is True
    assert d["connection_date"] is None and d["stage"] is None

    bucket = next(iter(agg.values()))
    assert (bucket["leads"], bucket["connections"], bucket["deals"]) == (1, 1.0, 1.0)
    assert bucket["payments_from_leads"] == 1
    assert bucket["revenue_from_leads"] == 185000
    assert bucket["days_to_pay_count"] == 1 and bucket["days_to_pay_sum"] == 12


def test_payments_only_skips_known_lead(monkeypatch):
    """Лид уже есть в листах или в базе → настоящую строку не трогаем."""
    paid = _paid([_row("600", "2026-08-10", "2026-08-22", 185000, 1)], monkeypatch)
    agg, details = _payments_only_rows(paid, {"600"})
    assert (agg, details) == ({}, [])


def test_payments_only_keeps_rows_older_than_sheets(monkeypatch):
    """Лид старше прочитанных листов доезжает: удаляемый диапазон задан отдельно.

    Раньше такую строку откладывали «до полного прогона», и полный откладывал её тоже —
    3 лида на 757 500 ₽ висели вне витрины.
    """
    paid = _paid([_row("700", "2024-09-27", "2025-07-06", 135000, 1)], monkeypatch)
    agg, details = _payments_only_rows(paid, set())
    assert [d["created_date"] for d in details] == ["2024-09-27"]


def test_synthetic_rows_are_rebuilt_not_skipped():
    """Прошлая синтетическая строка не попадает в skip: SQL отбирает только настоящие.

    Регрессия 12.10.2026: второй полный прогон недосчитал 5 020 296 ₽ — синтетические
    строки первого прогона попали в skip по lead_id, не были собраны заново, а DELETE
    диапазона их снёс.
    """
    import inspect

    from sync import db

    assert "WHERE NOT is_synthetic" in inspect.getsource(db.existing_real_lead_ids)
    assert "is_synthetic" in inspect.getsource(db.upsert_lead_details)


def test_delete_window_comes_from_sheets_not_from_batch():
    """Границу удаления синк задаёт отдельным аргументом, иначе достроенная строка 2024
    расширила бы DELETE на всю историю."""
    import inspect

    from sync import crm, db

    assert "delete_from" in inspect.signature(db.upsert_lead_details).parameters
    assert 'delete_from or min(str(r["created_date"]) for r in rows)' in inspect.getsource(
        db.upsert_lead_details
    )
    assert "delete_from=sheets_min_created" in inspect.getsource(crm.sync_crm_leads)
