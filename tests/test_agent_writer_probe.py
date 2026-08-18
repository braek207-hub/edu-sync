# -*- coding: utf-8 -*-
from probe_bidmodifiers import classify, summarize


def test_ok_when_result_present():
    assert classify(200, {"result": {"SetResults": [{"Id": 1}]}}) == "OK"


def test_ok_when_item_level_error():
    # Ошибка уровня элемента значит, что запрос прошёл авторизацию и разбор —
    # форма верна, объекта просто нет. Тот же признак, что в probe прав записи.
    body = {"result": {"SetResults": [{"Errors": [{"Code": 8800}]}]}}
    assert classify(200, body) == "OK"


def test_rejected_on_request_level_error():
    body = {"error": {"error_code": 8000, "error_detail": "неизвестный параметр"}}
    assert classify(200, body) == "REJECTED"


def test_no_access_on_rights_error():
    assert classify(200, {"error": {"error_code": 54}}) == "NO_ACCESS"


def test_unknown_on_unexpected_shape():
    assert classify(200, {"whatever": 1}) == "UNKNOWN"


def test_summarize_lists_every_case():
    results = [{"case": "set device", "verdict": "OK"},
               {"case": "add demographics", "verdict": "REJECTED"}]
    text = summarize(results)
    assert "set device" in text and "add demographics" in text
