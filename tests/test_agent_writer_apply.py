# -*- coding: utf-8 -*-
"""
tests/test_agent_writer_apply.py — тесты преобразования действий в вызовы API
и применения действий (журнал → отправка → отметка результата).

Клиент везде фейковый (client.mutate) — сеть не нужна ни одному тесту.

База — двух видов, и это важно. Счётчики отчёта и порядок вызовов проверяются
на фейковом журнале: он ничего не решает, только записывает. А контракт журнала
(что переписывается повторной планировкой, а что неприкосновенно) проверяется
на РЕАЛЬНОМ модуле работы с базой, под гейтом DATABASE_URL. Раньше фейк
воспроизводил этот контракт руками — и «поведенческие» тесты проходили на
сломанном коде, потому что проверяли сами себя.
"""

import os
import uuid
from typing import Any, Dict, List, Optional

import pytest
import requests

import sync.agent.writer.db as writer_db
from sync.agent.writer.apply import _element_errors, apply_actions, to_api_call
from sync.agent.writer.client import DirectWriteError, is_outcome_unknown
from sync.agent.writer.db import ensure_writer_tables
from sync.db import get_connection

live_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL")


# payload несёт ДЕЛЬТУ (30 = «+30 %»), в тело запроса уходит 100-базный
# коэффициент Директа (130). Граница конверсии — sync/agent/writer/units.py,
# сквозные тесты шкалы — tests/test_agent_writer_units.py.


def test_add_maps_to_bidmodifiers_add_with_mobile_shape():
    action = {"action_kind": "bidmodifier.add",
              "payload": {"CampaignId": 111, "Type": "MOBILE_ADJUSTMENT",
                          "key": "MOBILE", "BidModifier": 30}}
    service, method, params = to_api_call(action)
    assert (service, method) == ("bidmodifiers", "add")
    item = params["BidModifiers"][0]
    assert item["CampaignId"] == 111
    assert item["MobileAdjustment"]["BidModifier"] == 130


def test_add_maps_desktop_and_tablet_to_their_own_shapes():
    for direct_type, api_field in (("DESKTOP_ADJUSTMENT", "DesktopAdjustment"),
                                   ("TABLET_ADJUSTMENT", "TabletAdjustment")):
        action = {"action_kind": "bidmodifier.add",
                  "payload": {"CampaignId": 111, "Type": direct_type,
                              "key": api_field, "BidModifier": 30}}
        item = to_api_call(action)[2]["BidModifiers"][0]
        assert item[api_field]["BidModifier"] == 130
        assert "MobileAdjustment" not in item


def test_add_maps_demographics_shape():
    action = {"action_kind": "bidmodifier.add",
              "payload": {"CampaignId": 111, "Type": "DEMOGRAPHICS_ADJUSTMENT",
                          "key": "GENDER_MALE", "BidModifier": 20}}
    service, method, params = to_api_call(action)
    item = params["BidModifiers"][0]
    assert item["DemographicsAdjustments"][0]["Gender"] == "GENDER_MALE"
    assert item["DemographicsAdjustments"][0]["BidModifier"] == 120


def test_set_maps_to_bidmodifiers_set():
    action = {"action_kind": "bidmodifier.set", "payload": {"Id": 7, "BidModifier": 30}}
    service, method, params = to_api_call(action)
    assert (service, method) == ("bidmodifiers", "set")
    assert params["BidModifiers"][0] == {"Id": 7, "BidModifier": 130}


def test_unknown_action_kind_raises():
    with pytest.raises(ValueError):
        to_api_call({"action_kind": "campaign.delete", "payload": {}})


