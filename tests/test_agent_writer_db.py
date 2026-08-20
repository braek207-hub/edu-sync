# -*- coding: utf-8 -*-
import os
import uuid
from datetime import datetime, timedelta

import pytest

import sync.agent.writer.db as writer_db
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
    sql = " ".join(writer_db.INSERT_ACTION_SQL.split())
    assert "status = 'planned'" in sql
    assert "response = '{}'::jsonb" in sql
    assert "created_at = now()" in sql


# ------------------------------------- обрыв ПОСЛЕ отправки виден в отчёте
# Дефект 7: порядок «журнал → отправка» соблюдён, но смерть процесса ПОСЛЕ
# ухода запроса оставляла строку в статусе planned — без ответа и без Id
# созданного объекта. Такую строку не видит ни сторож применённых действий,
# ни откат; риск не списан; diff следующего прогона новых действий не
# предложит, потому что факт в кабинете уже совпал с планом.


def test_stale_planned_finds_rows_stuck_in_intermediate_status():
    sql = " ".join(writer_db.STALE_PLANNED_SQL.split())
    assert "status = 'planned'" in sql
    assert "created_at < now() - make_interval(mins => %s)" in sql


def test_stale_planned_passes_threshold_and_account(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        writer_db, "_fetch",
        lambda sql, params=(): captured.update(sql=sql, params=params) or [],
    )

    writer_db.stale_planned(60, account="acc-1")

    assert captured["params"] == (60, "acc-1", "acc-1")
    # Без кабинета — все зависшие строки, фильтр отключается значением NULL.
    writer_db.stale_planned(30)
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


def test_spent_risk_charges_every_live_status(monkeypatch):
    # Зависшее изменение живо в кабинете и обязано занимать риск-бюджет:
    # иначе прогон выдаёт лимит, которого нет.
    sql, _ = _captured_sql(monkeypatch, lambda: writer_db.spent_risk("2026-08-01"))

    expected = ", ".join("'%s'" % x for x in writer_db.LIVE_STATUSES)
    assert "status IN (%s)" % expected in sql


def test_mark_stale_sql_marks_only_rows_past_threshold():
    sql = " ".join(writer_db.MARK_STALE_SQL.split())
    assert sql.startswith("UPDATE edu_agent_actions")
    assert "SET status = 'stale'" in sql
    assert "WHERE status = 'planned'" in sql
    assert "created_at < now() - make_interval(mins => %s)" in sql
    # applied_at = момент ОТПРАВКИ, не момент обнаружения: иначе изменение
    # прошлой недели съело бы риск-бюджет текущей.
    assert "applied_at = COALESCE(applied_at, created_at)" in sql
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


def _journal_row(key: str, account: str, **over):
    row = {
        "idempotency_key": key,
        "account": account,
        "object_level": "campaign",
        "object_id": "111",
        "action_kind": "bidmodifier.set",
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
    ensure_writer_tables()
    suffix = uuid.uuid4().hex[:8]
    stuck = "test-stuck-" + suffix
    fresh = "test-fresh-" + suffix
    account = "test-" + suffix
    try:
        writer_db.insert_action(_journal_row(stuck, account, object_id="111"))
        writer_db.insert_action(_journal_row(fresh, account, object_id="222"))
        _backdate(stuck, minutes=90)

        found = writer_db.stale_planned(60, account=account)

        assert [r["idempotency_key"] for r in found] == [stuck]
        assert found[0]["object_id"] == "111"
        # Фильтр по кабинету работает: чужие зависшие строки в выборку не лезут.
        assert writer_db.stale_planned(60, account=account + "-other") == []
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
