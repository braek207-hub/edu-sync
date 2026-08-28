# -*- coding: utf-8 -*-
import os
import uuid
from datetime import datetime, timedelta

import pytest

import sync.agent.writer.db as writer_db
from sync.agent.writer import risk
from sync.agent.writer.db import WRITER_DDL, ensure_writer_tables, spent_risk
from sync.db import get_connection

REQUIRED = ["edu_agent_actions", "edu_agent_risk_budget"]

# Только test_spent_risk_counts_by_applied_week_not_created_week трогает БД —
# остальные тесты в файле чистые (строковые проверки WRITER_DDL) и без
# DATABASE_URL продолжают выполняться как обычно (конвенция tests/test_ml_db.py
# применяется тут точечно, decorator'ом на одном тесте, а не на весь модуль,
# чтобы не терять покрытие чистых тестов локально).


def test_required_tables_present():
    ddl = "\n".join(WRITER_DDL)
    for table in REQUIRED:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl, table


def test_ddl_has_no_destructive_statements():
    ddl = "\n".join(WRITER_DDL).upper()
    for forbidden in ("DROP TABLE", "TRUNCATE"):
        assert forbidden not in ddl


def test_actions_store_previous_state_for_rollback():
    # Нет сохранённого прошлого состояния — нет применения: откат обязан быть
    # возможен для каждого действия.
    ddl = "\n".join(WRITER_DDL)
    assert "previous_state" in ddl
    assert "red_line" in ddl


