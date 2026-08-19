# -*- coding: utf-8 -*-
"""
tests/test_agent_e0.py — тесты расчётной стороны Э0.

Чистые функции, без сети и без БД.

Дефект 1: расчёт вычисленных настроек идёт в цикле по кабинетам, но результат
складывался в один общий список и записывался с захардкоженным идентификатором
объекта. Первичный ключ таблицы включает этот идентификатор, запись построчная —
дубликаты не падали, а тихо перетирали друг друга: в базе выживали числа того
кабинета, который дописался последним. Кабинет обязан ехать вместе с числами.
"""

import sync.agent_e0 as agent_e0


def _segment_rows(clicks: int, p_pay: float):
    return [{"segment_kind": "device", "segment_key": "MOBILE",
             "clicks": clicks, "sum_p_pay": p_pay}]


def test_computed_rows_carry_their_own_account():
    job_a = {"purpose": "computed", "login": "acc-1", "kind": "device",
             "rows": _segment_rows(clicks=1000, p_pay=0.0)}
    job_b = {"purpose": "computed", "login": "acc-2", "kind": "device",
             "rows": _segment_rows(clicks=1000, p_pay=0.0)}

    login_a, rows_a = agent_e0.computed_rows_for_job(job_a, base_conv=1.0, base_expected=2000.0)
    login_b, rows_b = agent_e0.computed_rows_for_job(job_b, base_conv=1.0, base_expected=1000.0)

    assert login_a == "acc-1"
    assert login_b == "acc-2"
    # Числа кабинетов разные (разный объём ожидаемых оплат) — именно поэтому
    # их нельзя писать под общим ключом: один набор перетирал бы другой.
    assert rows_a and rows_b
    assert rows_a[0]["value"] != rows_b[0]["value"]


def test_computed_rows_are_empty_when_segment_below_support():
    job = {"purpose": "computed", "login": "acc-1", "kind": "device",
           "rows": _segment_rows(clicks=5, p_pay=100.0)}

    login, rows = agent_e0.computed_rows_for_job(job, base_conv=1.0, base_expected=100.0)

    assert login == "acc-1"
    assert rows == []
