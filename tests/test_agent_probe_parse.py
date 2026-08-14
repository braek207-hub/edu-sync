from probe_direct_write_access import classify_write_response, experiments_endpoint_verdict


def test_write_ok_when_result_present():
    body = {"result": {"UpdateResults": [{"Id": 123}]}}
    assert classify_write_response(200, body) == "WRITE_OK"


def test_write_ok_when_item_level_error_returned():
    # Ошибка на уровне ЭЛЕМЕНТА («объект не найден») означает, что запрос прошёл
    # авторизацию: право на запись есть. Это основной ожидаемый ответ safe-режима.
    body = {"result": {"UpdateResults": [{"Errors": [{"Code": 8000, "Message": "Объект не найден"}]}]}}
    assert classify_write_response(200, body) == "WRITE_OK"


def test_read_only_when_rights_error_54():
    body = {"error": {"error_code": 54, "error_string": "Нет прав"}}
    assert classify_write_response(200, body) == "READ_ONLY"


def test_read_only_when_rights_error_513():
    body = {"error": {"error_code": 513, "error_string": "Операция недоступна"}}
    assert classify_write_response(200, body) == "READ_ONLY"


def test_no_rights_on_http_403():
    assert classify_write_response(403, {}) == "NO_RIGHTS"


def test_unknown_on_unexpected_shape():
    assert classify_write_response(200, {"whatever": 1}) == "UNKNOWN"


def test_unknown_on_other_error_code():
    body = {"error": {"error_code": 9999, "error_string": "Что-то ещё"}}
    assert classify_write_response(200, body) == "UNKNOWN"


def test_experiments_absent_on_http_404():
    assert experiments_endpoint_verdict(404, {}) == "ABSENT"


def test_experiments_available_when_result_returned():
    assert experiments_endpoint_verdict(200, {"result": {"Experiments": []}}) == "AVAILABLE"


def test_experiments_no_access_on_error_58():
    body = {"error": {"error_code": 58, "error_string": "Нет доступа к сервису"}}
    assert experiments_endpoint_verdict(200, body) == "NO_ACCESS"