def test_actions_have_idempotency_key_unique():
    ddl = "\n".join(WRITER_DDL)
    assert "idempotency_key" in ddl
    assert "UNIQUE" in ddl.upper()


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL")
def test_spent_risk_counts_by_applied_week_not_created_week():
    # Риск-бюджет — деньги под ПРИМЕНЁННЫМИ изменениями. Действие, созданное
    # на прошлой неделе и применённое на текущей, обязано считаться в текущей
    # неделе; действие, созданное и применённое на прошлой, — не считаться.
    # Текстовая проверка исходника не ловит такую регрессию, поэтому здесь —
    # поведенческий тест с реальной БД (конвенция tests/test_ml_db.py).
    ensure_writer_tables()

    now = datetime.utcnow()
    last_week = now - timedelta(days=10)
    week_start = now - timedelta(days=1)
    suffix = uuid.uuid4().hex[:8]
    key_counted = f"test-spent-risk-counted-{suffix}"
    key_excluded = f"test-spent-risk-excluded-{suffix}"

    baseline = spent_risk(week_start.isoformat())
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO edu_agent_actions (
                        action_id, idempotency_key, created_at, applied_at,
                        account, object_level, object_id, action_kind,
                        risk_rub, status
                    ) VALUES (%s, %s, %s, %s, 'test', 'campaign', 'x1',
                              'set_field', 111.0, 'applied')
                    """,
                    (f"test-spent-risk-a-{suffix}", key_counted, last_week, now),
                )
                cur.execute(
                    """
                    INSERT INTO edu_agent_actions (
                        action_id, idempotency_key, created_at, applied_at,
                        account, object_level, object_id, action_kind,
                        risk_rub, status
                    ) VALUES (%s, %s, %s, %s, 'test', 'campaign', 'x2',
                              'set_field', 222.0, 'applied')
                    """,
                    (f"test-spent-risk-b-{suffix}", key_excluded, last_week, last_week),
                )
            conn.commit()

        spent = spent_risk(week_start.isoformat())
        # Только действие, применённое ПОСЛЕ week_start, попадает в сумму —
        # его created_at при этом лежит ДО week_start (проверяет именно
        # applied_at, а не created_at: на старом коде счёт был бы 0).
        assert spent - baseline == pytest.approx(111.0)
    finally:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM edu_agent_actions WHERE idempotency_key IN (%s, %s)",
                    (key_counted, key_excluded),
                )
            conn.commit()


# --------------------- повтор несёт свежие данные, а не прошлое первого прогона
# Дефект 5: ON CONFLICT DO NOTHING оставлял в журнале previous_state ПЕРВОГО
# прогона навсегда. Сценарий: первый прогон сохранил прошлое состояние и упал на
# отправке; человек поправил значение руками; второй прогон прочитал свежий факт,
# но ключ идемпотентности тот же — и откат вернул бы кабинет не туда, откуда
# агент его вывел. То же с оценкой риска и красной линией.


def test_insert_action_updates_unapplied_row_instead_of_ignoring_it():
    sql = " ".join(writer_db.INSERT_ACTION_SQL.split())
    assert "ON CONFLICT (idempotency_key) DO UPDATE SET" in sql
    assert "DO NOTHING" not in sql
    for column in ("previous_state = EXCLUDED.previous_state",
                   "red_line = EXCLUDED.red_line",
                   "risk_rub = EXCLUDED.risk_rub",
                   "payload = EXCLUDED.payload"):
        assert column in sql, column


def test_insert_action_never_touches_row_in_final_status():
    # Применённая, откатанная или зависшая строка неприкосновенна: её
    # previous_state описывает реально совершённое изменение и является
    # единственным основанием для отката.
    sql = " ".join(writer_db.INSERT_ACTION_SQL.split())
    assert "WHERE edu_agent_actions.status NOT IN (" in sql
    for status in writer_db.FINAL_STATUSES:
        assert "'%s'" % status in sql.rsplit("NOT IN", 1)[1], status


# Что применение действительно ПОЛЬЗУЕТСЯ этим списком, а не своим собственным,
# проверяется поведенчески — tests/test_agent_writer_apply.py::
# test_apply_actions_skips_action_in_any_final_status.


def test_insert_action_resets_stale_response_and_status():
    # Прежняя посылка «в SQL стоит литерал 'planned'» истекла: статус стал
    # параметром — тем же запросом заводится НАМЕРЕНИЕ полосы из тени
    # (SHADOW_STATUS). Проверяется то же самое свойство: повторная планировка
    # возвращает строку в исходное состояние, а не оставляет на ней ответ
    # прошлой попытки.
    sql = " ".join(writer_db.INSERT_ACTION_SQL.split())
    assert "status = %(status)s" in sql
    assert "response = '{}'::jsonb" in sql
    assert "created_at = now()" in sql


def _capture_execute(monkeypatch) -> dict:
    """Перехват запроса, который уходит в БД через get_connection."""
    captured: dict = {}

    class _Cursor:
        def execute(self, sql, params=None):
            captured.update(sql=sql, params=params)

        def fetchone(self):
            return ("act-1",)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Conn:
        def cursor(self, **kwargs):
            return _Cursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(writer_db, "get_connection", lambda: _Conn())
    return captured


def test_insert_action_writes_planned_unless_asked_otherwise(monkeypatch):
    # Умолчание — не мелочь оформления: заведение действия зовут из полудюжины
    # мест, и статус, забытый в одном из них, увёл бы настоящее изменение в
    # тень, где его никто не отправит и никто не откатит.
    seen = _capture_execute(monkeypatch)

    writer_db.insert_action(_journal_row("k-1", "acc-1"))
    assert seen["params"]["status"] == writer_db.PLANNED_STATUS

    writer_db.insert_action(_journal_row("k-2", "acc-1"),
                            status=writer_db.SHADOW_STATUS)
    assert seen["params"]["status"] == writer_db.SHADOW_STATUS


def test_the_shadow_status_is_in_none_of_the_status_tuples():
    # Утверждение, а не умолчание. Попади тень в финальные — выпуск полосы из
    # тени больше не переписал бы строку и действие не уехало бы никогда; в
    # живые — сторож начал бы откатывать изменение, которого в кабинете нет;
    # в платящие — риск-бюджет недели считал бы занятыми деньги, которыми
    # никто не рисковал.
    shadow = writer_db.SHADOW_STATUS
    assert shadow not in writer_db.FINAL_STATUSES
    assert shadow not in writer_db.LIVE_STATUSES
    assert shadow not in writer_db.RISK_CHARGED_STATUSES
    assert shadow not in writer_db.REATTEMPTED_STATUSES


def test_shadow_actions_asks_only_for_unjudged_intents():
    sql = " ".join(writer_db.SHADOW_ACTIONS_SQL.split())

    assert "status = %s" in sql
    # Вердикт выносится ОДИН раз: окно сверки с каждым днём шире, и повторный
    # проход тихо превращал бы «не сбылось» в «сбылось» накопленным дрейфом.
    assert "response->>'shadow_verdict' IS NULL" in sql


def test_shadow_outcome_never_touches_a_row_that_left_the_shadow():
    # Полосу выпустили из тени, и та же строка уехала в кабинет статусом
    # planned. Её обещание судит наблюдение, а не сверка намерений: запись
    # вердикта поверх была бы вердиктом о том, чего не происходило.
    sql = " ".join(writer_db.MARK_SHADOW_SQL.split())

    assert "status = %(shadow)s" in sql
    assert "response = response || %(response)s::jsonb" in sql


# ------------------------------------- обрыв ПОСЛЕ отправки виден в отчёте
# Дефект 7: порядок «журнал → отправка» соблюдён, но смерть процесса ПОСЛЕ
# ухода запроса оставляла строку в статусе planned — без ответа и без Id
# созданного объекта. Такую строку не видит ни сторож применённых действий,
# ни откат; риск не списан; diff следующего прогона новых действий не
# предложит, потому что факт в кабинете уже совпал с планом.


def test_stale_planned_finds_rows_stuck_in_intermediate_status():
    # Читающий дубль (stale_planned) удалён: он ничего не делал, а пометку и
    # так возвращает mark_stale_planned одним UPDATE ... RETURNING. Условие
    # поиска зависших строк живёт теперь в одном месте.
    sql = " ".join(writer_db.MARK_STALE_SQL.split())
    assert "status = 'planned'" in sql
    assert "created_at < now() - make_interval(mins => %s)" in sql


def test_stale_planned_passes_threshold_and_account(monkeypatch):
    # mark_stale_planned ходит в БД не через _fetch, а своим соединением
    # (UPDATE ... RETURNING обязан быть атомарным), поэтому перехватывается
    # курсор, а не хелпер чтения.
    captured = {}

    class _Cursor:
        def execute(self, sql, params=()):
            captured.update(sql=sql, params=params)

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Conn:
        def cursor(self, **kwargs):
            return _Cursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(writer_db, "get_connection", lambda: _Conn())

    writer_db.mark_stale_planned(60, account="acc-1")
    assert captured["params"] == (60, "acc-1", "acc-1")

    # Без кабинета — все зависшие строки, фильтр отключается значением NULL.
    writer_db.mark_stale_planned(30)
    assert captured["params"] == (30, None, None)


def _captured_sql(monkeypatch, call):
    """Текст запроса, который функция реально отправляет в БД.

    Проверять исходник функции нельзя: запрос собирается подстановкой, и
    статусы в тексте модуля не встречаются вовсе — зато встречаются в
    комментарии рядом, из-за чего проверка по исходнику зеленела бы на любом
    коде.
    """
    captured = {}
    monkeypatch.setattr(
        writer_db, "_fetch",
        lambda sql, params=(): captured.update(sql=sql, params=params) or [],
    )
    call()
    return " ".join(captured["sql"].split()), captured.get("params")


def test_open_actions_covers_every_live_status(monkeypatch):
    # Сторож красных линий обязан видеть ВСЕ живые изменения кабинета: и
    # применённые, и зависшие после обрыва (по ним изменение с высокой
    # вероятностью состоялось). Строка в статусе planned сюда не входит —
    # именно поэтому зависшую сначала переводят в 'stale'.
    sql, _ = _captured_sql(monkeypatch, writer_db.open_actions)

    expected = ", ".join("'%s'" % x for x in writer_db.LIVE_STATUSES)
    assert "status IN (%s)" % expected in sql
    assert "rolled_back_at IS NULL" in sql
    assert "'planned'" not in sql
    assert "rolled_back" not in writer_db.LIVE_STATUSES


def test_open_actions_exclude_closed_observation(monkeypatch):
    # Действие, чьё наблюдение закрыто вердиктом «выдержало», из-под сторожа
    # уходит: пересчитывать закрытое окно каждый прогон незачем (Э2.4).
    sql, _ = _captured_sql(monkeypatch, writer_db.open_actions)
    assert "observation_closed_at IS NULL" in sql


def test_mark_observation_closed_sql_claims_only_open_rows():
    # Захват атомарный и только своей строки: уже закрытую, откатанную или
    # неоткатываемую строку повторное закрытие не трогает — из двух
    # параллельных сторожей эксперимент записывает ровно один.
    sql = " ".join(writer_db.MARK_OBSERVATION_CLOSED_SQL.split())
    assert sql.startswith("UPDATE edu_agent_actions")
    assert "observation_closed_at = now()" in sql
    assert "WHERE action_id = %(action_id)s" in sql
    assert "AND observation_closed_at IS NULL" in sql
    assert "AND rolled_back_at IS NULL" in sql
    assert "AND rollback_failed_at IS NULL" in sql
    assert "RETURNING" in sql


def test_spent_risk_charges_every_live_status(monkeypatch):
    # Зависшее изменение живо в кабинете и обязано занимать риск-бюджет:
    # иначе прогон выдаёт лимит, которого нет.
    sql, _ = _captured_sql(monkeypatch, lambda: writer_db.spent_risk("2026-08-01"))

    expected = ", ".join("'%s'" % x for x in writer_db.RISK_CHARGED_STATUSES)
    assert "status IN (%s)" % expected in sql


def test_spent_risk_sums_every_charged_row(monkeypatch):
    # Дельта-модель: строка несёт цену СВОЕГО изменения, а не объекта, и две
    # правки одной кампании — два разных риска. Прежний максимум по объекту
    # защищал от модели, где каждое действие платило за всю кампанию; теперь
    # переплату ограничивает потолок объекта в fit_into_budget.
    sql, _ = _captured_sql(monkeypatch, lambda: writer_db.spent_risk("2026-08-17"))
    assert "SUM(risk_rub)" in sql
    assert "MAX(risk_rub)" not in sql


def test_charged_risk_key_matches_the_key_risk_model_uses(monkeypatch):
    # Форма ключа здесь и в risk.risk_object обязана совпадать: разъедутся —
    # прогон не узнает свой же оплаченный объект и спишет за него второй раз,
    # то есть ровно тот дефект, против которого эта функция и сделана.
    monkeypatch.setattr(
        writer_db, "_fetch",
        lambda sql, params=(): [{"object_level": "campaign",
                                 "object_id": "114057545", "spent": 1234.5}],
    )

    got = writer_db.charged_risk_by_object("2026-08-17")

    expected = risk.risk_object({"object_level": "campaign", "object_id": "114057545"})
    # Значение — СУММА списаний по объекту: потолок в fit_into_budget
    # добирает до цены объекта, а не переключает флаг «оплачен».
    assert got == {expected: 1234.5}


def test_mark_stale_sql_marks_only_rows_past_threshold():
    sql = " ".join(writer_db.MARK_STALE_SQL.split())
    assert sql.startswith("UPDATE edu_agent_actions")
    assert "SET status = 'stale'" in sql
    assert "WHERE status = 'planned'" in sql
    assert "created_at < now() - make_interval(mins => %s)" in sql
    # applied_at = момент ОТПРАВКИ, не момент обнаружения: иначе изменение
    # прошлой недели съело бы риск-бюджет текущей.
    assert "applied_at = COALESCE(applied_at, sent_at, created_at)" in sql
    assert "sent_at IS NOT NULL" in sql
    # RETURNING — то, чем отчёт ограничивается первым обнаружением.
    assert "RETURNING" in sql


# ==================================================================== живой SQL
# Запросы движка записи проверялись только сравнением подстрок в тексте.
# Синтаксическая или семантическая ошибка в них вылезла бы впервые в бою — на
# записи в журнал НЕПОСРЕДСТВЕННО ПЕРЕД отправкой изменения в кабинет, то есть
# уронила бы прогон в самый неудачный момент. Ниже — тесты, которые реально
# исполняют эти запросы (гейт DATABASE_URL, уборка за собой, как в
# test_spent_risk_counts_by_applied_week_not_created_week выше).

live_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="нужен DATABASE_URL")


def _journal_row(idem_key: str, account: str, **over):
    row = {
        "idempotency_key": idem_key,
        "account": account,
        "object_level": "campaign",
        "object_id": "111",
        "action_kind": "bidmodifier.set",
        # Сегмент — отдельными полями: у bidmodifier.set в payload его нет
        # вовсе, а история откатов адресуется именно по сегменту.
        "direct_type": "MOBILE_ADJUSTMENT",
        "key": "MOBILE",
        "payload": {"Id": 7, "BidModifier": 30},
        "previous_state": {"Id": 7, "percent": 10},
        "red_line": {"max_cpa": 1000.0},
        "risk_rub": 100.0,
    }
    row.update(over)
    return row


def _read_row(key: str):
    rows = writer_db._fetch(
        "SELECT * FROM edu_agent_actions WHERE idempotency_key = %s", (key,))
    return rows[0] if rows else None


def _backdate(key: str, minutes: int) -> None:
    """Сдвигает created_at строки в прошлое — так воспроизводится обрыв
    прошлого прогона без ожидания реального времени."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE edu_agent_actions "
                "SET created_at = now() - make_interval(mins => %s) "
                "WHERE idempotency_key = %s",
                (minutes, key),
            )
        conn.commit()