def test_element_errors_unknown_method_raises_instead_of_silent_no_errors():
    # Правка по код-ревью: тот же дефект, что уже ловился для add/set (отклонённое
    # API действие уходило в 'applied' и навсегда застревало за детерминированным
    # идемпотентным ключом) — теперь ждёт любой будущий вид операции, если method
    # не попал в _RESULT_COLLECTION. Неразобранный ответ обязан упасть, а не молча
    # вернуть "ошибок нет".
    with pytest.raises(ValueError):
        _element_errors("delete", {"DeleteResults": [{"Errors": []}]})


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
    """Журнал-протокол: только записывает, ничего не решает.

    Никакой логики ON CONFLICT здесь НЕТ намеренно. Пока фейк повторял
    контракт реального INSERT, тесты «повтор несёт свежие данные» и
    «применённое не переписывается» проверяли именно этот повтор — и зеленели
    на сломанном коде. Контракт журнала теперь проверяется живыми тестами в
    конце файла, а фейк отвечает только за счётчики отчёта и порядок вызовов.
    """

    def __init__(self, seed: Optional[Dict[str, Dict[str, Any]]] = None,
                 conflict_on_mark: bool = False):
        self.rows: Dict[str, Dict[str, Any]] = dict(seed or {})
        self.events: List[str] = []
        self.conflict_on_mark = conflict_on_mark

    def find_action_by_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        return self.rows.get(idempotency_key)

    def insert_action(self, row: Dict[str, Any]) -> str:
        self.events.append(f"insert:{row['idempotency_key']}")
        self.rows[row["idempotency_key"]] = {**row, "status": "planned"}
        return row["idempotency_key"]  # action_id — в тестах достаточно ключа

    def mark_action(self, action_id: str, status: str, response: Dict[str, Any]) -> bool:
        self.events.append(f"mark:{action_id}:{status}")
        if self.conflict_on_mark:
            # Строку забрал другой контур (сторож перевёл в 'stale' и откатил):
            # реальный запрос с гардом её не тронет и вернёт False.
            return False
        self.rows[action_id]["status"] = status
        self.rows[action_id]["response"] = response
        return True

    def mark_unknown_outcome(self, action_id: str, reason: str) -> bool:
        self.events.append(f"unknown:{action_id}")
        if self.conflict_on_mark:
            return False
        self.rows[action_id]["status"] = "stale"
        self.rows[action_id]["response"] = {"unknown_outcome": True, "error": reason}
        return True


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
                       "dry_run": 0, "unknown_outcome": 0, "conflicted": 0,
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
                       "dry_run": 0, "unknown_outcome": 0, "conflicted": 0,
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


@pytest.mark.parametrize("status", writer_db.FINAL_STATUSES)
def test_apply_actions_skips_action_in_any_final_status(status):
    # «Кого журнал не переписывает» и «кого применение не отправляет второй
    # раз» — один список (db.FINAL_STATUSES). Разъехавшись, они дают ровно тот
    # дефект, ради которого список и заведён: повторная планировка затирает
    # previous_state строки, отправку которой применение пропускает, — и
    # единственное основание для отката теряется. Отдельный случай — 'stale':
    # запрос по такой строке уже ушёл в кабинет, и повторный bidmodifier.add
    # создал бы там второй объект.
    db = _FakeDB(seed={"k1": {"status": status}})
    client = _FakeClient(response={"SetResults": [{"Id": 7}]})

    report = apply_actions(client, [_action()], db)

    assert report["skipped"] == 1, status
    assert client.calls == [], status


def test_apply_actions_unknown_method_marks_failed_not_applied(monkeypatch):
    # Сквозной вариант предыдущего теста: если to_api_call когда-нибудь вернёт
    # метод без записи в _RESULT_COLLECTION (новый вид операции), apply_actions
    # обязан пометить действие 'failed' (отказ, переприменяется на следующем
    # прогоне) — а не 'applied', как было бы при старом None-по-умолчанию.
    import sync.agent.writer.apply as apply_module
    monkeypatch.setattr(apply_module, "to_api_call",
                         lambda action: ("bidmodifiers", "delete", {}))

    db = _FakeDB()
    client = _FakeClient(response={"DeleteResults": [{"Id": 7}]})

    report = apply_actions(client, [_action()], db)

    assert report["applied"] == 0
    assert report["rejected"] == 0
    assert report["failed"] == 1
    assert db.rows["k1"]["status"] == "failed"


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


# ------------------------------------- репетиция считается, а не показывает нули
# Дефект 6: в режиме без записи каждое действие получает статус dry_run, но
# счётчики его не считали — главный артефакт, по которому принимается решение
# включать боевую запись, показывал ровные нули по всем счётчикам.


def test_apply_actions_counts_dry_run_actions():
    db = _FakeDB()
    client = _FakeClient(response={"dry_run": True})

    report = apply_actions(client, [_action("k1"), _action("k2")], db)

    assert report["dry_run"] == 2
    assert report["applied"] == 0
    assert report["failed"] == 0


def test_apply_actions_dry_run_counter_present_even_when_zero():
    db = _FakeDB()
    client = _FakeClient(response={"SetResults": [{"Id": 7}]})

    report = apply_actions(client, [_action()], db)

    assert report["dry_run"] == 0


