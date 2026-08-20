# -*- coding: utf-8 -*-
"""
sync/agent/writer/db.py — таблицы движка записи и доступ к ним.

Журнал действий — не логи, а рабочий механизм: из него берётся предыдущее
состояние для отката и ключ идемпотентности, чтобы повторный прогон не
отправил тот же запрос дважды.
"""

import hashlib
import json
from typing import Any, Dict, List, Optional

import psycopg2.extras

from sync.db import get_connection

WRITER_DDL: List[str] = [
    # Журнал действий: что собирались сделать, что было до, что ответил API.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_actions (
      action_id        TEXT PRIMARY KEY,
      idempotency_key  TEXT NOT NULL UNIQUE,
      created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
      applied_at       TIMESTAMPTZ,
      account          TEXT NOT NULL,
      object_level     TEXT NOT NULL,
      object_id        TEXT NOT NULL,
      action_kind      TEXT NOT NULL,
      payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
      previous_state   JSONB NOT NULL DEFAULT '{}'::jsonb,
      red_line         JSONB NOT NULL DEFAULT '{}'::jsonb,
      risk_rub         DOUBLE PRECISION NOT NULL DEFAULT 0,
      status           TEXT NOT NULL DEFAULT 'planned',
      response         JSONB NOT NULL DEFAULT '{}'::jsonb,
      rolled_back_at   TIMESTAMPTZ
    )
    """,
    # Недельный риск-бюджет: сколько денег под непроверенными изменениями.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_risk_budget (
      week_start   DATE PRIMARY KEY,
      limit_rub    DOUBLE PRECISION NOT NULL,
      note         TEXT
    )
    """,
]


# Статусы, при которых строка журнала ЗАКРЫТА для повторной планировки и
# повторной отправки. Один кортеж на три места (INSERT_ACTION_SQL, apply.py,
# читатели журнала) — потому что расхождение между «кого не переписываем» и
# «кого не отправляем второй раз» и есть тот самый класс ошибки, когда два
# куска по отдельности корректны, а вместе не сходятся.
#
#   applied      — изменение совершилось, previous_state описывает точку возврата;
#   rolled_back  — изменение уже отменено;
#   stale        — запрос ушёл в кабинет, а результат не отмечен (обрыв процесса
#                  между отправкой и отметкой). Считаем применённым, пока не
#                  доказано обратное: повторная отправка bidmodifiers.add
#                  создала бы в кабинете второй объект, а перезапись
#                  previous_state стёрла бы единственное основание для отката.
FINAL_STATUSES = ("applied", "rolled_back", "stale")

# Статусы ЖИВОГО изменения в кабинете: наблюдаются сторожем красных линий и
# оплачены риск-бюджетом. 'rolled_back' сюда не входит — изменения больше нет.
LIVE_STATUSES = ("applied", "stale")


def _sql_literals(values) -> str:
    """Кортеж статусов → список литералов для IN (...). Значения — константы
    модуля, не пользовательский ввод, поэтому подстановка в текст безопасна и
    даёт то, ради чего затевалась: SQL не может разойтись с FINAL_STATUSES."""
    return ", ".join("'%s'" % v for v in values)


def ensure_writer_tables() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            for statement in WRITER_DDL:
                cur.execute(statement)
        conn.commit()


def make_action_id(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]