def _cleanup(*keys) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM edu_agent_actions WHERE idempotency_key = ANY(%s)", (list(keys),))
        conn.commit()


@live_db
def test_live_repeat_of_unapplied_action_refreshes_state_risk_and_red_line():
    # Первый прогон сохранил прошлое состояние и упал на отправке; человек
    # поправил корректировку руками; второй прогон прочитал свежий факт из API.
    # Ключ идемпотентности тот же — строка ОБЯЗАНА обновиться, иначе откат
    # вернёт кабинет не туда, откуда агент его вывел.
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    key = "test-reinsert-" + suffix
    account = "test-" + suffix
    try:
        first_id = writer_db.insert_action(_journal_row(key, account))
        writer_db.mark_action(first_id, "failed", {"error": "сеть недоступна"})

        second_id = writer_db.insert_action(_journal_row(
            key, account,
            previous_state={"Id": 7, "percent": 25},
            red_line={"max_cpa": 2500.0},
            risk_rub=333.0,
        ))

        assert second_id == first_id  # ключ детерминирован, строка та же
        row = _read_row(key)
        assert row["previous_state"] == {"Id": 7, "percent": 25}
        assert row["red_line"] == {"max_cpa": 2500.0}
        assert row["risk_rub"] == 333.0
        # Прошлая ошибка не должна выглядеть ответом на новую попытку.
        assert row["status"] == "planned"
        assert row["response"] == {}
    finally:
        _cleanup(key)


@live_db
def test_live_mark_observation_closed_is_atomic_and_hides_row_from_watch():
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    key = "test-obs-closed-" + suffix
    account = "test-" + suffix
    try:
        action_id = writer_db.insert_action(_journal_row(key, account))
        writer_db.mark_action(action_id, "applied", {"SetResults": [{"Id": 7}]})
        assert any(a["action_id"] == action_id for a in writer_db.open_actions())

        assert writer_db.mark_observation_closed(action_id, "held") is True
        # Повторный захват — уже чужой: строка закрыта первым прогоном.
        assert writer_db.mark_observation_closed(action_id, "held") is False

        row = _read_row(key)
        assert row["observation_closed_at"] is not None
        assert row["observation_verdict"] == "held"
        assert all(a["action_id"] != action_id for a in writer_db.open_actions())
    finally:
        _cleanup(key)


@live_db
def test_live_repeat_of_applied_action_leaves_row_untouched():
    # Уже применённая строка неприкосновенна: её previous_state описывает
    # реально совершённое изменение и является единственным основанием отката.
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    key = "test-applied-" + suffix
    account = "test-" + suffix
    try:
        action_id = writer_db.insert_action(_journal_row(key, account))
        writer_db.mark_action(action_id, "applied", {"SetResults": [{"Id": 7}]})

        writer_db.insert_action(_journal_row(
            key, account,
            previous_state={"Id": 7, "percent": 99},
            red_line={"max_cpa": 9999.0},
            risk_rub=777.0,
        ))

        row = _read_row(key)
        assert row["status"] == "applied"
        assert row["previous_state"] == {"Id": 7, "percent": 10}
        assert row["red_line"] == {"max_cpa": 1000.0}
        assert row["risk_rub"] == 100.0
        assert row["applied_at"] is not None
    finally:
        _cleanup(key)


@live_db
def test_live_stale_planned_finds_only_rows_older_than_threshold():
    # Обрыв ПОСЛЕ отправки: строка осталась planned. Свежая строка того же
    # прогона зависшей не считается — порог отделяет одно от другого.
    # Читающий дубль stale_planned удалён: и находку, и пометку делает
    # mark_stale_planned одним UPDATE ... RETURNING — живой тест проверяет
    # то же условие через него.
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    stuck = "test-stuck-" + suffix
    fresh = "test-fresh-" + suffix
    account = "test-" + suffix
    try:
        writer_db.insert_action(_journal_row(stuck, account, object_id="111"))
        writer_db.insert_action(_journal_row(fresh, account, object_id="222"))
        _backdate(stuck, minutes=90)

        # Фильтр по кабинету: чужой кабинет зависшую строку не забирает.
        assert writer_db.mark_stale_planned(60, account=account + "-other") == []
        assert _read_row(stuck)["status"] == "planned"

        found = writer_db.mark_stale_planned(60, account=account)

        assert [r["idempotency_key"] for r in found] == [stuck]
        assert found[0]["object_id"] == "111"
        # Свежая строка того же прогона не тронута порогом.
        assert _read_row(fresh)["status"] == "planned"
    finally:
        _cleanup(stuck, fresh)