# =========================================================================
# Дефект C: неизвестный исход трактовался как «не применилось»
# =========================================================================
#
# Механизм зависших строк закрывал ровно одну дыру — смерть процесса после
# отправки. Но под статусом 'failed' пряталось то же самое: недоступность
# сервиса после ретраев, таймаут на записи, разрыв соединения после отправки
# тела. Во всех трёх случаях изменение может быть живым в кабинете, а строка
# не наблюдается сторожем, риск по ней не списан, и diff следующего прогона её
# не предложит — фактическое состояние уже совпало с планом.


def test_timeout_on_write_goes_to_stale_not_failed():
    db = _FakeDB()
    client = _FakeClient(raises=requests.exceptions.ReadTimeout("нет ответа"))

    report = apply_actions(client, [_action()], db)

    assert report["unknown_outcome"] == 1
    assert report["failed"] == 0
    # Статус — тот же, что у обрыва процесса: живое непроверенное изменение.
    assert db.rows["k1"]["status"] == "stale"
    assert "unknown:k1" in db.events


def test_service_unavailable_after_retries_goes_to_stale():
    # 5xx после ретраев: запрос уходил в Директ, и не один раз.
    db = _FakeDB()
    client = _FakeClient(raises=DirectWriteError(
        "bidmodifiers", 503, "сервис недоступен после ретраев", outcome_unknown=True))

    report = apply_actions(client, [_action()], db)

    assert report["unknown_outcome"] == 1
    assert db.rows["k1"]["status"] == "stale"


def test_connection_error_after_body_sent_goes_to_stale():
    db = _FakeDB()
    client = _FakeClient(raises=requests.exceptions.ConnectionError("сброс соединения"))

    report = apply_actions(client, [_action()], db)

    assert report["unknown_outcome"] == 1
    assert db.rows["k1"]["status"] == "stale"


def test_request_that_never_left_stays_failed():
    # Соединение не установилось — тело не отправлялось, изменения нет.
    # Такое действие обязано переприменяться, а не занимать наблюдение и
    # риск-бюджет фантомом.
    db = _FakeDB()
    client = _FakeClient(raises=requests.exceptions.ConnectTimeout("не достучались"))

    report = apply_actions(client, [_action()], db)

    assert report["failed"] == 1
    assert report["unknown_outcome"] == 0
    assert db.rows["k1"]["status"] == "failed"


def test_direct_rejection_at_request_level_stays_failed():
    # Директ явно ответил отказом уровня запроса — исход известен.
    db = _FakeDB()
    client = _FakeClient(raises=DirectWriteError("bidmodifiers", 54, "нет прав"))

    report = apply_actions(client, [_action()], db)

    assert report["failed"] == 1
    assert report["unknown_outcome"] == 0


def test_is_outcome_unknown_separates_the_two_families():
    assert is_outcome_unknown(requests.exceptions.ReadTimeout()) is True
    assert is_outcome_unknown(requests.exceptions.ConnectionError()) is True
    assert is_outcome_unknown(requests.exceptions.ChunkedEncodingError()) is True
    # ConnectTimeout наследует и ConnectionError, и Timeout — но означает
    # обратное: соединение не установилось, тело не отправлялось.
    assert is_outcome_unknown(requests.exceptions.ConnectTimeout()) is False
    assert is_outcome_unknown(ValueError("неизвестный тип корректировки")) is False
    assert is_outcome_unknown(DirectWriteError("s", 54, "нет прав")) is False
    assert is_outcome_unknown(
        DirectWriteError("s", 503, "недоступен", outcome_unknown=True)) is True


def test_unknown_outcome_is_not_counted_as_applied():
    # Отдельный счётчик: неизвестный исход не «применено» и не «не удалось».
    db = _FakeDB()
    client = _FakeClient(raises=requests.exceptions.ReadTimeout())

    report = apply_actions(client, [_action()], db)

    assert report["applied"] == 0
    assert report["rejected"] == 0
    assert report["failed"] == 0


# ---------------------------- гонка с откатом: отметка не затирает чужой исход


def test_mark_conflict_does_not_count_as_applied():
    # Строку между проверкой и отметкой забрал сторож (перевёл в 'stale' и
    # откатил). Запрос с гардом её не тронет — вызывающий код обязан это
    # увидеть, а не считать действие применённым.
    db = _FakeDB(conflict_on_mark=True)
    client = _FakeClient(response={"SetResults": [{"Id": 7}]})

    report = apply_actions(client, [_action()], db)

    assert report["applied"] == 0
    assert report["conflicted"] == 1
    assert report["details"][0]["result"] == "conflicted"
    assert report["details"][0]["attempted_status"] == "applied"