def _fetch(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def find_action_by_key(idempotency_key: str) -> Optional[Dict[str, Any]]:
    rows = _fetch(
        "SELECT * FROM edu_agent_actions WHERE idempotency_key = %s", (idempotency_key,)
    )
    return rows[0] if rows else None


# Повторная планировка ещё не применённого действия обязана НЕСТИ СВЕЖИЕ ДАННЫЕ.
# Со старым ON CONFLICT DO NOTHING сценарий был такой: первый прогон сохранил
# прошлое состояние и упал на отправке; человек поправил значение руками; второй
# прогон прочитал свежий факт из API, но идемпотентный ключ тот же — и в журнале
# НАВСЕГДА оставалось прошлое состояние первого прогона. Откат вернул бы кабинет
# не туда, откуда агент его вывел. То же с оценкой риска и красной линией.
#
# Условие в DO UPDATE — граница между «ещё не сделано» и «сделано»: строку в
# закрытом статусе (FINAL_STATUSES) не трогаем никогда, её previous_state
# описывает реально совершённое изменение и является единственным основанием
# для отката.
# status/response сбрасываются: прошлая ошибка не должна выглядеть ответом на
# новую попытку, а created_at обновляется, чтобы «зависшая» строка считалась от
# последней попытки (stale_planned ниже).
INSERT_ACTION_SQL = """
    INSERT INTO edu_agent_actions (
        action_id, idempotency_key, account, object_level, object_id,
        action_kind, payload, previous_state, red_line, risk_rub, status
    ) VALUES (
        %(action_id)s, %(idempotency_key)s, %(account)s, %(object_level)s,
        %(object_id)s, %(action_kind)s, %(payload)s, %(previous_state)s,
        %(red_line)s, %(risk_rub)s, 'planned'
    )
    ON CONFLICT (idempotency_key) DO UPDATE SET
        account        = EXCLUDED.account,
        object_level   = EXCLUDED.object_level,
        object_id      = EXCLUDED.object_id,
        action_kind    = EXCLUDED.action_kind,
        payload        = EXCLUDED.payload,
        previous_state = EXCLUDED.previous_state,
        red_line       = EXCLUDED.red_line,
        risk_rub       = EXCLUDED.risk_rub,
        status         = 'planned',
        response       = '{}'::jsonb,
        created_at     = now()
    WHERE edu_agent_actions.status NOT IN (__FINAL_STATUSES__)
""".replace("__FINAL_STATUSES__", _sql_literals(FINAL_STATUSES))


def insert_action(row: Dict[str, Any]) -> str:
    """Пишет действие в статусе planned.

    Повторный вызов с тем же ключом обновляет ещё не применённую строку
    свежими данными (previous_state, red_line, risk_rub) и возвращает тот же
    action_id. Уже применённую или откатанную строку не трогает — см.
    INSERT_ACTION_SQL.
    """
    action_id = make_action_id(row["idempotency_key"])
    sql = INSERT_ACTION_SQL
    params = {
        **row,
        "action_id": action_id,
        "payload": json.dumps(row.get("payload", {}), ensure_ascii=False),
        "previous_state": json.dumps(row.get("previous_state", {}), ensure_ascii=False),
        "red_line": json.dumps(row.get("red_line", {}), ensure_ascii=False),
    }
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    return action_id


def mark_action(action_id: str, status: str, response: Dict[str, Any]) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE edu_agent_actions
                   SET status = %s,
                       response = %s,
                       applied_at = CASE WHEN %s = 'applied' THEN now() ELSE applied_at END
                 WHERE action_id = %s
                """,
                (status, json.dumps(response, ensure_ascii=False), status, action_id),
            )
        conn.commit()


def open_actions() -> List[Dict[str, Any]]:
    """Живые изменения в кабинете — за ними следит сторож красных линий.

    Два статуса, а не один. 'applied' — изменение точно состоялось. 'stale' —
    запрос ушёл, а результат не отмечен (обрыв процесса), и по умолчанию мы
    считаем изменение состоявшимся: пока обратное не доказано, оно живое,
    и оставлять его без наблюдения нельзя. Именно из-за прежнего фильтра
    только по 'applied' зависшая строка не попадала в поле зрения ни сторожа,
    ни отката — и вечно печаталась в отчёте, ничем больше не отзываясь.
    """
    return _fetch(
        "SELECT * FROM edu_agent_actions "
        "WHERE status IN (__LIVE_STATUSES__) AND rolled_back_at IS NULL".replace(
            "__LIVE_STATUSES__", _sql_literals(LIVE_STATUSES))
    )


# Строка, застрявшая в промежуточном статусе, — след обрыва ПОСЛЕ отправки
# запроса. Порядок «журнал → отправка» защищает от потери следа, но не от
# смерти процесса между уходом запроса и отметкой результата: изменение в
# кабинете состоялось, а строка осталась planned — без ответа и без Id
# созданного объекта. Такую строку не видит ни сторож применённых действий
# (open_actions смотрит только applied), ни откат; риск-бюджет не списан;
# diff следующего прогона новых действий не предложит, потому что факт в
# кабинете уже совпал с планом. Расхождение не всплывает нигде — поэтому
# ищем его явно и показываем в отчёте прогона.
STALE_PLANNED_SQL = """
    SELECT action_id, idempotency_key, account, object_level, object_id,
           action_kind, created_at
    FROM edu_agent_actions
    WHERE status = 'planned'
      AND created_at < now() - make_interval(mins => %s)
      AND (%s IS NULL OR account = %s)
    ORDER BY created_at
"""


def stale_planned(older_than_minutes: int, account: Optional[str] = None) -> List[Dict[str, Any]]:
    """Действия, застрявшие в статусе planned дольше разумного времени.

    Порог в минутах, а не в днях: прогон живёт минуты, и всё, что старше
    порога, к текущему прогону отношения не имеет — это след прошлого обрыва,
    который надо разобрать руками (сверить кабинет с журналом), а не автоматом:
    достоверно отличить «запрос ушёл и применился» от «запрос не ушёл» по
    самой строке невозможно.

    Только чтение: перевод строки в статус 'stale' делает mark_stale_planned.
    """
    return _fetch(STALE_PLANNED_SQL, (int(older_than_minutes), account, account))


# Перевод зависшей строки в терминальный статус 'stale' — единственным
# UPDATE ... RETURNING, то есть атомарно: обнаружение и пометка не могут
# разъехаться между двумя прогонами (и двумя параллельными прогонами тоже —
# строку заберёт ровно один).
#
# Что даёт статус, кроме печати в отчёте (ради чего он и заведён):
#   1. Отчёт перестаёт повторять одно и то же вечно — печатаются только
#      строки, обнаруженные ВПЕРВЫЕ, то есть возвращённые этим запросом.
#   2. Строка попадает в open_actions → её увидит будущий сторож отката.
#   3. Риск списывается: applied_at проставляется моментом ОТПРАВКИ запроса
#      (created_at), а не моментом обнаружения, — иначе изменение прошлой
#      недели съело бы бюджет текущей.
#   4. Повторная отправка исключена: 'stale' входит в FINAL_STATUSES, а
#      значит и apply.py пропустит действие, и INSERT не перезапишет его
#      previous_state.
# Ответ помечается флагом, а не выдумывается: результат запроса неизвестен,
# и строка всё равно требует ручной сверки кабинета с журналом.
MARK_STALE_SQL = """
    UPDATE edu_agent_actions
       SET status     = 'stale',
           applied_at = COALESCE(applied_at, created_at),
           response   = jsonb_build_object(
                            'stale', true,
                            'detected_at', now(),
                            'note', 'обрыв после отправки: результат неизвестен, '
                                    'нужна ручная сверка кабинета с журналом')
     WHERE status = 'planned'
       AND created_at < now() - make_interval(mins => %s)
       AND (%s IS NULL OR account = %s)
    RETURNING action_id, idempotency_key, account, object_level, object_id,
              action_kind, created_at, risk_rub
"""


def mark_stale_planned(
    older_than_minutes: int, account: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Помечает зависшие planned-строки статусом 'stale' и отдаёт ТОЛЬКО те,
    что помечены этим вызовом, — то есть обнаруженные впервые.

    Повторный вызов вернёт пустой список: строка уже не в статусе 'planned'.
    Ровно этим отчёт прогона перестаёт бесконечно повторять одну и ту же
    находку, а сама находка не теряется — она видна через open_actions.
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(MARK_STALE_SQL, (int(older_than_minutes), account, account))
            rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
    return rows


def mark_rolled_back(action_id: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE edu_agent_actions SET rolled_back_at = now(), status = 'rolled_back' "
                "WHERE action_id = %s",
                (action_id,),
            )
        conn.commit()


def spent_risk(week_start: str) -> float:
    """Деньги под живыми непроверенными изменениями недели.

    Зависшая строка ('stale') считается наравне с применённой: запрос ушёл в
    кабинет, изменение с высокой вероятностью живое, и не списывать за него
    риск значит выдавать бюджет, которого нет. Неделя берётся по applied_at,
    который для такой строки равен моменту отправки (см. MARK_STALE_SQL).
    """
    rows = _fetch(
        """
        SELECT COALESCE(SUM(risk_rub), 0) AS spent
        FROM edu_agent_actions
        WHERE status IN (__LIVE_STATUSES__)
          AND rolled_back_at IS NULL
          AND applied_at >= %s
        """.replace("__LIVE_STATUSES__", _sql_literals(LIVE_STATUSES)),
        (week_start,),
    )
    return float(rows[0]["spent"]) if rows else 0.0


def risk_limit(week_start: str, default_rub: float) -> float:
    rows = _fetch(
        "SELECT limit_rub FROM edu_agent_risk_budget WHERE week_start = %s", (week_start,)
    )
    return float(rows[0]["limit_rub"]) if rows else default_rub