@live_db
def test_live_mark_stale_reports_finding_once_and_keeps_it_visible():
    # Дефект: зависшая строка печаталась в отчёте КАЖДОГО прогона вечно и
    # больше ничем не отзывалась. Теперь она переводится в статус 'stale':
    # отчёт показывает её один раз, а сама она попадает в поле зрения сторожа
    # отката и списывает риск.
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    key = "test-stale-" + suffix
    account = "test-" + suffix
    try:
        writer_db.insert_action(_journal_row(key, account))
        _backdate(key, minutes=90)
        created_at = _read_row(key)["created_at"]

        first = writer_db.mark_stale_planned(60, account=account)
        second = writer_db.mark_stale_planned(60, account=account)

        assert [r["idempotency_key"] for r in first] == [key]
        assert second == []  # отчёт не повторяет одну и ту же находку вечно

        row = _read_row(key)
        assert row["status"] == "stale"
        # Момент отправки, а не момент обнаружения: изменение прошлой недели не
        # должно съедать риск-бюджет текущей.
        assert row["applied_at"] == created_at
        assert row["response"]["stale"] is True
    finally:
        _cleanup(key)


@live_db
def test_live_stale_row_is_watched_and_charged_to_risk():
    # Три следствия статуса 'stale', ради которых он и заведён.
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    key = "test-stale-live-" + suffix
    account = "test-" + suffix
    since = (datetime.utcnow() - timedelta(days=1)).isoformat()
    try:
        writer_db.insert_action(_journal_row(key, account, risk_rub=444.0))
        _backdate(key, minutes=90)
        baseline = writer_db.spent_risk(since)
        writer_db.mark_stale_planned(60, account=account)

        # 1. Сторож отката её видит.
        assert key in {r["idempotency_key"] for r in writer_db.open_actions()}
        # 2. Риск за живое непроверенное изменение списан.
        assert writer_db.spent_risk(since) - baseline == pytest.approx(444.0)
        # 3. Повторная планировка не переписывает её прошлое состояние —
        #    единственное основание для отката.
        writer_db.insert_action(_journal_row(
            key, account, previous_state={"Id": 7, "percent": 99}, risk_rub=1.0))
        row = _read_row(key)
        assert row["status"] == "stale"
        assert row["previous_state"] == {"Id": 7, "percent": 10}
        assert row["risk_rub"] == 444.0
    finally:
        _cleanup(key)


# ============================================================ гард отметки
# Дефект B: отметка результата была ЕДИНСТВЕННЫМ запросом модуля без условия
# на текущее состояние строки. Достижимое состояние — статус 'applied' И
# проставленный rolled_back_at одновременно: наблюдение такую строку не видит,
# риск-бюджет её не считает, переприменить нельзя. Действие исчезает из всех
# трёх контуров разом.


def test_mark_action_sql_refuses_rolled_back_and_final_rows():
    sql = " ".join(writer_db.MARK_ACTION_SQL.split())
    assert sql.startswith("UPDATE edu_agent_actions")
    assert "rolled_back_at IS NULL" in sql
    assert "status NOT IN (" in sql
    for status in writer_db.FINAL_STATUSES:
        assert "'%s'" % status in sql.rsplit("NOT IN", 1)[1], status
    # RETURNING — то, чем вызывающий код отличает «отметили» от «строку забрал
    # другой контур». Без него гард молчит, и апплай считает действие успешным.
    assert "RETURNING" in sql


def test_mark_unknown_outcome_sql_puts_row_under_watch():
    sql = " ".join(writer_db.MARK_UNKNOWN_OUTCOME_SQL.split())
    assert "SET status = 'stale'" in sql
    # applied_at обязателен: по нему считается и окно наблюдения, и неделя
    # риск-бюджета. Без него строка формально живая, но невидимая обоим.
    assert "applied_at = COALESCE(applied_at, created_at, now())" in sql
    assert "rolled_back_at IS NULL" in sql
    assert "RETURNING" in sql


def test_unknown_outcome_status_is_live_and_final():
    # Неизвестный исход попадает в тот же контур, что и обрыв процесса:
    # под наблюдение и под риск-бюджет (LIVE), без повторной отправки (FINAL).
    assert "stale" in writer_db.LIVE_STATUSES
    assert "stale" in writer_db.FINAL_STATUSES


# ==================================================== обратная связь от отката


def test_harmful_segments_sql_groups_by_object_and_segment():
    sql = " ".join(writer_db.HARMFUL_SEGMENTS_SQL.split())
    # Ключ истории — объект и сегмент. Процента в группировке нет и быть не
    # может: он дрейфует между расчётами и обходил бы кулдаун.
    assert "GROUP BY 1, 2, 3, 4" in sql
    assert "percent" not in sql
    assert "make_interval(days => %s)" in sql
    # Строки старого формата: сегмент достаётся из payload, пока колонок не было.
    assert "COALESCE(direct_type, payload->>'Type')" in sql


def test_cooldown_covers_breach_without_successful_rollback():
    # Дефект 3: кулдаун смотрел только на отметку об УСПЕШНОМ откате. Пробитая
    # красная линия без удавшегося возврата планировщику не сообщалась вовсе —
    # процент дрейфует на пункт, ключ идемпотентности получается новый, и агент
    # назавтра крутит тот же сегмент ещё раз, поверх живого вредного изменения.
    sql = " ".join(writer_db.HARMFUL_SEGMENTS_SQL.split())
    assert "harmful_verdict_at" in sql
    assert "GREATEST(rolled_back_at, harmful_verdict_at)" in sql
    # Условия «только откатанные» больше нет: оно и отсекало неоткатанные.
    assert "WHERE rolled_back_at IS NOT NULL" not in sql


def test_harmful_verdict_columns_are_added_by_ddl():
    ddl = " ".join(" ".join(WRITER_DDL).split())
    assert "ADD COLUMN IF NOT EXISTS harmful_verdict_at" in ddl
    assert "ADD COLUMN IF NOT EXISTS harmful_reason" in ddl


def test_mark_harmful_sql_keeps_the_first_verdict_time():
    sql = " ".join(writer_db.MARK_HARMFUL_SQL.split())
    # Кулдаун отсчитывается от ПЕРВОГО пробоя: повторный прогон сторожа не
    # должен продлевать его задним числом.
    assert "harmful_verdict_at = COALESCE(harmful_verdict_at, now())" in sql
    assert "RETURNING" in sql


# ======================================= переход «откатано» тоже под гардом


