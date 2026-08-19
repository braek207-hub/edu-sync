# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_apply.py — тесты преобразования действий в вызовы API
и применения действий (журнал → отправка → отметка результата).

Фейковые client/db_module по протоколу реальных (client.mutate,
db_module.find_action_by_key/insert_action/mark_action) — без сети и без БД.
"""

from typing import Any, Dict, List, Optional

from sync.agent.writer.apply import apply_actions, to_api_call


def test_add_maps_to_bidmodifiers_add_with_mobile_shape():
    action = {"action_kind": "bidmodifier.add",
              "payload": {"CampaignId": 111, "Type": "MOBILE_ADJUSTMENT",
                          "key": "mobile", "BidModifier": 30}}
    service, method, params = to_api_call(action)
    assert (service, method) == ("bidmodifiers", "add")
    item = params["BidModifiers"][0]
    assert item["CampaignId"] == 111
    assert item["MobileAdjustment"]["BidModifier"] == 30


def test_add_maps_demographics_shape():
    action = {"action_kind": "bidmodifier.add",
              "payload": {"CampaignId": 111, "Type": "DEMOGRAPHICS_ADJUSTMENT",
                          "key": "GENDER_MALE", "BidModifier": 20}}
    service, method, params = to_api_call(action)
    item = params["BidModifiers"][0]
    assert item["DemographicsAdjustments"][0]["Gender"] == "GENDER_MALE"
    assert item["DemographicsAdjustments"][0]["BidModifier"] == 20


def test_set_maps_to_bidmodifiers_set():
    action = {"action_kind": "bidmodifier.set", "payload": {"Id": 7, "BidModifier": 30}}
    service, method, params = to_api_call(action)
    assert (service, method) == ("bidmodifiers", "set")
    assert params["BidModifiers"][0] == {"Id": 7, "BidModifier": 30}


def test_unknown_action_kind_raises():
    import pytest
    with pytest.raises(ValueError):
        to_api_call({"action_kind": "campaign.delete", "payload": {}})


# ------------------------------------------------------------- apply_actions


def _action(key: str = "k1") -> Dict[str, Any]:
    return {
        "action_kind": "bidmodifier.set",
        "object_level": "campaign",
        "object_id": "111",
        "payload": {"Id": 7, "BidModifier": 30},
        "previous_state": {"Id": 7, "percent": 10},
        "idempotency_key": key,
        "account": "test-login",
        "risk_rub": 100.0,
        "red_line": {},
    }


class _FakeDB:
    """Минимальная замена sync.agent.writer.db по протоколу apply_actions.

    insert_action повторяет ON CONFLICT (idempotency_key) DO NOTHING реального
    кода: вторая вставка того же ключа не трогает уже существующую строку.
    """

    def __init__(self, seed: Optional[Dict[str, Dict[str, Any]]] = None):
        self.rows: Dict[str, Dict[str, Any]] = dict(seed or {})
        self.events: List[str] = []

    def find_action_by_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        return self.rows.get(idempotency_key)

    def insert_action(self, row: Dict[str, Any]) -> str:
        self.events.append(f"insert:{row['idempotency_key']}")
        key = row["idempotency_key"]
        if key not in self.rows:
            self.rows[key] = {"status": "planned"}
        return key  # action_id — в тестах достаточно самого ключа

    def mark_action(self, action_id: str, status: str, response: Dict[str, Any]) -> None:
        self.events.append(f"mark:{action_id}:{status}")
        self.rows[action_id]["status"] = status
        self.rows[action_id]["response"] = response


class _FakeClient:
    def __init__(self, response: Optional[Dict[str, Any]] = None,
                 raises: Optional[Exception] = None):
        self._response = response if response is not None else {}
        self._raises = raises
        self.calls: List[tuple] = []

    def mutate(self, service: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((service, method, params))
        if self._raises is not None:
            raise self._raises
        return self._response


def test_apply_actions_marks_applied_on_clean_success():
    db = _FakeDB()
    client = _FakeClient(response={"SetResults": [{"Id": 7}]})

    report = apply_actions(client, [_action()], db)

    assert report == {"applied": 1, "skipped": 0, "failed": 0, "rejected": 0,
                       "details": [{"key": "k1", "result": "applied"}]}
    assert db.rows["k1"]["status"] == "applied"
    assert len(client.calls) == 1


def test_apply_actions_element_level_error_is_rejected_not_applied():
    # Транспорт не поднимает исключение на ошибку уровня элемента (см. client.py) —
    # HTTP 200, но result.SetResults[0].Errors непустой: API принял запрос и
    # отклонил элемент. Это не должно засчитаться как applied.
    db = _FakeDB()
    client = _FakeClient(response={
        "SetResults": [{"Errors": [{"Code": 8800, "Message": "кампания не найдена"}]}]
    })

    report = apply_actions(client, [_action()], db)

    assert report["applied"] == 0
    assert report["rejected"] == 1
    assert report["failed"] == 0
    assert db.rows["k1"]["status"] == "rejected"
    assert report["details"][0]["result"] == "rejected"


def test_apply_actions_add_element_level_error_uses_add_results():
    action = {**_action(), "action_kind": "bidmodifier.add",
              "payload": {"CampaignId": 111, "Type": "MOBILE_ADJUSTMENT",
                          "key": "mobile", "BidModifier": 30}}
    db = _FakeDB()
    client = _FakeClient(response={
        "AddResults": [{"Errors": [{"Code": 8800, "Message": "кампания не найдена"}]}]
    })

    report = apply_actions(client, [action], db)

    assert report["rejected"] == 1
    assert report["applied"] == 0


def test_apply_actions_repeat_run_skips_already_applied():
    db = _FakeDB(seed={"k1": {"status": "applied"}})
    client = _FakeClient(response={"SetResults": [{"Id": 7}]})

    report = apply_actions(client, [_action()], db)

    assert report == {"applied": 0, "skipped": 1, "failed": 0, "rejected": 0,
                       "details": [{"key": "k1", "result": "skipped"}]}
    assert client.calls == []  # запрос не ушёл второй раз


def test_apply_actions_repeat_run_does_not_skip_rejected():
    # Отклонённое элементом действие не входит в {'applied','rolled_back'} —
    # оно обязано переприменяться, а не застревать без диагностики.
    db = _FakeDB(seed={"k1": {"status": "rejected"}})
    client = _FakeClient(response={"SetResults": [{"Id": 7}]})

    report = apply_actions(client, [_action()], db)

    assert report["skipped"] == 0
    assert report["applied"] == 1
    assert len(client.calls) == 1


def test_apply_actions_exception_on_send_marks_failed_and_stays_in_journal():
    db = _FakeDB()
    client = _FakeClient(raises=RuntimeError("сеть недоступна"))

    report = apply_actions(client, [_action()], db)

    assert report["failed"] == 1
    assert report["applied"] == 0
    # Журнал ОБЯЗАН содержать запись — insert_action прошёл до отправки,
    # исключение при mutate не стирает planned-строку, а обновляет её статус.
    assert db.rows["k1"]["status"] == "failed"
    assert "сеть недоступна" in report["details"][0]["error"]


def test_apply_actions_dry_run_does_not_send_request():
    db = _FakeDB()
    client = _FakeClient(response={"dry_run": True, "service": "bidmodifiers",
                                    "method": "set", "params": {}})

    report = apply_actions(client, [_action()], db)

    assert report["applied"] == 0
    assert report["rejected"] == 0
    assert db.rows["k1"]["status"] == "dry_run"
    # dry_run в этом фейке всё равно "вызывает" mutate (как реальный WriteClient —
    # решение не слать запрос принимается ВНУТРИ mutate, apply_actions его не знает).
    assert len(client.calls) == 1


def test_apply_actions_journal_write_happens_before_api_call():
    # Порядок обязателен: сначала insert_action (planned + previous_state),
    # ПОТОМ mutate. Если процесс упадёт между ними, действие останется
    # видимым в статусе planned; в обратном порядке изменение в кабинете
    # осталось бы без следа и без возможности отката.
    order: List[str] = []

    class _OrderedDB(_FakeDB):
        def insert_action(self, row):
            order.append("insert")
            return super().insert_action(row)

    class _OrderedClient(_FakeClient):
        def mutate(self, service, method, params):
            order.append("mutate")
            return super().mutate(service, method, params)

    db = _OrderedDB()
    client = _OrderedClient(response={"SetResults": [{"Id": 7}]})

    apply_actions(client, [_action()], db)

    assert order == ["insert", "mutate"]
