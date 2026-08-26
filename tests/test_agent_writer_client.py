# -*- coding: utf-8 -*-
import json

import pytest

import sync.agent.writer.client as client_module
from sync.agent.writer.client import (
    DirectWriteError,
    WriteClient,
    is_outcome_unknown,
    parse_units,
)


class _FakeResponse:
    """Минимальная замена requests.Response для тестов без сети."""

    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.headers = {}
        self.encoding = None

    def json(self):
        if self._json_body is None:
            raise ValueError("нет тела")
        return self._json_body


def test_sandbox_is_default():
    # Песочница по умолчанию: боевой кабинет требует явного решения.
    client = WriteClient("login-1")
    assert client.sandbox is True
    assert "api-sandbox" in client.base


def test_prod_base_when_sandbox_off():
    client = WriteClient("login-1", sandbox=False)
    assert client.base == "https://api.direct.yandex.com/json/v5"


def test_dry_run_is_default_and_blocks_mutation():
    client = WriteClient("login-1")
    assert client.dry_run is True
    assert client.is_write_allowed() is False


def test_write_allowed_only_with_explicit_apply():
    client = WriteClient("login-1", dry_run=False)
    assert client.is_write_allowed() is True


def test_parse_units_reads_remaining():
    # Заголовок Units: «израсходовано/осталось/суточный лимит».
    assert parse_units("10/4990/5000") == 4990


def test_parse_units_tolerates_garbage():
    assert parse_units("") is None
    assert parse_units("нечисло") is None


def test_error_keeps_service_and_code():
    err = DirectWriteError("bidmodifiers", 8000, "Некорректный запрос", "деталь")
    assert err.service == "bidmodifiers"
    assert err.code == 8000
    assert "деталь" in str(err)


def test_retryable_status_on_last_attempt_raises_not_empty_result(monkeypatch):
    # 500 на КАЖДОЙ попытке, включая последнюю: тело — валидный JSON без "error"
    # ({}), но статус остаётся retryable. Раньше код на последней попытке
    # проваливался в обычный разбор и возвращал {} как будто успех — журнал
    # действий пометил бы мутацию applied, хотя Директ её не применил.
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append(url)
        return _FakeResponse(500, json_body={}, text="upstream unavailable")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)

    client = WriteClient("login-1", dry_run=False, token="t")
    with pytest.raises(DirectWriteError):
        client.mutate("bidmodifiers", "add", {"x": 1})

    assert calls  # пустой результат вместо ошибки не возвращается


# ================== повтор — только для чтения (дефект 1)
# Транспорт переотправлял ОДНО И ТО ЖЕ тело до четырёх раз при недоступности
# сервиса, одинаково для чтения и для записи. У bidmodifiers.add нет ключа
# идемпотентности на стороне Директа, и 5xx может прийти уже ПОСЛЕ применения:
# одна сетевая икота давала в кабинете до четырёх корректировок вместо одной,
# и три из них — без строки журнала, без красной линии и без возможности
# отката (Id сохраняется только у последнего ответа).


def test_mutating_request_is_sent_exactly_once_on_unavailable_service(monkeypatch):
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append(url)
        return _FakeResponse(503, json_body={}, text="service unavailable")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)

    client = WriteClient("login-1", dry_run=False, token="t")
    with pytest.raises(DirectWriteError) as excinfo:
        client.mutate("bidmodifiers", "add", {"x": 1})

    assert len(calls) == 1, "изменяющий запрос не переотправляется"
    # И исход при этом честно неизвестен: строка уходит под наблюдение
    # сторожа, а не переприменяется как «не ушло».
    assert excinfo.value.outcome_unknown is True
    assert is_outcome_unknown(excinfo.value) is True


def test_read_is_still_retried_on_unavailable_service(monkeypatch):
    # Обратная половина правила: чтение идемпотентно, повтор ему нужен и
    # полезен — сорванное чтение состояния кабинета останавливает весь прогон.
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append(url)
        return _FakeResponse(503, json_body={}, text="service unavailable")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)

    client = WriteClient("login-1", dry_run=False, token="t")
    with pytest.raises(DirectWriteError):
        client.get("campaigns", {"x": 1})

    assert client_module.READ_RETRIES > 1
    assert len(calls) == client_module.READ_RETRIES