def test_mark_rolled_back_sql_has_state_guard_and_confirms_the_update():
    # Дефект 4: единственный переход состояния, остававшийся безусловным —
    # UPDATE по одному action_id, без условия на текущее состояние и без
    # подтверждения, что строка действительно изменилась.
    sql = " ".join(writer_db.MARK_ROLLED_BACK_SQL.split())
    assert "WHERE action_id = %(action_id)s" in sql
    assert "rolled_back_at IS NULL" in sql
    expected = ", ".join("'%s'" % x for x in writer_db.LIVE_STATUSES)
    assert "status IN (%s)" % expected in sql
    assert "RETURNING" in sql


def test_mark_rolled_back_reports_whether_the_row_was_taken(monkeypatch):
    # Вызывающий код обязан различать «отметили» и «строку уже забрал другой
    # контур»: отчёт сторожа иначе утверждает про журнал то, чего в нём нет.
    class _Cur:
        def __init__(self, row):
            self.row = row

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return self.row

    class _Conn:
        def __init__(self, row):
            self.row = row

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self, *a, **k):
            return _Cur(self.row)

        def commit(self):
            pass

    monkeypatch.setattr(writer_db, "get_connection", lambda: _Conn(("act-1",)))
    assert writer_db.mark_rolled_back("act-1") is True

    monkeypatch.setattr(writer_db, "get_connection", lambda: _Conn(None))
    assert writer_db.mark_rolled_back("act-1") is False


# ================================ срок хранения строк репетиционных прогонов


def test_purge_touches_only_rehearsal_rows():
    # У строк репетиции не было судьбы: статус не финальный и не живой, ни один
    # механизм журнала их не закрывает — и они копились вечно. Удаление
    # безопасно ровно потому, что за ними не стоит ни одного запроса в кабинет.
    sql = " ".join(writer_db.PURGE_DRY_RUN_SQL.split())
    assert sql.startswith("DELETE FROM edu_agent_actions")
    assert "status = 'dry_run'" in sql
    assert "created_at < now() - make_interval(days => %s)" in sql
    for status in set(writer_db.FINAL_STATUSES) | set(writer_db.LIVE_STATUSES):
        assert "'%s'" % status not in sql, status
    assert writer_db.DRY_RUN_RETENTION_DAYS > 0


def test_insert_action_writes_segment_columns():
    sql = " ".join(writer_db.INSERT_ACTION_SQL.split())
    assert "direct_type" in sql and "setting_key" in sql
    assert "direct_type = EXCLUDED.direct_type" in sql
    assert "setting_key = EXCLUDED.setting_key" in sql


def test_final_status_keys_asks_only_about_given_keys(monkeypatch):
    sql, params = _captured_sql(
        monkeypatch, lambda: writer_db.final_status_keys(["a", "b"]))

    assert "idempotency_key = ANY(%s)" in sql
    expected = ", ".join("'%s'" % x for x in writer_db.FINAL_STATUSES)
    assert "status IN (%s)" % expected in sql
    assert params == (["a", "b"],)


def test_final_status_keys_does_not_query_on_empty_input(monkeypatch):
    called = []
    monkeypatch.setattr(writer_db, "_fetch",
                        lambda sql, params=(): called.append(1) or [])

    assert writer_db.final_status_keys([]) == set()
    assert called == []


# ============================================================ аренда на прогон


# Проверки «имя сторожа есть в реестре RUN_LOCK_NAMES» здесь больше нет: она
# была зелёной ровно всё время, пока сторож аренду НЕ БРАЛ, — то есть давала
# ложное чувство покрытия. Факт использования аренды каждым рабочим процессом
# проверяется там, где он и происходит: tests/test_agent_e1.py и
# tests/test_agent_e1_watchdog.py (main обязан взять аренду и остановиться,
# если она занята). Реестр как таковой прикрыт снизу — run_lock отвергает
# незнакомое имя (тест ниже).


def test_acquire_run_lock_sql_takes_only_expired_lease():
    sql = " ".join(writer_db.ACQUIRE_RUN_LOCK_SQL.split())
    assert "ON CONFLICT (lock_name) DO UPDATE SET" in sql
    # Чужую живую аренду перехватывать нельзя — только протухшую.
    assert "WHERE edu_agent_run_lock.expires_at < now()" in sql
    assert "RETURNING" in sql


def test_run_lock_rejects_unknown_name():
    with pytest.raises(ValueError):
        with writer_db.run_lock("agent_e9"):
            pass  # pragma: no cover


# ---------------------------------------- аренда продлевается и перепроверяется
# Дефект 5: аренду брали на час и больше о ней не вспоминали. Прогон по сотням
# кампаний (чтение состояния по каждой, ретраи, таймауты по две минуты) живёт
# дольше часа — аренда протухает НА ХОДУ, следующий прогон стартует штатно, и
# оба шлют bidmodifiers.add по одной кампании. Это ровно тот сценарий с двумя
# объектами в кабинете, ради которого аренда и заводилась.


def test_renew_run_lock_sql_extends_only_our_own_live_lease():
    sql = " ".join(writer_db.RENEW_RUN_LOCK_SQL.split())
    assert "expires_at = now() + make_interval(mins => %(ttl)s)" in sql
    assert "holder = %(holder)s" in sql      # чужую аренду не продлеваем
    assert "expires_at > now()" in sql       # и протухшую — тоже не воскрешаем
    assert "RETURNING" in sql


class _FakeCursor:
    def __init__(self, conn, row):
        self.conn = conn
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))

    def fetchone(self):
        return self.row


class _FakeConn:
    def __init__(self, row):
        self.row = row
        self.executed = []
        self.commits = 0

    def cursor(self, *a, **k):
        return _FakeCursor(self, self.row)

    def commit(self):
        self.commits += 1


def test_lease_renew_reports_loss_instead_of_pretending_to_own():
    alive = writer_db.RunLease(_FakeConn(("2026-08-20",)), "agent_writer", "me", 60)
    assert alive.renew() is True
    alive.guard()   # владение подтверждено — прогон продолжается

    lost = writer_db.RunLease(_FakeConn(None), "agent_writer", "me", 60)
    assert lost.renew() is False
    with pytest.raises(writer_db.RunLeaseLost):
        lost.guard()


def test_lease_guard_renews_the_lease_it_checks():
    # Продление и перепроверка — один и тот же запрос: пока прогон работает,
    # аренда не протухает, а если она уже не наша, это видно сразу же.
    conn = _FakeConn(("2026-08-20",))
    lease = writer_db.RunLease(conn, "agent_writer", "me", 60)

    lease.guard()

    assert len(conn.executed) == 1
    assert conn.executed[0][0] is writer_db.RENEW_RUN_LOCK_SQL
    assert conn.executed[0][1] == {"name": "agent_writer", "holder": "me", "ttl": 60}


def _lock_expires(name: str):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT expires_at FROM edu_agent_run_lock WHERE lock_name = %s",
                        (name,))
            row = cur.fetchone()
    return row[0] if row else None


# ================================================================= живой SQL


