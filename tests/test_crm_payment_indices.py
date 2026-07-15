import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.crm import find_orders_index, find_revenue_index

# Заголовки листа «Оплаты» — «Дата оплаты» ЛЕВЕЕ счётчика «orders» (реальная раскладка EDU).
# Регрессия: pick_index_loose(["оплат"]) ловил «Дата оплаты» → счётчик читал дату → 0 оплат.
BASE_HEADERS = [
    "ID", "ID Сделки в Битрикс", "ID лида в SCRM", "Дата создания", "Б24 источник",
    "Источник (Utm Source)", "Ленд", "Кампания (Utm Campaign)", "Группа продуктов",
    "Ответственный", "Визуал", "Этап", "Дата получения сертификата",
    "Дата дистанционного оформления", "Дата оплаты", "Сумма (в оборот)",
    "Выручка", "orders",
]


def test_orders_not_confused_with_payment_date():
    idx = find_orders_index(BASE_HEADERS)
    assert BASE_HEADERS[idx] == "orders"
    assert "оплаты" not in BASE_HEADERS[idx].lower()  # не «Дата оплаты»


def test_revenue_not_confused_with_turnover():
    idx = find_revenue_index(BASE_HEADERS)
    assert BASE_HEADERS[idx] == "Выручка"  # не «Сумма (в оборот)»


def test_robust_to_inserted_column():
    # Вставили новый столбец в середину → «orders»/«Выручка» сдвинулись с позиций 17/18.
    shifted = BASE_HEADERS[:5] + ["Новый столбец"] + BASE_HEADERS[5:]
    assert shifted[find_orders_index(shifted)] == "orders"
    assert shifted[find_revenue_index(shifted)] == "Выручка"


def test_russian_orders_column_name():
    hdrs = ["ID лида в SCRM", "Дата оплаты", "Выручка", "Оплаты"]
    assert hdrs[find_orders_index(hdrs)] == "Оплаты"


def test_missing_returns_minus_one():
    assert find_orders_index(["ID", "Дата оплаты", "Выручка"]) == -1