def test_call_refuses_retries_for_a_mutating_method(monkeypatch):
    # Страховка от будущего вызывающего кода: повторять можно только чтение,
    # и проверка идёт по МЕТОДУ, а не по флагу вызывающего.
    def fake_post(url, data=None, headers=None, timeout=None):  # pragma: no cover
        raise AssertionError("запрос не должен уйти вовсе")

    monkeypatch.setattr(client_module.requests, "post", fake_post)

    client = WriteClient("login-1", dry_run=False, token="t")
    with pytest.raises(ValueError):
        client._call("bidmodifiers", "add", {"x": 1}, retries=4)


def test_write_attempts_constant_is_a_single_attempt():
    assert client_module.WRITE_ATTEMPTS == 1


def test_retryable_status_error_carries_status_code(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        return _FakeResponse(503, json_body={}, text="service unavailable")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)

    client = WriteClient("login-1", dry_run=False, token="t")
    with pytest.raises(DirectWriteError) as excinfo:
        client.mutate("bidmodifiers", "add", {"x": 1})

    assert excinfo.value.code == 503


# ============================== исход неизвестен ≠ «запрос не ушёл»
# Дефект C: под статусом 'failed' пряталась недоступность сервиса после
# ретраев — а запрос к тому моменту уходил в Директ несколько раз, и 5xx мог
# прийти уже ПОСЛЕ применения. Такая строка обязана попасть в тот же контур,
# что и обрыв процесса, иначе изменение живёт в кабинете без наблюдения и без
# списанного риска, а diff следующего прогона его не предложит.


def test_unavailable_after_retries_is_marked_outcome_unknown(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        return _FakeResponse(503, json_body={}, text="service unavailable")

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)

    client = WriteClient("login-1", dry_run=False, token="t")
    with pytest.raises(DirectWriteError) as excinfo:
        client.mutate("bidmodifiers", "add", {"x": 1})

    assert excinfo.value.outcome_unknown is True
    assert is_outcome_unknown(excinfo.value) is True


def test_unparsable_response_is_marked_outcome_unknown(monkeypatch):
    # Ответ пришёл, но это не JSON: что Директ сделал с запросом — неизвестно.
    def fake_post(url, data=None, headers=None, timeout=None):
        return _FakeResponse(200, json_body=None, text="<html>502</html>")

    monkeypatch.setattr(client_module.requests, "post", fake_post)

    client = WriteClient("login-1", dry_run=False, token="t")
    with pytest.raises(DirectWriteError) as excinfo:
        client.mutate("bidmodifiers", "add", {"x": 1})

    assert excinfo.value.outcome_unknown is True


def test_request_level_error_from_direct_is_not_outcome_unknown(monkeypatch):
    # Директ явно ответил отказом уровня запроса — изменения точно нет.
    def fake_post(url, data=None, headers=None, timeout=None):
        return _FakeResponse(200, json_body={
            "error": {"error_code": 54, "error_string": "Нет прав"}})

    monkeypatch.setattr(client_module.requests, "post", fake_post)

    client = WriteClient("login-1", dry_run=False, token="t")
    with pytest.raises(DirectWriteError) as excinfo:
        client.mutate("bidmodifiers", "add", {"x": 1})

    assert excinfo.value.outcome_unknown is False
    assert is_outcome_unknown(excinfo.value) is False


def test_missing_token_is_not_outcome_unknown():
    # Ошибка ДО отправки: запрос не строился вовсе.
    client = WriteClient("login-1", dry_run=False)
    with pytest.raises(DirectWriteError) as excinfo:
        client.mutate("bidmodifiers", "add", {"x": 1})

    assert excinfo.value.outcome_unknown is False


# ============================ одно правило журнала на оба рабочих процесса
# Сторож в репетиции журнал не трогал сознательно, а прогон применения ровно
# в той же репетиции переводил зависшие строки в 'stale' — то есть закрывал
# их от повторной отправки и списывал за них риск-бюджет, ничего не отправив.


def test_journal_writes_only_in_prod_apply():
    from sync.agent.writer.client import journal_allowed, journal_writes_allowed

    assert journal_writes_allowed(sandbox=False, dry_run=False) is True
    assert journal_writes_allowed(sandbox=False, dry_run=True) is False   # репетиция
    assert journal_writes_allowed(sandbox=True, dry_run=False) is False   # песочница
    assert journal_writes_allowed(sandbox=True, dry_run=True) is False

    # И то же правило по клиенту — им пользуется сторож.
    assert journal_allowed(WriteClient("l", sandbox=False, dry_run=False)) is True
    assert journal_allowed(WriteClient("l", sandbox=False, dry_run=True)) is False
    assert journal_allowed(WriteClient("l", sandbox=True, dry_run=False)) is False


