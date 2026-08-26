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

import sync.agent.writer.apply as apply_mod
import sync.agent.writer.db as writer_db
from sync.agent.writer.apply import _element_errors, apply_actions, to_api_call
from sync.agent.writer.client import DirectWriteError, is_outcome_unknown
import sync.agent.writer.client as client_module
from sync.agent import rejects
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

    def mark_sent(self, action_id: str) -> None:
        self.events.append(f"sent:{action_id}")
        self.rows[action_id]["sent_at"] = "now"

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
    """Транспорт-протокол: считает ЗАПРОСЫ, а не действия.

    mutate_batch повторяет форму настоящего клиента и сводится к одному
    mutate — иначе счётчик calls считал бы элементы и батч был бы неотличим
    от запроса за элементом ровно в тех тестах, ради которых он заведён.
    """

    def __init__(self, response: Optional[Dict[str, Any]] = None,
                 raises: Optional[Exception] = None,
                 respond=None):
        self._response = response if response is not None else {}
        self._raises = raises
        self._respond = respond          # callable(params) → ответ на запрос
        self.calls: List[tuple] = []
        self.units_left: Optional[int] = None
        self.units_limit: Optional[int] = None
        self.write_allowed = True

    def is_write_allowed(self) -> bool:
        return self.write_allowed

    def mutate(self, service: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((service, method, params))
        if self._raises is not None:
            raise self._raises
        if self._respond is not None:
            return self._respond(params)
        return self._response

    def mutate_batch(self, service: str, method: str, collection: str,
                     items: List[Dict[str, Any]]) -> Dict[str, Any]:
        assert items, "пустой батч до транспорта доходить не должен"
        assert len(items) <= client_module.BATCH_LIMIT, "перебор элементов в запросе"
        return self.mutate(service, method, {collection: list(items)})


def test_apply_actions_marks_applied_on_clean_success():
    db = _FakeDB()
    client = _FakeClient(response={"SetResults": [{"Id": 7}]})

    report = apply_actions(client, [_action()], db)

    assert report == {"applied": 1, "skipped": 0, "failed": 0, "rejected": 0,
                       "dry_run": 0, "unknown_outcome": 0, "conflicted": 0,
                       "deferred": 0, "units_low": 0, "rejects": [],
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
                       "deferred": 0, "units_low": 0, "rejects": [],
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


def test_apply_actions_unknown_method_is_unknown_outcome_not_failure(monkeypatch):
    # Сквозной вариант предыдущего теста: если to_api_call когда-нибудь вернёт
    # метод без записи в _RESULT_COLLECTION (новый вид операции), разбор
    # ответа падает — но падает ПОСЛЕ отправки. Запрос состоялся, изменение
    # может быть живым в кабинете, и 'failed' здесь означал бы переприменение
    # уже совершённого добавления, то есть ВТОРОЙ объект в кабинете.
    # 'applied' тоже нельзя: ответ не разобран, элемент мог быть отклонён.
    import sync.agent.writer.apply as apply_module
    monkeypatch.setattr(apply_module, "to_api_call",
                         lambda action: ("bidmodifiers", "delete", {}))

    db = _FakeDB()
    client = _FakeClient(response={"DeleteResults": [{"Id": 7}]})

    report = apply_actions(client, [_action()], db)

    assert report["applied"] == 0
    assert report["rejected"] == 0
    assert report["failed"] == 0
    assert report["unknown_outcome"] == 1
    assert db.rows["k1"]["status"] == "stale"


def test_apply_actions_failure_before_send_is_still_a_failure(monkeypatch):
    # Обратная половина: граница проходит по ФАКТУ ОТПРАВКИ, а не по месту в
    # try. Ошибка сборки тела запроса случается ДО mutate — запрос точно не
    # ушёл, в кабинете ничего нет, и такое действие обязано переприменяться,
    # а не занимать наблюдение и риск-бюджет как живое изменение.
    import sync.agent.writer.apply as apply_module

    def _boom(action):
        raise ValueError("демографический ключ вне перечня API Директа")

    monkeypatch.setattr(apply_module, "to_api_call", _boom)

    db = _FakeDB()
    client = _FakeClient(response={"AddResults": [{"Id": 7}]})

    report = apply_actions(client, [_action()], db)

    assert client.calls == []
    assert report["failed"] == 1
    assert report["unknown_outcome"] == 0
    assert db.rows["k1"]["status"] == "failed"


def test_apply_actions_rehearsal_parse_failure_is_not_a_live_change(monkeypatch):
    # Репетиция отправкой не считается: mutate вернул пометку, не сходив в
    # сеть. Пометь мы её как отправку — исключение при разборе увело бы
    # строку репетиции в 'stale', то есть в живые непроверенные изменения
    # БОЕВОГО журнала, за которыми ничего не стоит.
    import sync.agent.writer.apply as apply_module

    calls = {"n": 0}

    def _explode(method, response):
        calls["n"] += 1
        raise RuntimeError("разбор ответа сломан")

    monkeypatch.setattr(apply_module, "_element_errors", _explode)

    db = _FakeDB()
    client = _FakeClient(response={"dry_run": True})

    report = apply_actions(client, [_action()], db)

    assert calls["n"] == 0, "в репетиции ответ вообще не разбирается"
    assert report["dry_run"] == 1
    assert report["unknown_outcome"] == 0
    assert db.rows["k1"]["status"] == "dry_run"


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


# =========================================================================
# Дефект 5: аренда прогона перепроверяется перед КАЖДЫМ изменяющим запросом
# =========================================================================
#
# Аренду берут на час, а прогон по сотням кампаний живёт дольше. Протухшая на
# ходу аренда пускает второй прогон, и оба шлют bidmodifiers.add по одной
# кампании — второй объект в кабинете, Id первого не знает никто. Поэтому
# владение подтверждается прямо перед отправкой, а не только на старте.


class _FakeLease:
    def __init__(self, lost_after=None):
        self.guards = 0
        self.lost_after = lost_after

    def guard(self):
        self.guards += 1
        if self.lost_after is not None and self.guards > self.lost_after:
            raise writer_db.RunLeaseLost("аренда потеряна")


def test_apply_actions_checks_the_lease_before_every_send():
    db = _FakeDB()
    client = _FakeClient(response={"SetResults": [{"Id": 7}]})
    lease = _FakeLease()

    apply_actions(client, [_action("k1"), _action("k2")], db, lease=lease)

    assert lease.guards == 2
    assert len(client.calls) == 2


def test_lost_lease_stops_the_run_instead_of_writing_further():
    # Потеря аренды — не ошибка одного действия: в кабинет уже может писать
    # второй прогон. Исключение обязано выйти наружу, а не превратиться в
    # 'failed' у одного действия и продолжение у остальных.
    db = _FakeDB()
    client = _FakeClient(response={"SetResults": [{"Id": 7}]})
    lease = _FakeLease(lost_after=1)

    with pytest.raises(writer_db.RunLeaseLost):
        apply_actions(client, [_action("k1"), _action("k2")], db, lease=lease)

    assert len(client.calls) == 1, "после потери аренды запросы не уходят"
    assert "insert:k2" not in db.events


def test_lease_is_checked_before_the_journal_row_is_written():
    order = []

    class _OrderedDB(_FakeDB):
        def insert_action(self, row):
            order.append("insert")
            return super().insert_action(row)

    class _OrderedLease(_FakeLease):
        def guard(self):
            order.append("guard")
            super().guard()

    apply_actions(_FakeClient(response={"SetResults": [{"Id": 7}]}),
                  [_action()], _OrderedDB(), lease=_OrderedLease())

    assert order == ["guard", "insert"]


def test_apply_actions_works_without_a_lease():
    # Вызывающий код без аренды (разовый разбор, тесты) остаётся рабочим.
    report = apply_actions(_FakeClient(response={"SetResults": [{"Id": 7}]}),
                           [_action()], _FakeDB())
    assert report["applied"] == 1


# ============================ докстринг применения сходится с машиной статусов


def test_apply_docstring_lists_every_status_of_the_machine():
    # Докстринг обещал «один из четырёх», перечислял пять, а в машине журнала
    # их семь. Отчёт по нему читают глазами перед включением боевой записи —
    # расхождение здесь дороже, чем кажется.
    doc = apply_actions.__doc__
    machine = {"planned", "dry_run", "applied", "rejected", "failed", "stale",
               "rolled_back"}
    for status in machine:
        assert "'%s'" % status in doc, status
    assert "семь" in doc
    assert "один из четырёх" not in doc


# =========================================================================
# Дефект И2 (вторая сторона): выбор между полом и возрастом по «иначе»
# =========================================================================


def test_age_key_goes_to_the_age_field_by_list_not_by_elimination():
    action = {"action_kind": "bidmodifier.add",
              "payload": {"CampaignId": 111, "Type": "DEMOGRAPHICS_ADJUSTMENT",
                          "key": "AGE_25_34", "BidModifier": 20}}
    adjustment = to_api_call(action)[2]["BidModifiers"][0]["DemographicsAdjustments"][0]

    assert adjustment["Age"] == "AGE_25_34"
    assert "Gender" not in adjustment


def test_unknown_demographic_key_never_reaches_the_request():
    # Прежде выбор делался по принципу «если ключ не пол, значит возраст», и
    # любое значение, дошедшее сюда, молча становилось Age. Первый и самый
    # частый постоялец — «не определено» (UNKNOWN) из отчётов Директа.
    action = {"action_kind": "bidmodifier.add",
              "payload": {"CampaignId": 111, "Type": "DEMOGRAPHICS_ADJUSTMENT",
                          "key": "UNKNOWN", "BidModifier": 20}}

    with pytest.raises(ValueError) as exc:
        to_api_call(action)
    assert "UNKNOWN" in str(exc.value)


def test_demographic_field_map_matches_the_planning_lists():
    # Один справочник на планирование и на сборку запроса: расхождение между
    # «какие ключи мы разрешаем» и «как мы их раскладываем по полям» и есть
    # тот класс ошибки, ради которого справочник заводится.
    from sync.agent.writer.plan import AGE_KEYS, DEMOGRAPHIC_FIELD, GENDER_KEYS

    assert {k for k, v in DEMOGRAPHIC_FIELD.items() if v == "Gender"} == set(GENDER_KEYS)
    assert {k for k, v in DEMOGRAPHIC_FIELD.items() if v == "Age"} == set(AGE_KEYS)


# --------------- расписание: другой сервис, другой метод, другая коллекция

def test_schedule_goes_through_campaigns_update():
    """Расписание применяется через кампанию, а не через корректировки.

    У Директа нет способа поменять один час: Schedule принимается целиком,
    и живёт он внутри campaigns.update. Отправь мы его в bidmodifiers —
    получили бы отказ на каждом прогоне.
    """
    service, method, params = to_api_call({
        "action_kind": "schedule.set",
        "payload": {"CampaignId": "114057545",
                    "TimeTargeting": {"Schedule": {"Items": ["1,100"]}}},
    })

    assert (service, method) == ("campaigns", "update")
    assert params["Campaigns"][0]["Id"] == 114057545
    assert params["Campaigns"][0]["TimeTargeting"] == {"Schedule": {"Items": ["1,100"]}}


def test_update_results_collection_is_known():
    """У campaigns.update своя коллекция результатов — UpdateResults.

    Не знай её разбор, отказ уровня элемента прочитался бы как успех:
    коллекция не нашлась бы, а «ошибок нет» означает «принято».
    """
    errors = _element_errors("update", {"UpdateResults": [
        {"Errors": [{"Code": 6000, "Details": "нельзя"}]}]})

    assert errors and errors[0]["Code"] == 6000


def test_update_success_is_read_as_success():
    # Ошибок нет — None, а не пустой список: вызывающий код различает
    # «разобрали, чисто» и «разбирать было нечего».
    assert _element_errors("update", {"UpdateResults": [{"Id": 1}]}) is None


def test_schedule_payload_keeps_neighbour_fields():
    # Праздничный режим и учёт рабочих выходных настроены человеком: тело
    # уходит ровно таким, каким его собрал план, без досборки здесь.
    targeting = {"Schedule": {"Items": ["1,100"]},
                 "HolidaysSchedule": {"SuspendOnHolidays": "YES"},
                 "ConsiderWorkingWeekends": "YES"}
    _, _, params = to_api_call({
        "action_kind": "schedule.set",
        "payload": {"CampaignId": 1, "TimeTargeting": targeting},
    })

    assert params["Campaigns"][0]["TimeTargeting"] == targeting


# ------------------------------- квоты API: deferred и порог баллов


def test_quota_error_defers_without_burning_attempt():
    # Код 152 «Недостаточно баллов» — ошибка уровня запроса: Директ отклонил
    # вызов, НЕ применив его. Это не провал действия ('failed' жёг попытку и
    # вёл к потолку apply_attempts), а временное состояние кабинета: статус
    # 'deferred', следующий прогон переприменит без счёта попыток.
    db = _FakeDB()
    client = _FakeClient(raises=DirectWriteError(
        "bidmodifiers", 152, "Недостаточно баллов"))

    report = apply_actions(client, [_action()], db)

    assert report["deferred"] == 1
    assert report["failed"] == 0
    assert db.rows["k1"]["status"] == "deferred"
    assert report["details"][0]["result"] == "deferred"


def test_units_low_stops_before_journal():
    # Баллов почти нет (остаток < 5 % суточного лимита) — отправлять нечем.
    # Прогон останавливается ДО записи в журнал: строка 'planned' без
    # отправки — мусор, который потом закрывал бы сторож.
    db = _FakeDB()
    client = _FakeClient(response={"SetResults": [{"Id": 7}]})
    client.units_left = 100
    client.units_limit = 10_000

    report = apply_actions(client, [_action()], db)

    assert report["units_low"] == 1
    assert db.events == []
    assert client.calls == []


def test_units_low_lands_in_rejects_with_a_reason():
    # Убрать этот тест — и хвост такта снова исчезнет без следа: баллы
    # кончились, действия не отправлены, в edu_agent_rejects пусто, а в отчёте
    # одно число units_low, которое живёт до конца лога. При лимите в 50
    # действий порог не срабатывал ни разу; при трёхстах он срабатывает первым
    # же тактом, и «почему агент сегодня ничего не сделал» станет неотвечаемым.
    db = _FakeDB()
    client = _FakeClient(response={"SetResults": [{"Id": 7}]})
    client.units_left = 1
    client.units_limit = 1000

    report = apply_actions(client, [_action("k1"), _action("k2")], db)

    assert report["rejects"], "хвост такта исчез без причины"
    assert all(r["reason"] == rejects.UNITS_LOW for r in report["rejects"])
    assert [r["key"] for r in report["rejects"]] == ["", ""]
    # Цена действия едет со строкой: отказ без денег на кону не отличить от
    # отказа по копеечному объекту.
    assert report["rejects"][0]["risk_rub"] == 100.0
    assert report["rejects"][0]["account"] == "test-login"
    assert report["rejects"][0]["stage"] == "e1"
    assert report["units_low"] == 2


def test_units_healthy_does_not_stop():
    db = _FakeDB()
    client = _FakeClient(response={"SetResults": [{"Id": 7}]})
    client.units_left = 9_000
    client.units_limit = 10_000

    report = apply_actions(client, [_action()], db)

    assert report["applied"] == 1
    assert report["units_low"] == 0


def test_sent_at_marked_between_insert_and_mutate():
    # Отметка «запрос отправляется» ложится в журнал ДО mutate: обрыв
    # процесса между insert и отправкой оставляет строку БЕЗ sent_at, и
    # закрывать её надо как никогда-не-отправленную, а не как зависшую.
    db = _FakeDB()
    client = _FakeClient(response={"SetResults": [{"Id": 7}]})

    apply_actions(client, [_action()], db)

    assert db.events[0] == "insert:k1"
    assert db.events[1] == "sent:k1"
    assert db.events[2].startswith("mark:k1")


def test_rehearsal_does_not_mark_sent():
    # dry-run никуда не ходит — sent_at репетиции лгал бы, что запрос уходил.
    db = _FakeDB()
    client = _FakeClient(response={"dry_run": True})
    client.write_allowed = False

    apply_actions(client, [_action()], db)

    assert not any(e.startswith("sent:") for e in db.events)


# ------------------------------- батчи: много действий одним запросом


def _bidmodifier_actions(count: int, campaign_start: int = 100
                         ) -> List[Dict[str, Any]]:
    """count действий bidmodifier.add, каждое на СВОЮ кампанию."""
    return [{
        "action_kind": "bidmodifier.add",
        "object_level": "campaign",
        "object_id": str(campaign_start + i),
        "direct_type": "MOBILE_ADJUSTMENT",
        "key": "MOBILE",
        "payload": {"CampaignId": campaign_start + i, "Type": "MOBILE_ADJUSTMENT",
                    "key": "MOBILE", "BidModifier": 30},
        "previous_state": {},
        "idempotency_key": f"k{i}",
        "account": "test-login",
        "risk_rub": 10.0,
        "red_line": {},
    } for i in range(count)]


def _add_results(params: Dict[str, Any], errors_at: Optional[int] = None,
                 short_by: int = 0) -> Dict[str, Any]:
    """Ответ Директа на батч: результат на КАЖДЫЙ отправленный элемент."""
    items = params["BidModifiers"]
    results: List[Dict[str, Any]] = []
    for index, _ in enumerate(items):
        if index == errors_at:
            results.append({"Errors": [{"Code": 8800, "Message": "не найдена"}]})
        else:
            results.append({"Id": 1000 + index})
    return {"AddResults": results[:len(results) - short_by]}


def test_same_kind_actions_are_sent_in_one_request():
    # Уберите этот тест — и запись вернётся к запросу за действием. При
    # трёхстах действиях это триста обращений к API в окне между кроном Э1 и
    # сверкой: сверка начнёт читать кабинет посреди записи и объявит
    # расхождением то, что дописывается прямо сейчас.
    db = _FakeDB()
    client = _FakeClient(respond=_add_results)

    report = apply_actions(client, _bidmodifier_actions(10), db)

    assert len(client.calls) == 1
    assert len(client.calls[0][2]["BidModifiers"]) == 10
    assert report["applied"] == 10


def test_batch_is_cut_by_the_transport_limit():
    # Размер пачки — свойство транспорта (client.BATCH_LIMIT), а не
    # усмотрение применения: перебор Директ отвергает запрос целиком.
    db = _FakeDB()
    client = _FakeClient(respond=_add_results)

    apply_actions(client, _bidmodifier_actions(client_module.BATCH_LIMIT + 2), db)

    assert [len(call[2]["BidModifiers"]) for call in client.calls] == [
        client_module.BATCH_LIMIT, 2]


def test_two_actions_on_one_object_never_share_a_request():
    # Сегодня каждый элемент едет своим запросом, и соседство двух элементов
    # про ОДИН объект в одном теле нигде не проверено: у campaigns.update это
    # прямо означает, что второй элемент затрёт первый. Батч меняет число
    # запросов, а не соседство — три действия по одной кампании остаются
    # тремя запросами.
    db = _FakeDB()
    client = _FakeClient(respond=_add_results)
    actions = [{**a, "object_id": "111", "payload": {**a["payload"], "CampaignId": 111}}
               for a in _bidmodifier_actions(3)]

    report = apply_actions(client, actions, db)

    assert len(client.calls) == 3
    assert report["applied"] == 3


def test_different_kinds_do_not_share_a_request():
    # bidmodifiers.add и campaigns.update — разные сервисы и разные тела.
    db = _FakeDB()
    client = _FakeClient(respond=lambda params: (
        _add_results(params) if "BidModifiers" in params
        else {"UpdateResults": [{"Id": 1}]}))
    actions = _bidmodifier_actions(2) + [{
        **_action("budget"), "action_kind": "budget.set_daily",
        "object_id": "900",
        "payload": {"CampaignId": 900, "DailyBudget": {"Amount": 1_000_000}},
    }]

    report = apply_actions(client, actions, db)

    assert [call[0] for call in client.calls] == ["bidmodifiers", "campaigns"]
    assert report["applied"] == 3


def test_element_error_in_a_batch_does_not_fail_its_neighbours():
    # То же различение, что держит client._call: ошибка ЗАПРОСА и ошибка
    # ЭЛЕМЕНТА — разные события. Не разбери мы батч поэлементно, один
    # отклонённый элемент отменил бы девять применённых соседей (или, что
    # хуже, девять отклонённых уехали бы в журнал как applied).
    db = _FakeDB()
    client = _FakeClient(respond=lambda params: _add_results(params, errors_at=3))

    report = apply_actions(client, _bidmodifier_actions(10), db)

    assert report["applied"] == 9
    assert report["rejected"] == 1
    assert db.rows["k3"]["status"] == "rejected"
    assert db.rows["k4"]["status"] == "applied"


def test_batch_row_keeps_its_own_result_not_a_neighbours_id():
    # Откат bidmodifier.add ищет Id в response.AddResults[0].Id
    # (writer/rollback._added_modifier_id). Положи мы в каждую строку батча
    # ОБЩИЙ ответ — все десять строк вернули бы Id первого элемента, и сторож
    # откатывал бы чужую корректировку, а свою оставлял жить.
    db = _FakeDB()
    client = _FakeClient(respond=_add_results)

    apply_actions(client, _bidmodifier_actions(3), db)

    assert db.rows["k0"]["response"]["AddResults"][0]["Id"] == 1000
    assert db.rows["k1"]["response"]["AddResults"][0]["Id"] == 1001
    assert db.rows["k2"]["response"]["AddResults"][0]["Id"] == 1002


def test_journal_stays_one_row_per_action_even_in_batches():
    # Батч — свойство транспорта, а не журнала: одна строка на батч сделала
    # бы неоткатываемым каждое действие внутри него (откат идёт по строке).
    db = _FakeDB()
    client = _FakeClient(respond=_add_results)

    apply_actions(client, _bidmodifier_actions(40), db)

    assert sum(1 for e in db.events if e.startswith("insert:")) == 40
    assert sum(1 for e in db.events if e.startswith("mark:")) == 40
    assert sum(1 for e in db.events if e.startswith("sent:")) == 40


def test_request_level_error_fails_every_element_of_the_batch():
    # Ошибка уровня ЗАПРОСА означает, что не применилось НИЧЕГО: все элементы
    # обязаны получить свой исход, а не потеряться вместе с запросом.
    db = _FakeDB()
    client = _FakeClient(raises=DirectWriteError("bidmodifiers", 8000,
                                                 "Некорректный запрос"))

    report = apply_actions(client, _bidmodifier_actions(5), db)

    assert report["failed"] == 5
    assert all(db.rows[f"k{i}"]["status"] == "failed" for i in range(5))


def test_response_shorter_than_the_batch_is_unknown_not_applied():
    # Ответ описывает не все отправленные элементы. Чем ЗАКОНЧИЛИСЬ
    # неописанные — неизвестно, и пометить их applied значит соврать журналу
    # о живых изменениях: 'stale' ставит их под сторожа и риск-бюджет.
    db = _FakeDB()
    client = _FakeClient(respond=lambda params: _add_results(params, short_by=2))

    report = apply_actions(client, _bidmodifier_actions(5), db)

    assert report["unknown_outcome"] == 5
    assert report["applied"] == 0
    assert all(db.rows[f"k{i}"]["status"] == "stale" for i in range(5))


def test_rehearsal_batch_reports_every_action():
    # В репетиции ответ один на батч. Разложи мы его на строки неаккуратно —
    # отчёт репетиции показал бы одно действие вместо десяти, а именно по
    # нему принимается решение включать боевую запись.
    db = _FakeDB()
    client = _FakeClient(response={"dry_run": True})
    client.write_allowed = False

    report = apply_actions(client, _bidmodifier_actions(10), db)

    assert report["dry_run"] == 10
    assert len(client.calls) == 1


def test_run_fits_the_cron_window():
    """Уберите этот тест — и прогон снова сможет перерасти своё окно.

    Окно считается по фактам расписания (docs/AGENT-BETA-MASTER-PLAN.md,
    строки 339–341): крон Э1 08:00 UTC, сверка 08:30 UTC, задержка
    диспетчера GitHub 38–50 мин. Задержки такту и сверке выпадают
    независимо, поэтому ГАРАНТИРОВАННЫЙ зазор — не 30 минут, а
    30 − (50 − 38) = 18 минут: Э1 может стартовать в 08:50, а сверка в 09:08.

    Тест не выдумывает время ответа Директа — измерить его здесь нечем. Он
    фиксирует то, чем мы управляем: число ЗАПРОСОВ. Из него следует бюджет
    на один запрос: при 30 запросах это 36 секунд, то есть запас к таймауту
    транспорта (120 с) есть в типичном случае и его нет в худшем — так что
    запросов должно быть десятки, а не сотни.
    """
    db = _FakeDB()
    client = _FakeClient(respond=_add_results)

    apply_actions(client, _bidmodifier_actions(300), db)

    guaranteed_window_s = 18 * 60
    budget_per_request_s = guaranteed_window_s / len(client.calls)
    assert len(client.calls) == 30
    assert budget_per_request_s >= 30, (
        f"на запрос остаётся {budget_per_request_s:.0f} с при таймауте "
        f"транспорта 120 с — прогон не укладывается в окно")