@live_db
def test_live_mark_action_does_not_resurrect_rolled_back_row():
    # Гонка: apply увидел нефинальный статус и отправил запрос; пока он летел,
    # сторож перевёл строку в 'stale' и откатил её. Безусловный UPDATE затирал
    # откат обратно в 'applied' — и строка выпадала из наблюдения, из
    # риск-бюджета и из переприменения одновременно.
    ensure_writer_tables()
    key = "test-mark-guard-" + uuid.uuid4().hex[:8]
    try:
        action_id = writer_db.insert_action(_journal_row(key, "test-acc"))
        # Откатить можно только живую строку — гард mark_rolled_back.
        assert writer_db.mark_action(action_id, "applied", {"ok": True}) is True
        assert writer_db.mark_rolled_back(action_id) is True

        landed = writer_db.mark_action(action_id, "applied", {"ok": True})

        assert landed is False, "отметка обязана НЕ ложиться на откатанную строку"
        row = _read_row(key)
        assert row["status"] == "rolled_back"
        assert row["rolled_back_at"] is not None
    finally:
        _cleanup(key)


@live_db
def test_live_mark_action_lands_on_planned_row():
    # Обратная половина гарда: нормальный путь обязан работать, иначе гард
    # зеленел бы просто потому, что не обновляет ничего и никогда.
    ensure_writer_tables()
    key = "test-mark-ok-" + uuid.uuid4().hex[:8]
    try:
        action_id = writer_db.insert_action(_journal_row(key, "test-acc"))

        assert writer_db.mark_action(action_id, "applied", {"ok": True}) is True

        row = _read_row(key)
        assert row["status"] == "applied"
        assert row["applied_at"] is not None
    finally:
        _cleanup(key)


@live_db
def test_live_unknown_outcome_row_is_watched_and_charged():
    # Строка с неизвестным исходом обязана вести себя как зависшая: попадать
    # в наблюдение сторожа и занимать риск-бюджет. Иначе изменение живёт в
    # кабинете бесплатно и без присмотра.
    ensure_writer_tables()
    key = "test-unknown-" + uuid.uuid4().hex[:8]
    account = "test-" + uuid.uuid4().hex[:8]
    try:
        action_id = writer_db.insert_action(_journal_row(key, account))

        assert writer_db.mark_unknown_outcome(action_id, "ReadTimeout") is True

        row = _read_row(key)
        assert row["status"] == "stale"
        assert row["applied_at"] is not None
        assert row["response"]["unknown_outcome"] is True
        assert key in {r["idempotency_key"] for r in writer_db.open_actions()}
        week = row["applied_at"].date().isoformat()
        assert writer_db.spent_risk(week) >= 100.0
    finally:
        _cleanup(key)


@live_db
def test_live_unknown_outcome_is_not_sent_again():
    # 'stale' входит в FINAL_STATUSES: повторная отправка исключена, иначе
    # bidmodifiers.add создал бы в кабинете второй объект.
    ensure_writer_tables()
    key = "test-unknown-repeat-" + uuid.uuid4().hex[:8]
    try:
        action_id = writer_db.insert_action(_journal_row(key, "test-acc"))
        writer_db.mark_unknown_outcome(action_id, "ReadTimeout")

        assert writer_db.final_status_keys([key]) == {key}
        # Повторная планировка того же ключа не переписывает previous_state.
        writer_db.insert_action(_journal_row(key, "test-acc",
                                             previous_state={"Id": 7, "percent": 99}))
        assert _read_row(key)["previous_state"] == {"Id": 7, "percent": 10}
    finally:
        _cleanup(key)


@live_db
def test_live_rolled_back_segment_is_visible_to_planning():
    # Ключевой факт дефекта A: после отката планирование обязано УЗНАТЬ об
    # этом по объекту и сегменту, а не по проценту.
    ensure_writer_tables()
    key = "test-cooldown-" + uuid.uuid4().hex[:8]
    account = "test-" + uuid.uuid4().hex[:8]
    try:
        action_id = writer_db.insert_action(_journal_row(
            key, account, direct_type="MOBILE_ADJUSTMENT", key="MOBILE"))
        writer_db.mark_action(action_id, "applied", {})
        writer_db.mark_rolled_back(action_id)

        cooled = writer_db.harmful_segments(60, account=account)

        assert ("111", "MOBILE_ADJUSTMENT", "MOBILE") in cooled
    finally:
        _cleanup(key)


@live_db
def test_live_rollback_outside_cooldown_window_does_not_block():
    # Кулдаун конечен: старый откат планирование не запирает навсегда.
    ensure_writer_tables()
    key = "test-cooldown-old-" + uuid.uuid4().hex[:8]
    account = "test-" + uuid.uuid4().hex[:8]
    try:
        action_id = writer_db.insert_action(_journal_row(
            key, account, direct_type="MOBILE_ADJUSTMENT", key="MOBILE"))
        writer_db.mark_action(action_id, "applied", {})
        writer_db.mark_rolled_back(action_id)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE edu_agent_actions "
                    "SET rolled_back_at = now() - make_interval(days => 61) "
                    "WHERE idempotency_key = %s",
                    (key,),
                )
            conn.commit()

        assert writer_db.harmful_segments(60, account=account) == {}
    finally:
        _cleanup(key)


@live_db
def test_live_second_run_cannot_take_the_same_lock():
    # Два одновременных прогона на одном ключе создают в кабинете два объекта.
    ensure_writer_tables()
    with writer_db.run_lock("agent_writer"):
        with pytest.raises(writer_db.RunLockBusy):
            with writer_db.run_lock("agent_writer"):
                pass  # pragma: no cover

    # Аренда снята по выходу — следующий прогон стартует штатно.
    with writer_db.run_lock("agent_writer"):
        pass


@live_db
def test_live_expired_lease_is_taken_over():
    # Смерть процесса не должна блокировать движок навсегда: аренда протухает.
    ensure_writer_tables()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO edu_agent_run_lock (lock_name, holder, expires_at) "
                "VALUES ('agent_e1', 'dead-process', now() - make_interval(mins => 1)) "
                "ON CONFLICT (lock_name) DO UPDATE SET holder = EXCLUDED.holder, "
                "expires_at = EXCLUDED.expires_at"
            )
        conn.commit()

    with writer_db.run_lock("agent_writer") as holder:
        assert holder != "dead-process"


@live_db
def test_live_lease_is_renewable_while_the_run_is_alive():
    # Продление сдвигает срок годности вперёд: прогон, идущий дольше часа, не
    # теряет аренду, и второй прогон не стартует.
    ensure_writer_tables()
    with writer_db.run_lock("agent_e1", ttl_minutes=1) as lease:
        before = _lock_expires("agent_writer")
        lease.ttl_minutes = 60
        assert lease.renew() is True
        assert _lock_expires("agent_writer") > before

        # И чужой прогон по-прежнему не пройдёт.
        with pytest.raises(writer_db.RunLockBusy):
            with writer_db.run_lock("agent_writer"):
                pass  # pragma: no cover


@live_db
def test_live_lost_lease_is_not_silently_renewed():
    # Аренду перехватили — продлевать её нельзя ни в коем случае: иначе прогон
    # отобрал бы у живого владельца то, что тот успел взять.
    ensure_writer_tables()
    with writer_db.run_lock("agent_writer") as lease:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE edu_agent_run_lock SET holder = 'other-run' "
                    "WHERE lock_name = 'agent_writer'"
                )
            conn.commit()

        assert lease.renew() is False
        with pytest.raises(writer_db.RunLeaseLost):
            lease.guard()

    # Уборка: чужую аренду контекст не снимает, снимаем сами.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM edu_agent_run_lock WHERE lock_name = 'agent_writer'")
        conn.commit()