# ================== батчи записи: несколько элементов одним запросом


def test_batch_sends_all_elements_in_one_request(monkeypatch):
    # Уберите этот тест — и батч распадётся обратно на запрос за элементом.
    # При 50 действиях за прогон это стоило секунд, при трёхстах прогон
    # перестаёт помещаться между кроном Э1 и сверкой, и сверка объявляет
    # расхождением кабинет, который прямо сейчас дописывается.
    bodies = []

    def fake_post(url, data=None, headers=None, timeout=None):
        bodies.append(json.loads(data.decode("utf-8")))
        return _FakeResponse(200, json_body={"result": {"AddResults": [{"Id": 1},
                                                                       {"Id": 2}]}})

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    client = WriteClient("login-1", dry_run=False, token="t")

    result = client.mutate_batch("bidmodifiers", "add", "BidModifiers",
                                 [{"CampaignId": 1}, {"CampaignId": 2}])

    assert len(bodies) == 1
    assert bodies[0]["params"]["BidModifiers"] == [{"CampaignId": 1}, {"CampaignId": 2}]
    assert len(result["AddResults"]) == 2


def test_batch_refuses_to_exceed_the_request_limit():
    # Перебор элементов Директ отвергает целиком — вместе со здоровыми
    # соседями. Молча отправить «сколько дали» значит потерять весь запрос
    # из-за арифметики вызывающего кода.
    client = WriteClient("login-1", dry_run=False, token="t")
    items = [{"CampaignId": i} for i in range(client_module.BATCH_LIMIT + 1)]

    with pytest.raises(ValueError):
        client.mutate_batch("bidmodifiers", "add", "BidModifiers", items)


def test_empty_batch_never_goes_to_the_network(monkeypatch):
    # Пустой запрос — это не «ничего не делаем», это трата балла и строка
    # ошибки от Директа.
    monkeypatch.setattr(client_module.requests, "post",
                        lambda *a, **k: pytest.fail("пустой батч ушёл в сеть"))
    client = WriteClient("login-1", dry_run=False, token="t")

    with pytest.raises(ValueError):
        client.mutate_batch("bidmodifiers", "add", "BidModifiers", [])


def test_batch_in_rehearsal_stays_offline(monkeypatch):
    # Репетиция не ходит в сеть и в батче: пометка возвращается с ПОЛНЫМ
    # телом, иначе отчёт репетиции покажет один элемент вместо десяти.
    monkeypatch.setattr(client_module.requests, "post",
                        lambda *a, **k: pytest.fail("репетиция ушла в сеть"))
    client = WriteClient("login-1", dry_run=True, token="t")

    out = client.mutate_batch("bidmodifiers", "add", "BidModifiers",
                              [{"CampaignId": 1}, {"CampaignId": 2}])

    assert out["dry_run"] is True
    assert len(out["params"]["BidModifiers"]) == 2


def test_element_errors_do_not_raise_from_a_batch(monkeypatch):
    # Различение уровней сохраняется и в батче: ошибка ЭЛЕМЕНТА приезжает в
    # result (её разбирает apply.py по индексу), а исключение поднимается
    # только на ошибке уровня ЗАПРОСА. Иначе один плохой элемент откатил бы
    # девять применённых соседей.
    def fake_post(url, data=None, headers=None, timeout=None):
        return _FakeResponse(200, json_body={"result": {"AddResults": [
            {"Id": 1},
            {"Errors": [{"Code": 8800, "Message": "кампания не найдена"}]},
        ]}})

    monkeypatch.setattr(client_module.requests, "post", fake_post)
    client = WriteClient("login-1", dry_run=False, token="t")

    result = client.mutate_batch("bidmodifiers", "add", "BidModifiers",
                                 [{"CampaignId": 1}, {"CampaignId": 2}])

    assert result["AddResults"][0]["Id"] == 1
    assert result["AddResults"][1]["Errors"]


def test_split_batches_keeps_order_and_size():
    # Порядок элементов — единственное, чем ответ Директа привязан к
    # действию: результаты приходят позиционно. Перетасовка при нарезке
    # приписала бы исход одного действия другому.
    items = list(range(25))
    chunks = client_module.split_batches(items)

    assert [len(c) for c in chunks] == [10, 10, 5]
    assert [x for c in chunks for x in c] == items