def test_mark_conflict_on_unknown_outcome_is_visible_too():
    db = _FakeDB(conflict_on_mark=True)
    client = _FakeClient(raises=requests.exceptions.ReadTimeout())

    report = apply_actions(client, [_action()], db)

    assert report["unknown_outcome"] == 0
    assert report["conflicted"] == 1
    assert report["details"][0]["attempted_status"] == "stale"


# ------------------- повтор непринятого действия несёт свежее прошлое состояние
# Дефект 5, поведенческая половина. Эти две проверки раньше стояли на фейковой
# БД, которая ВОСПРОИЗВОДИЛА контракт ON CONFLICT руками, — то есть проверяли
# сами себя и зеленели на сломанном коде. Теперь они идут через реальный модуль
# работы с базой, под тем же гейтом доступа, что и остальные живые тесты
# (tests/test_agent_writer_db.py).


def _live_action(key: str, account: str, percent: int) -> Dict[str, Any]:
    return {**_action(key), "account": account,
            "previous_state": {"Id": 7, "percent": percent}}


def _live_row(key: str):
    rows = writer_db._fetch(
        "SELECT * FROM edu_agent_actions WHERE idempotency_key = %s", (key,))
    return rows[0] if rows else None


def _live_cleanup(key: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM edu_agent_actions WHERE idempotency_key = %s", (key,))
        conn.commit()


@live_db
def test_live_replanned_action_overwrites_previous_state_of_failed_attempt():
    # Первая попытка упала на отправке; человек поправил корректировку руками;
    # вторая попытка несёт свежий факт. Журнал обязан хранить именно его —
    # иначе откат вернёт кабинет не туда, откуда агент его вывел.
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    key = "test-apply-replan-" + suffix
    account = "test-" + suffix
    try:
        apply_actions(_FakeClient(raises=RuntimeError("сеть недоступна")),
                      [_live_action(key, account, 10)], writer_db)
        assert _live_row(key)["status"] == "failed"

        apply_actions(_FakeClient(response={"SetResults": [{"Id": 7}]}),
                      [_live_action(key, account, 25)], writer_db)

        row = _live_row(key)
        assert row["previous_state"] == {"Id": 7, "percent": 25}
        assert row["status"] == "applied"
    finally:
        _live_cleanup(key)


@live_db
def test_live_applied_action_is_not_replanned():
    # Применённое действие не отправляется второй раз и не переписывается:
    # его previous_state — единственное основание для отката.
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    key = "test-apply-skip-" + suffix
    account = "test-" + suffix
    try:
        apply_actions(_FakeClient(response={"SetResults": [{"Id": 7}]}),
                      [_live_action(key, account, 10)], writer_db)

        client = _FakeClient(response={"SetResults": [{"Id": 7}]})
        report = apply_actions(client, [_live_action(key, account, 99)], writer_db)

        assert report["skipped"] == 1
        assert client.calls == []  # запрос не ушёл второй раз
        assert _live_row(key)["previous_state"] == {"Id": 7, "percent": 10}
    finally:
        _live_cleanup(key)


@live_db
def test_live_stale_action_is_not_sent_again():
    # Зависшая строка ('stale') закрыта для повторной отправки наравне с
    # применённой: запрос по ней уже ушёл в кабинет, и второй bidmodifiers.add
    # создал бы там второй объект.
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    key = "test-apply-stale-" + suffix
    account = "test-" + suffix
    try:
        apply_actions(_FakeClient(raises=RuntimeError("обрыв")),
                      [_live_action(key, account, 10)], writer_db)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE edu_agent_actions SET status = 'planned', "
                    "created_at = now() - make_interval(mins => 90) "
                    "WHERE idempotency_key = %s", (key,))
            conn.commit()
        assert [r["idempotency_key"] for r in
                writer_db.mark_stale_planned(60, account=account)] == [key]

        client = _FakeClient(response={"SetResults": [{"Id": 7}]})
        report = apply_actions(client, [_live_action(key, account, 99)], writer_db)

        assert report["skipped"] == 1
        assert client.calls == []
        assert _live_row(key)["previous_state"] == {"Id": 7, "percent": 10}
    finally:
        _live_cleanup(key)