def test_writer_lock_is_single_and_shared():
    # Прямое применение и сторож отката пишут в ОДИН кабинет: параллельный
    # прогон e1 во время отката (или наоборот) — та же гонка, что два e1.
    # Прежние независимые имена agent_e1/agent_e1_watchdog разрешали её
    # молча. Аренда одна на всех пишущих.
    assert writer_db.RUN_LOCK_NAMES == ("agent_writer",)


# =========================================================================
# Дефект И3: у применения не было потолка попыток
# =========================================================================


def test_apply_attempts_column_is_added_by_ddl():
    ddl = chr(10).join(WRITER_DDL)
    assert "apply_attempts" in ddl
    # Отдельным ALTER, а не правкой CREATE TABLE: таблица уже существует в
    # базе, и CREATE TABLE IF NOT EXISTS её не трогает — колонка в теле
    # CREATE появилась бы только на чистой базе.
    assert "ADD COLUMN IF NOT EXISTS apply_attempts" in ddl


def test_attempts_grow_only_on_reattempted_statuses():
    # Счётчик обязан расти ровно там, где действие уйдёт на повтор.
    # 'applied' и 'dry_run' его не двигают: за ними повтора нет.
    assert writer_db.REATTEMPTED_STATUSES == ("rejected", "failed")
    for status in writer_db.REATTEMPTED_STATUSES:
        assert f"'{status}'" in writer_db.MARK_ACTION_SQL
    assert "apply_attempts = apply_attempts + CASE" in writer_db.MARK_ACTION_SQL


def test_replanning_does_not_reset_the_attempt_counter():
    # Повторная планировка обновляет previous_state, риск и красную линию —
    # но не счётчик попыток. Сбрасывай она его, потолок не наступал бы
    # никогда: каждый прогон переписывает строку заново.
    assert "apply_attempts" not in writer_db.INSERT_ACTION_SQL


def test_exhausted_segments_sql_counts_by_segment_not_by_key():
    sql = writer_db.EXHAUSTED_SEGMENTS_SQL
    # Группировка по объекту и сегменту, а не по ключу идемпотентности: в ключ
    # зашит процент, он дрейфует между расчётами, и у каждой попытки свой ключ.
    assert "idempotency_key" not in sql
    assert "SUM(apply_attempts)" in sql
    assert "GROUP BY" in sql and "HAVING SUM(apply_attempts) >= %s" in sql
    # Сегмент восстанавливается и у строк, записанных до появления колонок.
    assert "COALESCE(direct_type, payload->>'Type')" in sql
    assert "COALESCE(setting_key, payload->>'key')" in sql


def test_exhausted_segments_looks_only_at_refused_rows():
    # Сегмент, по которому изменение однажды состоялось, стеной не является:
    # строки в 'applied'/'stale'/'rolled_back' в выборку не входят.
    sql = writer_db.EXHAUSTED_SEGMENTS_SQL
    assert "status IN ('rejected', 'failed')" in sql
    for closed in ("applied", "rolled_back", "stale"):
        assert f"'{closed}'" not in sql


def test_exhausted_segments_passes_account_and_threshold(monkeypatch):
    seen = {}
    monkeypatch.setattr(writer_db, "_fetch",
                        lambda sql, params=(): seen.update({"sql": sql, "params": params}) or [])

    writer_db.exhausted_segments(account="cab")

    # Окно памяти о неудачах едет третьим параметром: блокировка сегмента
    # больше не вечная (EXHAUSTED_RETENTION_DAYS).
    assert seen["params"] == ("cab", "cab", writer_db.EXHAUSTED_RETENTION_DAYS,
                              writer_db.MAX_APPLY_ATTEMPTS)


def test_apply_attempt_cap_matches_the_rollback_one():
    # Разовый сбой стоит повторить, детерминированный — нет. Довод и порядок
    # величины у обоих путей одинаковые, и расхождение цифр было бы случайным.
    assert writer_db.MAX_APPLY_ATTEMPTS == writer_db.MAX_ROLLBACK_ATTEMPTS == 3


# =========================================================================
# Мелкое: пометка неудачного отката шла без условия на состояние строки
# =========================================================================


def test_mark_rollback_failed_sql_has_a_state_guard():
    # Единственный переход модуля, остававшийся без гарда: счётчик попыток рос
    # и у УЖЕ ОТКАТАННОЙ строки, если её забрал параллельный контур между
    # решением и отметкой, — и следующая настоящая неудача хоронила бы её
    # раньше срока.
    sql = writer_db.MARK_ROLLBACK_FAILED_SQL
    assert "rolled_back_at IS NULL" in sql
    assert "status IN ('applied', 'stale')" in sql


def test_every_state_transition_has_a_guard():
    # Свойство всего модуля, а не одного запроса: строку между решением и
    # отметкой может забрать другой контур, и безусловный UPDATE затирает
    # чужой, более точный исход.
    for sql in (writer_db.MARK_ACTION_SQL, writer_db.MARK_UNKNOWN_OUTCOME_SQL,
                writer_db.MARK_ROLLED_BACK_SQL, writer_db.MARK_HARMFUL_SQL,
                writer_db.MARK_ROLLBACK_FAILED_SQL):
        assert "rolled_back_at IS NULL" in sql
        assert "RETURNING" in sql


# --------------- живое исполнение выборок по сегментам
# Оба запроса до сих пор проверялись только подстроками в тексте и подменённым
# _fetch. Их особенность в том, что ошибка была бы не синтаксической, а
# семантической: GROUP BY по выражению с COALESCE, HAVING поверх SUM, три
# позиционных параметра подряд, из которых два — один и тот же account.
# Такое ловится только исполнением против Postgres.


def _set_columns(key: str, **cols) -> None:
    """Проставляет колонки, которых нет в insert_action (статус попыток,
    отметки вердиктов) — воспроизводит состояние журнала после прогонов."""
    sets = ", ".join(f"{name} = %s" for name in cols)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE edu_agent_actions SET {sets} WHERE idempotency_key = %s",
                (*cols.values(), key),
            )
        conn.commit()


@live_db
def test_live_exhausted_segments_sums_attempts_of_one_segment():
    # Два прогона бились в один сегмент разными ключами идемпотентности —
    # порог считается по СУММЕ попыток сегмента, иначе стена не видна никогда.
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    account = "test-" + suffix
    first, second = "test-exh-a-" + suffix, "test-exh-b-" + suffix
    try:
        writer_db.insert_action(_journal_row(first, account))
        writer_db.insert_action(_journal_row(second, account))
        _set_columns(first, status="failed", apply_attempts=1)
        _set_columns(second, status="rejected", apply_attempts=1)

        out = writer_db.exhausted_segments(max_attempts=2, account=account)

        assert list(out) == [("111", "MOBILE_ADJUSTMENT", "MOBILE")]
        assert out[("111", "MOBILE_ADJUSTMENT", "MOBILE")]["attempts"] == 2
        assert out[("111", "MOBILE_ADJUSTMENT", "MOBILE")]["last_attempt_at"] is not None

        # Порог выше суммы — сегмент ещё не исчерпан.
        assert writer_db.exhausted_segments(max_attempts=3, account=account) == {}
    finally:
        _cleanup(first, second)


@live_db
def test_live_exhausted_segments_ignores_a_segment_that_once_succeeded():
    # Строка дошла до applied — сегмент стеной не является, сколько бы попыток
    # ни было потрачено по дороге.
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    account = "test-" + suffix
    key = "test-exh-ok-" + suffix
    try:
        writer_db.insert_action(_journal_row(key, account))
        _set_columns(key, status="applied", apply_attempts=5)

        assert writer_db.exhausted_segments(max_attempts=1, account=account) == {}
    finally:
        _cleanup(key)


@live_db
def test_live_harmful_segments_respects_the_cooldown_window():
    # GREATEST по двум отметкам и сравнение с make_interval — выражение, которое
    # молча вернуло бы пустоту при ошибке в типах, то есть сняло бы кулдаун.
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    account = "test-" + suffix
    fresh, old = "test-harm-new-" + suffix, "test-harm-old-" + suffix
    try:
        writer_db.insert_action(_journal_row(fresh, account))
        writer_db.insert_action(_journal_row(old, account, object_id="222"))
        _set_columns(fresh, harmful_verdict_at=datetime.utcnow() - timedelta(days=1))
        _set_columns(old, rolled_back_at=datetime.utcnow() - timedelta(days=40))

        out = writer_db.harmful_segments(cooldown_days=14, account=account)

        assert list(out) == [("111", "MOBILE_ADJUSTMENT", "MOBILE")]
        # За горизонтом кулдауна ничего не осталось, а внутри широкого — обе.
        assert len(writer_db.harmful_segments(cooldown_days=60, account=account)) == 2
    finally:
        _cleanup(fresh, old)


@live_db
def test_live_harmful_segments_ignores_rows_without_a_verdict():
    # Строка без обеих отметок отсекается самим сравнением (NULL > interval даёт
    # NULL) — отдельного IS NOT NULL в запросе нет, и это утверждение живое.
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    account = "test-" + suffix
    key = "test-harm-none-" + suffix
    try:
        writer_db.insert_action(_journal_row(key, account))
        assert writer_db.harmful_segments(cooldown_days=14, account=account) == {}
    finally:
        _cleanup(key)


def test_spent_risk_charges_rolled_back_rows_too(monkeypatch):
    # Откат не возвращает деньги: экспозиция уже случилась, и худший недельный
    # исход считается по ВСЕМ применённым изменениям недели. Освобождение
    # бюджета откатом создавало насос: применили → пробили линию → откатили →
    # бюджет свободен → применили следующее, и недельный лимит переставал
    # ограничивать суммарный взятый риск.
    sql, _ = _captured_sql(monkeypatch, lambda: writer_db.spent_risk("2026-08-17"))

    assert "rolled_back_at" not in sql
    expected = ", ".join("'%s'" % x for x in writer_db.RISK_CHARGED_STATUSES)
    assert "status IN (%s)" % expected in sql
    assert "'rolled_back'" in sql


def test_spent_risk_window_covers_exposure_overlapping_week_start(monkeypatch):
    # Риск оценён на DEFAULT_DAYS_TO_MEASURE дней вперёд, значит действие,
    # применённое в конце прошлой недели, продолжает жить под риском в начале
    # этой. Окно «с понедельника» обнуляло счёт в ночь на понедельник, и
    # суммарная экспозиция могла достигать двух недельных лимитов сразу.
    sql, params = _captured_sql(
        monkeypatch, lambda: writer_db.spent_risk("2026-08-17"))

    assert "make_interval(days => %s)" in sql
    assert params == ("2026-08-17", risk.DEFAULT_DAYS_TO_MEASURE)


def test_charged_risk_matches_spent_risk_filter(monkeypatch):
    # Множество оплаченных объектов обязано считаться тем же фильтром, что и
    # сумма spent_risk: разъедутся — прогон либо спишет за объект второй раз,
    # либо посчитает свободным бюджет, которого нет.
    sql, params = _captured_sql(
        monkeypatch, lambda: writer_db.charged_risk_by_object("2026-08-17"))

    assert "rolled_back_at" not in sql
    expected = ", ".join("'%s'" % x for x in writer_db.RISK_CHARGED_STATUSES)
    assert "status IN (%s)" % expected in sql
    assert "make_interval(days => %s)" in sql
    assert params == ("2026-08-17", risk.DEFAULT_DAYS_TO_MEASURE)


def test_recent_action_objects_filters_kind_status_window_account(monkeypatch):
    # Кулдаун бюджета: трогали лимит кампании за последние N дней — в этот
    # такт не трогаем. Считаются только строки, чьё изменение (вероятно)
    # дошло до кабинета, — те же статусы, что списывают риск.
    sql, params = _captured_sql(
        monkeypatch,
        lambda: writer_db.recent_action_objects(
            ("budget.set", "budget.set_daily"), 14, account="acc"))

    assert "action_kind = ANY(%s)" in sql
    expected = ", ".join("'%s'" % x for x in writer_db.RISK_CHARGED_STATUSES)
    assert "status IN (%s)" % expected in sql
    assert "applied_at >= now() - make_interval(days => %s)" in sql
    assert params == (["budget.set", "budget.set_daily"], 14, "acc", "acc")


# ------------------------------- статусы deferred / aborted


def test_deferred_is_neither_final_nor_live_nor_reattempted():
    # 'deferred' — квота/недоступность API: не финальный (переприменяется),
    # не живой (в кабинете ничего не изменилось — сторожу и риск-бюджету не
    # виден), не reattempted (попытку не жжёт: кабинет виноват, не действие).
    assert "deferred" not in writer_db.FINAL_STATUSES
    assert "deferred" not in writer_db.LIVE_STATUSES
    assert "deferred" not in writer_db.REATTEMPTED_STATUSES
    assert "deferred" not in writer_db.RISK_CHARGED_STATUSES


def test_aborted_is_neither_final_nor_live():
    assert "aborted" not in writer_db.FINAL_STATUSES
    assert "aborted" not in writer_db.LIVE_STATUSES
    assert "aborted" not in writer_db.RISK_CHARGED_STATUSES


def test_mark_stale_requires_sent_at_and_aborts_the_rest():
    # Раскол зависших: 'stale' (исход неизвестен) — только у строк, чей
    # запрос ТОЧНО уходил (sent_at стоит). Строка без sent_at оборвалась до
    # отправки: кабинет её не видел, риск не списывается, статус 'aborted'.
    assert "sent_at IS NOT NULL" in writer_db.MARK_STALE_SQL
    assert "sent_at IS NULL" in writer_db.MARK_ABORTED_SQL
    assert "'aborted'" in writer_db.MARK_ABORTED_SQL
    assert "applied_at" not in writer_db.MARK_ABORTED_SQL


def test_exhausted_segments_forget_old_failures():
    # Счётчик попыток копится вечно: сегмент, у которого три отказа случились
    # полгода назад (кампанию с тех пор починили), заблокирован навсегда.
    # Считаются только попытки внутри окна retention.
    assert "EXHAUSTED_RETENTION_DAYS" in dir(writer_db)
    assert "created_at >= now() - make_interval(days => %s)" in \
        writer_db.EXHAUSTED_SEGMENTS_SQL
