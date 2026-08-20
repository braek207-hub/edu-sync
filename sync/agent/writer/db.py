# -*- coding: utf-8 -*-
"""
sync/agent/writer/db.py — таблицы движка записи и доступ к ним.

Журнал действий — не логи, а рабочий механизм: из него берётся предыдущее
состояние для отката и ключ идемпотентности, чтобы повторный прогон не
отправил тот же запрос дважды.
"""

import hashlib
import json
import os
import socket
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

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
    # Неудавшийся откат. Колонки добавляются отдельным ALTER, а не правкой
    # CREATE TABLE выше: таблица уже существует в базе, и CREATE TABLE IF NOT
    # EXISTS её не трогает — новая колонка в теле CREATE появилась бы только на
    # чистой базе, а на рабочей молча отсутствовала бы.
    #
    #   rollback_attempts  — сколько раз откат пытались отправить. Ограничитель
    #                        повторов: сетевой сбой стоит повторить, но не вечно.
    #   rollback_failed_at — откат признан неисполнимым автоматически. Строка
    #                        снимается с наблюдения (open_actions), потому что
    #                        каждый следующий прогон повторял бы ровно тот же
    #                        обречённый запрос. Изменение при этом ОСТАЁТСЯ
    #                        живым: статус не меняется, rolled_back_at пуст,
    #                        риск-бюджет продолжает его оплачивать — деньги под
    #                        неоткатанным изменением никуда не делись.
    #   rollback_error     — причина последней неудачи, для отчёта и разбора.
    """
    ALTER TABLE edu_agent_actions
      ADD COLUMN IF NOT EXISTS rollback_attempts  INTEGER NOT NULL DEFAULT 0,
      ADD COLUMN IF NOT EXISTS rollback_failed_at TIMESTAMPTZ,
      ADD COLUMN IF NOT EXISTS rollback_error     TEXT
    """,
    # СЕГМЕНТ действия отдельными колонками, а не только внутри payload.
    # Без них история откатов неадресуема: payload действия bidmodifier.set
    # несёт лишь Id корректировки и коэффициент — ни вида, ни ключа сегмента
    # там нет вообще, и «этот сегмент этой кампании признан вредным» не
    # выводится из журнала ничем. Ключ идемпотентности тоже не помогает: в нём
    # зашит ПРОЦЕНТ, а он дрейфует между расчётами, поэтому вчерашний откат
    # завтрашнему действию по тому же сегменту не соответствует.
    #
    #   direct_type — тип корректировки Директа (MOBILE_ADJUSTMENT, ...);
    #   setting_key — ключ сегмента внутри типа (MOBILE, GENDER_MALE, 213).
    """
    ALTER TABLE edu_agent_actions
      ADD COLUMN IF NOT EXISTS direct_type TEXT,
      ADD COLUMN IF NOT EXISTS setting_key TEXT
    """,
    # ВЕРДИКТ СТОРОЖА, отдельно от успешности технической операции возврата.
    #
    # Кулдаун планирования (agent_e1.COOLDOWN_AFTER_ROLLBACK_DAYS) смотрел
    # только на rolled_back_at — то есть на то, УДАЛСЯ ли откат. А вредным
    # сегмент делает не удавшийся возврат, а пробитая красная линия: если
    # линия пробита, а откатить не смогли (Id неизвестен, рельсы отклонили,
    # кабинет не отвечает), изменение снимается с наблюдения — и планировщик
    # об этом не знает. Процент дрейфует на пункт между расчётами, ключ
    # идемпотентности получается новый, и агент назавтра крутит тот же
    # сегмент той же кампании ещё раз.
    #
    #   harmful_verdict_at — момент, когда сторож признал изменение вредным;
    #   harmful_reason     — формулировка пробоя, для отчёта и разбора.
    """
    ALTER TABLE edu_agent_actions
      ADD COLUMN IF NOT EXISTS harmful_verdict_at TIMESTAMPTZ,
      ADD COLUMN IF NOT EXISTS harmful_reason     TEXT
    """,
    # ЧИСЛО ПОПЫТОК ПРИМЕНЕНИЯ, парное rollback_attempts.
    #
    # Статус 'rejected' сознательно не финальный: API вернул 200 и отклонил
    # элемент, в кабинете ничего не изменилось, и действие обязано быть
    # переприменимо. Но потолка попыток у этого пути не было вовсе — в
    # отличие от отката, где он есть с самого начала. Детерминированный отказ
    # (неподдерживаемый ключ сегмента, неподходящий тип кампании, «объект уже
    # существует») переотправлялся бы КАЖДЫЙ прогон бесконечно, каждый раз
    # занимая слот лимита действий и часть риск-бюджета. Это ровно та
    # болезнь, ради которой введён отсев уже закрытых ключей
    # (final_status_keys), воспроизведённая через другой статус.
    #
    # Счётчик растёт на статусах 'rejected' и 'failed' (см. MARK_ACTION_SQL) и
    # НЕ сбрасывается повторной планировкой: INSERT_ACTION_SQL его не трогает.
    """
    ALTER TABLE edu_agent_actions
      ADD COLUMN IF NOT EXISTS apply_attempts INTEGER NOT NULL DEFAULT 0
    """,
    # Потолок попыток читается на каждом прогоне по сегменту среди строк,
    # оставшихся в отказных статусах, — тот же довод про seq scan, что у
    # индексов кулдауна.
    """
    CREATE INDEX IF NOT EXISTS edu_agent_actions_apply_attempts_idx
      ON edu_agent_actions (account, object_id, direct_type, setting_key)
      WHERE apply_attempts > 0
    """,
    # Кулдаун по сегменту читается на каждом прогоне по (кабинет, объект,
    # тип, ключ) среди вредных строк — без индексов это seq scan по всему
    # журналу на каждый прогон. Индекса два, потому что вредность даёт любая
    # из двух отметок.
    """
    CREATE INDEX IF NOT EXISTS edu_agent_actions_rolled_back_idx
      ON edu_agent_actions (rolled_back_at)
      WHERE rolled_back_at IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS edu_agent_actions_harmful_idx
      ON edu_agent_actions (harmful_verdict_at)
      WHERE harmful_verdict_at IS NOT NULL
    """,
    # Аренда на прогон: два одновременных прогона на одном ключе создают в
    # кабинете ДВА объекта, и Id первого теряется навсегда — без строки
    # журнала и без красной линии. Таблица, а не pg_advisory_lock: сессионная
    # advisory-блокировка не переживает пулер в транзакционном режиме
    # (sync/db.py::_database_url специально снимает pgbouncer=true из URI —
    # значит через пулер сюда ходят), а аренда со сроком годности работает
    # одинаково при любом способе подключения.
    #
    #   holder     — кто держит: uuid прогона + pid + хост, чтобы снять аренду
    #                мог только он сам, а не случайный следующий прогон;
    #   expires_at — срок годности. Прогон, умерший вместе с процессом, не
    #                оставляет вечную блокировку: аренда просто протухает.
    """
    CREATE TABLE IF NOT EXISTS edu_agent_run_lock (
      lock_name    TEXT PRIMARY KEY,
      holder       TEXT NOT NULL,
      acquired_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
      expires_at   TIMESTAMPTZ NOT NULL
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

# Статусы, из которых действие уходит на ПОВТОРНУЮ отправку следующим
# прогоном. В кабинете по ним ничего не изменилось, поэтому они не финальные,
# — но именно они и копят попытки: отказ бывает детерминированным, и тогда
# повтор не поможет никогда.
#
#   rejected — API ответил 200 и отклонил элемент (Errors в AddResults/
#              SetResults): неподдерживаемый ключ сегмента, неподходящий тип
#              кампании, «объект уже существует»;
#   failed   — запрос точно не ушёл (ошибка сборки тела, отказ уровня запроса).
REATTEMPTED_STATUSES = ("rejected", "failed")

# Сколько прогонов подряд один СЕГМЕНТ вправе получать отказ, прежде чем
# движок перестанет его переотправлять. Столько же, сколько у отката
# (MAX_ROLLBACK_ATTEMPTS), и по той же причине: разовый сбой стоит повторить,
# детерминированный — нет.
#
# По сегменту, а не по ключу идемпотентности: в ключ зашит ПРОЦЕНТ, а он
# пересчитывается на каждом прогоне по скользящему окну и дрейфует на пункт-
# другой — то есть у каждой попытки свой ключ, и счётчик на ключе показывал бы
# единицу вечно. Тот же довод, по которому кулдаун после отката считается по
# сегменту (harmful_segments).
MAX_APPLY_ATTEMPTS = 3


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
        action_kind, direct_type, setting_key, payload, previous_state,
        red_line, risk_rub, status
    ) VALUES (
        %(action_id)s, %(idempotency_key)s, %(account)s, %(object_level)s,
        %(object_id)s, %(action_kind)s, %(direct_type)s, %(setting_key)s,
        %(payload)s, %(previous_state)s, %(red_line)s, %(risk_rub)s, 'planned'
    )
    ON CONFLICT (idempotency_key) DO UPDATE SET
        account        = EXCLUDED.account,
        object_level   = EXCLUDED.object_level,
        object_id      = EXCLUDED.object_id,
        action_kind    = EXCLUDED.action_kind,
        direct_type    = EXCLUDED.direct_type,
        setting_key    = EXCLUDED.setting_key,
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
        # Сегмент — в собственные колонки. В действии он лежит на верхнем
        # уровне (diff.py кладёт direct_type/key рядом с payload); ключ
        # называется "key", потому что это ключ настройки, а не действия.
        "direct_type": row.get("direct_type"),
        "setting_key": None if row.get("key") is None else str(row.get("key")),
        "payload": json.dumps(row.get("payload", {}), ensure_ascii=False),
        "previous_state": json.dumps(row.get("previous_state", {}), ensure_ascii=False),
        "red_line": json.dumps(row.get("red_line", {}), ensure_ascii=False),
    }
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    return action_id


# Отметка результата — единственный запрос модуля, который раньше стоял БЕЗ
# условия на текущее состояние строки. Достижимое состояние было такое: статус
# 'applied' И проставленный rolled_back_at одновременно. Путь: apply.py увидел
# нефинальный статус, отправил запрос, а пока он летел, сторож перевёл строку в
# 'stale' и откатил её; безусловный UPDATE затирал откат обратно в 'applied'.
#
# Такая строка исчезала из ВСЕХ трёх контуров сразу: наблюдение её не видит
# (open_actions фильтрует rolled_back_at IS NULL), риск-бюджет не считает
# (spent_risk фильтрует так же), переприменить нельзя (статус финальный).
#
# Гард ровно про это: строку с проставленным откатом или в финальном статусе
# не трогаем. Возврат RETURNING отличает «отметили» от «строку забрал кто-то
# другой» — вызывающий код обязан это увидеть, а не считать отметку успешной.
#
# Счётчик попыток применения растёт ровно на отказных статусах
# (REATTEMPTED_STATUSES) и тем же UPDATE, что ставит статус: попытка и её
# исход не могут разъехаться между двумя прогонами — тот же приём, что у
# MARK_ROLLBACK_FAILED_SQL. На 'applied' и 'dry_run' счётчик не двигается.
MARK_ACTION_SQL = """
    UPDATE edu_agent_actions
       SET status = %(status)s,
           response = %(response)s,
           applied_at = CASE WHEN %(status)s = 'applied' THEN now() ELSE applied_at END,
           apply_attempts = apply_attempts + CASE
               WHEN %(status)s IN (__REATTEMPTED_STATUSES__) THEN 1 ELSE 0
           END
     WHERE action_id = %(action_id)s
       AND rolled_back_at IS NULL
       AND status NOT IN (__FINAL_STATUSES__)
    RETURNING action_id
""".replace("__FINAL_STATUSES__", _sql_literals(FINAL_STATUSES)) \
   .replace("__REATTEMPTED_STATUSES__", _sql_literals(REATTEMPTED_STATUSES))


def mark_action(action_id: str, status: str, response: Dict[str, Any]) -> bool:
    """Отмечает исход отправки. True — отметка легла, False — строку уже
    забрал другой контур (откат сторожа или пометка зависшей)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MARK_ACTION_SQL, {
                "status": status,
                "response": json.dumps(response, ensure_ascii=False),
                "action_id": action_id,
            })
            landed = cur.fetchone() is not None
        conn.commit()
    return landed


# Исход НЕИЗВЕСТЕН: запрос ушёл, а ответа нет (таймаут на записи, разрыв после
# отправки тела, недоступность сервиса после ретраев). Это не 'failed': под
# 'failed' строка не наблюдается сторожем, риск по ней не списан, а diff
# следующего прогона её не предложит — фактическое состояние кабинета уже
# совпало с планом. Изменение исчезает из виду навсегда.
#
# Поэтому такой исход попадает в тот же контур, что и обрыв процесса, — статус
# 'stale' с applied_at на момент отправки: живое непроверенное изменение под
# наблюдением и под риск-бюджетом, повторно не отправляемое.
# Гард тот же, что у MARK_ACTION_SQL, и по той же причине.
MARK_UNKNOWN_OUTCOME_SQL = """
    UPDATE edu_agent_actions
       SET status     = 'stale',
           applied_at = COALESCE(applied_at, created_at, now()),
           response   = %(response)s
     WHERE action_id = %(action_id)s
       AND rolled_back_at IS NULL
       AND status NOT IN (__FINAL_STATUSES__)
    RETURNING action_id
""".replace("__FINAL_STATUSES__", _sql_literals(FINAL_STATUSES))


def mark_unknown_outcome(action_id: str, reason: str) -> bool:
    """Переводит действие с неизвестным исходом в 'stale'.

    True — отметка легла, False — строку уже забрал другой контур.
    """
    response = {
        "unknown_outcome": True,
        "error": str(reason)[:400],
        "note": ("исход отправки неизвестен: запрос мог примениться в кабинете. "
                 "Строка считается живым непроверенным изменением до ручной сверки"),
    }
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MARK_UNKNOWN_OUTCOME_SQL, {
                "response": json.dumps(response, ensure_ascii=False),
                "action_id": action_id,
            })
            landed = cur.fetchone() is not None
        conn.commit()
    return landed


def open_actions() -> List[Dict[str, Any]]:
    """Живые изменения в кабинете — за ними следит сторож красных линий.

    Два статуса, а не один. 'applied' — изменение точно состоялось. 'stale' —
    запрос ушёл, а результат не отмечен (обрыв процесса), и по умолчанию мы
    считаем изменение состоявшимся: пока обратное не доказано, оно живое,
    и оставлять его без наблюдения нельзя. Именно из-за прежнего фильтра
    только по 'applied' зависшая строка не попадала в поле зрения ни сторожа,
    ни отката — и вечно печаталась в отчёте, ничем больше не отзываясь.

    Строки с проставленным rollback_failed_at исключены: их откат уже признан
    неисполнимым автоматически (неизвестен Id, рельсы отклонили запрос на
    возврат, исчерпаны попытки отправки). Держать их в наблюдении значило бы
    отправлять один и тот же обречённый запрос каждый прогон. Потерянными они
    не становятся: их считает failed_rollbacks_count и разбирает человек.
    """
    return _fetch(
        "SELECT * FROM edu_agent_actions "
        "WHERE status IN (__LIVE_STATUSES__) AND rolled_back_at IS NULL "
        "AND rollback_failed_at IS NULL".replace(
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


# Отметка «откатано» — единственный переход состояния, остававшийся без гарда:
# UPDATE по одному только action_id, без условия на текущее состояние и без
# проверки, что строка действительно изменилась. Соседние переходы
# (MARK_ACTION_SQL, MARK_UNKNOWN_OUTCOME_SQL, MARK_STALE_SQL) давно приведены
# к безопасной форме, и по той же причине: строку между решением и отметкой
# мог забрать другой контур.
#
# Условие — «строка ещё живая»: статус из LIVE_STATUSES и пустой
# rolled_back_at. Что это отсекает:
#   * повторный откат уже откатанной строки (второй сторож, ретрай отчёта) —
#     rolled_back_at не сдвигается, и кулдаун сегмента не продлевается задним
#     числом от чужой попытки;
#   * пометку строки, которую параллельный контур уже перевёл в 'planned'
#     повторной планировкой, — иначе в журнале появилось бы 'rolled_back' у
#     действия, которого в кабинете ещё не было.
MARK_ROLLED_BACK_SQL = """
    UPDATE edu_agent_actions
       SET rolled_back_at = now(),
           status         = 'rolled_back'
     WHERE action_id = %(action_id)s
       AND rolled_back_at IS NULL
       AND status IN (__LIVE_STATUSES__)
    RETURNING action_id
""".replace("__LIVE_STATUSES__", _sql_literals(LIVE_STATUSES))


def mark_rolled_back(action_id: str) -> bool:
    """True — отметка легла. False — строку уже забрал другой контур.

    Возврат обязателен: вызывающий код (сторож) должен различать «отметили» и
    «строка ушла из-под нас», иначе отчёт утверждает про журнал то, чего в нём
    нет.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MARK_ROLLED_BACK_SQL, {"action_id": action_id})
            updated = cur.fetchone() is not None
        conn.commit()
    return updated


# Вердикт сторожа «изменение вредно» — отдельно от того, удался ли возврат.
# Гард тот же и по той же причине, что у соседей; повторная пометка живой
# строки не сдвигает момент вердикта (COALESCE), чтобы кулдаун сегмента
# отсчитывался от ПЕРВОГО пробоя, а не продлевался каждым прогоном сторожа.
MARK_HARMFUL_SQL = """
    UPDATE edu_agent_actions
       SET harmful_verdict_at = COALESCE(harmful_verdict_at, now()),
           harmful_reason     = %(reason)s
     WHERE action_id = %(action_id)s
       AND rolled_back_at IS NULL
       AND status IN (__LIVE_STATUSES__)
    RETURNING action_id, harmful_verdict_at
""".replace("__LIVE_STATUSES__", _sql_literals(LIVE_STATUSES))


def mark_harmful_verdict(action_id: str, reason: str) -> bool:
    """Фиксирует вердикт сторожа: красная линия пробита.

    Ставится ДО попытки отката и независимо от её исхода — потому что
    признак вредности сегмента это вердикт, а не успешность технической
    операции возврата. Из этой отметки планировщик берёт кулдаун
    (harmful_segments), и без неё неоткатанный пробой был бы для него
    невидим.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MARK_HARMFUL_SQL, {"action_id": action_id,
                                           "reason": str(reason)[:500]})
            updated = cur.fetchone() is not None
        conn.commit()
    return updated


# Сколько раз откат одного действия вообще стоит пытаться отправить.
# Сбой отправки бывает разовым (сеть, 5xx после ретраев клиента) — такой
# повтор на следующем прогоне осмыслен. Но повторять бесконечно нельзя:
# детерминированный отказ (кампании больше нет, объект удалён руками) иначе
# уходил бы в кабинет каждый час вечно и вечно же занимал бы отчёт.
MAX_ROLLBACK_ATTEMPTS = 3

# Попытка и её исход — одним UPDATE ... RETURNING: счётчик попыток и решение
# «снимать ли с повторов» не могут разъехаться между двумя прогонами.
#
# permanent=True приходит для отказов, которые повтор не исправит в принципе:
# неизвестен Id корректировки (откат вслепую) или запрос на возврат отклонён
# рельсами. Ждать три попытки в этом случае бессмысленно — исход тот же.
#
# Гард на состояние строки — последний из переходов модуля, который его не
# имел. Условие то же, что у MARK_ROLLBACK_FAILED_SQL-соседей (MARK_ACTION_SQL,
# MARK_HARMFUL_SQL, MARK_ROLLED_BACK_SQL): строка ещё живая и не откатана.
# Что это отсекает: строку, которую параллельный сторож успел откатить между
# решением и отметкой, — без гарда её счётчик попыток рос бы у УЖЕ ОТКАТАННОГО
# изменения, и следующая (настоящая) неудача по этой строке хоронила бы её
# раньше срока. Возврат None вызывающий код (agent_e1_watchdog._fail) уже
# трактует как «строку забрал другой контур».
MARK_ROLLBACK_FAILED_SQL = """
    UPDATE edu_agent_actions
       SET rollback_attempts  = rollback_attempts + 1,
           rollback_error     = %(reason)s,
           rollback_failed_at = CASE
               WHEN %(permanent)s OR rollback_attempts + 1 >= %(max_attempts)s
                   THEN COALESCE(rollback_failed_at, now())
               ELSE rollback_failed_at
           END
     WHERE action_id = %(action_id)s
       AND rolled_back_at IS NULL
       AND status IN (__LIVE_STATUSES__)
    RETURNING action_id, object_id, action_kind, rollback_attempts,
              rollback_failed_at, rollback_error
""".replace("__LIVE_STATUSES__", _sql_literals(LIVE_STATUSES))


def mark_rollback_failed(
    action_id: str, reason: str, permanent: bool = False
) -> Optional[Dict[str, Any]]:
    """Фиксирует неудачную попытку отката и возвращает новое состояние строки.

    Изменение остаётся живым: статус не трогается, rolled_back_at не ставится.
    Проставленный в ответе rollback_failed_at означает, что автоматических
    попыток больше не будет и строку разбирает человек.

    None — строку забрал другой контур (её уже откатили или она вернулась в
    планирование): попытка не засчитывается, счётчик не растёт.
    """
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(MARK_ROLLBACK_FAILED_SQL, {
                "action_id": action_id,
                "reason": reason[:500],
                "permanent": bool(permanent),
                "max_attempts": MAX_ROLLBACK_ATTEMPTS,
            })
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def failed_rollbacks_count() -> int:
    """Сколько живых изменений остались неоткатанными и ждут человека.

    Число, а не список: отчёт читается глазами каждый прогон, и растущий
    перечень одних и тех же строк перестаёт читаться (тот же довод, что у
    mark_stale_planned). Список достаётся из журнала по rollback_failed_at.
    """
    rows = _fetch(
        """
        SELECT COUNT(*) AS n FROM edu_agent_actions
        WHERE rollback_failed_at IS NOT NULL AND rolled_back_at IS NULL
        """
    )
    return int(rows[0]["n"]) if rows else 0


# --------------------------------------------- срок хранения строк репетиции

# Сколько дней живут собственные строки репетиционного прогона (статус
# 'dry_run'). У них не было судьбы вообще: статус не финальный (журнал их
# перезапишет повторной планировкой) и не живой (риск-бюджет их не считает,
# сторож не наблюдает) — то есть ни один механизм их не закрывает, а ключ
# репетиции с новым процентом каждый раз новый, поэтому строки копились вечно.
#
# Судьба — срок хранения. Удалять их безопасно ровно потому, что за ними
# ничего нет: mutate в репетиции не уходил в кабинет, отката такая строка не
# требует, риск не оплачивает. Двух недель хватает, чтобы прочитать отчёт
# репетиции и сравнить его с журналом руками.
DRY_RUN_RETENTION_DAYS = 14

PURGE_DRY_RUN_SQL = """
    DELETE FROM edu_agent_actions
     WHERE status = 'dry_run'
       AND rolled_back_at IS NULL
       AND created_at < now() - make_interval(days => %s)
"""


def purge_dry_run_actions(older_than_days: int = DRY_RUN_RETENTION_DAYS) -> int:
    """Убирает старые строки репетиции. Возвращает число удалённых.

    Условие бьёт только по статусу 'dry_run': ни одна строка, за которой стоит
    отправленный в кабинет запрос, под него не попадает — у таких статус
    'applied', 'stale', 'rejected', 'failed' или 'rolled_back'.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(PURGE_DRY_RUN_SQL, (int(older_than_days),))
            removed = cur.rowcount or 0
        conn.commit()
    return int(removed)


# ------------------------------------------------ обратная связь от отката

# Сегмент, по которому был откат, определяется ОБЪЕКТОМ И СЕГМЕНТОМ, а не
# ключом идемпотентности: в ключ зашит процент, а он пересчитывается на каждом
# прогоне по скользящему окну и дрейфует на пункт-другой. Применили тридцать,
# откатили, назавтра расчёт дал двадцать девять — это уже другой ключ, и
# идемпотентность цикл не ловит вообще.
#
# COALESCE с payload — для строк, записанных до появления колонок direct_type/
# setting_key: у действия bidmodifier.add сегмент лежал в payload, у
# bidmodifier.set его не было нигде. Что не восстанавливается, то в кулдаун не
# попадает, и это честнее, чем угадывать сегмент по Id корректировки.
# Вредным сегмент делает ВЕРДИКТ сторожа, а не успешность возврата. Поэтому
# в выборку входят обе отметки:
#   rolled_back_at     — линия пробита, изменение отменено;
#   harmful_verdict_at — линия пробита, откатить не удалось (Id неизвестен,
#                        рельсы отклонили, кабинет не ответил). Изменение
#                        снято с наблюдения и ждёт человека — но сегмент от
#                        этого не стал безопасным, и трогать его снова
#                        планировщику нельзя тем более.
# GREATEST в Postgres игнорирует NULL и возвращает NULL только когда NULL все
# аргументы, а NULL > interval даёт NULL — то есть строки без обеих отметок
# отсекаются самим сравнением, отдельного IS NOT NULL не нужно.
HARMFUL_SEGMENTS_SQL = """
    SELECT account,
           object_id,
           COALESCE(direct_type, payload->>'Type') AS direct_type,
           COALESCE(setting_key, payload->>'key')  AS setting_key,
           MAX(GREATEST(rolled_back_at, harmful_verdict_at)) AS last_harmful_at
    FROM edu_agent_actions
    WHERE GREATEST(rolled_back_at, harmful_verdict_at) > now() - make_interval(days => %s)
      AND (%s IS NULL OR account = %s)
      AND COALESCE(direct_type, payload->>'Type') IS NOT NULL
      AND COALESCE(setting_key, payload->>'key') IS NOT NULL
    GROUP BY 1, 2, 3, 4
"""


def harmful_segments(
    cooldown_days: int, account: Optional[str] = None
) -> Dict[Tuple[str, str, str], Any]:
    """Сегменты кампаний, признанные вредными за последние cooldown_days.

    Ключ — (object_id, direct_type, setting_key), значение — момент последнего
    вердикта. Кабинет в ключ не входит: выборка уже ограничена одним
    кабинетом, а идентификаторы кампаний между кабинетами не пересекаются.
    """
    rows = _fetch(HARMFUL_SEGMENTS_SQL, (int(cooldown_days), account, account))
    return {
        (str(r["object_id"]), str(r["direct_type"]), str(r["setting_key"])):
            r["last_harmful_at"]
        for r in rows
    }


# Сегменты, исчерпавшие попытки применения. Считается СУММА попыток по всем
# строкам сегмента, оставшимся в отказных статусах, — по той же причине, по
# которой по сегменту считается кулдаун: процент дрейфует, ключ идемпотентности
# каждый прогон новый, и у каждой строки попытка ровно одна. Сумма по сегменту
# и есть «сколько прогонов подряд мы бились в эту стену».
#
# Строки, дошедшие до 'applied'/'stale'/'rolled_back', в выборку не входят: они
# уже не отказные, и сегмент, по которому изменение однажды состоялось, стеной
# не является.
#
# COALESCE с payload — тот же, что в HARMFUL_SEGMENTS_SQL: у строк, записанных
# до появления колонок direct_type/setting_key, сегмент лежал только в payload.
EXHAUSTED_SEGMENTS_SQL = """
    SELECT account,
           object_id,
           COALESCE(direct_type, payload->>'Type') AS direct_type,
           COALESCE(setting_key, payload->>'key')  AS setting_key,
           SUM(apply_attempts) AS attempts,
           MAX(created_at)     AS last_attempt_at
    FROM edu_agent_actions
    WHERE apply_attempts > 0
      AND status IN (__REATTEMPTED_STATUSES__)
      AND (%s IS NULL OR account = %s)
      AND COALESCE(direct_type, payload->>'Type') IS NOT NULL
      AND COALESCE(setting_key, payload->>'key') IS NOT NULL
    GROUP BY 1, 2, 3, 4
    HAVING SUM(apply_attempts) >= %s
""".replace("__REATTEMPTED_STATUSES__", _sql_literals(REATTEMPTED_STATUSES))


def exhausted_segments(
    max_attempts: int = MAX_APPLY_ATTEMPTS, account: Optional[str] = None
) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """Сегменты, которым движок больше не отправляет действия.

    Ключ — (object_id, direct_type, setting_key), как у harmful_segments и по
    той же причине: кабинет в ключ не входит, выборка уже ограничена одним
    кабинетом, а идентификаторы кампаний между кабинетами не пересекаются.

    Значение — число накопленных попыток и момент последней: отчёт прогона
    обязан показывать не только «заблокировано», но и на чём именно движок
    остановился, — эти строки разбирает человек.
    """
    rows = _fetch(EXHAUSTED_SEGMENTS_SQL, (account, account, int(max_attempts)))
    return {
        (str(r["object_id"]), str(r["direct_type"]), str(r["setting_key"])):
            {"attempts": int(r["attempts"] or 0),
             "last_attempt_at": r["last_attempt_at"]}
        for r in rows
    }


def final_status_keys(keys: Iterable[str]) -> Set[str]:
    """Ключи идемпотентности, уже закрытые финальным статусом.

    Нужны ДО отбора по лимиту действий. Раньше такое действие доходило до
    apply_actions и отсеивалось только там: порядок обхода детерминирован,
    накопившиеся закрытые ключи стабильно занимали начало списка и съедали
    лимит прогона целиком — отчёт рапортовал «подготовлено пятьдесят,
    применено ноль», и ни одно новое действие в кабинет не уходило.
    """
    keys = [str(k) for k in keys]
    if not keys:
        return set()
    rows = _fetch(
        "SELECT idempotency_key FROM edu_agent_actions "
        "WHERE idempotency_key = ANY(%s) AND status IN (__FINAL_STATUSES__)".replace(
            "__FINAL_STATUSES__", _sql_literals(FINAL_STATUSES)),
        (keys,),
    )
    return {str(r["idempotency_key"]) for r in rows}


# ------------------------------------------------------- аренда на прогон

# Имя аренды → назначение. Реестр общий на оба рабочих процесса движка:
# параллельный запуск опасен у обоих, и у каждого своё имя, потому что
# прямое применение и откат друг другу не мешают.
RUN_LOCK_NAMES = ("agent_e1", "agent_e1_watchdog")

# Срок годности аренды. Прогон живёт минуты; час — тот же порог, по которому
# строка журнала признаётся зависшей (agent_e1.STALE_PLANNED_MINUTES): процесс,
# держащий аренду дольше, по тому же критерию считается мёртвым, и его аренду
# можно перехватить, иначе смерть процесса блокировала бы движок навсегда.
RUN_LOCK_TTL_MINUTES = 60

# Захват аренды — одним запросом: проверка «свободно ли» и запись владельца не
# могут разъехаться между двумя прогонами. Строка возвращается только тому, кто
# аренду действительно получил; занятая и не протухшая аренда даёт пустой ответ.
ACQUIRE_RUN_LOCK_SQL = """
    INSERT INTO edu_agent_run_lock (lock_name, holder, acquired_at, expires_at)
    VALUES (%(name)s, %(holder)s, now(), now() + make_interval(mins => %(ttl)s))
    ON CONFLICT (lock_name) DO UPDATE SET
        holder      = EXCLUDED.holder,
        acquired_at = now(),
        expires_at  = EXCLUDED.expires_at
    WHERE edu_agent_run_lock.expires_at < now()
    RETURNING lock_name, holder, expires_at
"""


# Продление аренды. Условие «мы всё ещё владелец И аренда не протухла» — то
# же самое, по которому её мог бы перехватить чужой прогон: пустой ответ
# означает «аренда уже не наша», и продлевать её нельзя ни в коем случае —
# иначе прогон отобрал бы у живого владельца то, что тот успел взять.
RENEW_RUN_LOCK_SQL = """
    UPDATE edu_agent_run_lock
       SET expires_at = now() + make_interval(mins => %(ttl)s)
     WHERE lock_name = %(name)s
       AND holder    = %(holder)s
       AND expires_at > now()
    RETURNING expires_at
"""


class RunLockBusy(RuntimeError):
    """Прогон уже идёт. Второй одновременный прогон на том же ключе создал бы
    в кабинете второй объект, а Id первого потерялся бы навсегда."""


class RunLeaseLost(RuntimeError):
    """Аренда прогона перестала быть нашей.

    Значит, её мог взять другой прогон, и с этого момента в кабинете возможны
    два одновременных пишущих процесса. Единственное безопасное поведение —
    немедленно прекратить запись; сделанное остаётся в журнале, недоделанное
    доедет следующим прогоном.
    """


def _lock_holder() -> str:
    return f"{uuid.uuid4().hex[:12]}@{socket.gethostname()}:{os.getpid()}"


class RunLease:
    """Аренда прогона, которую можно продлить и перепроверить.

    Зачем этого мало — «взяли аренду на час в начале прогона»: срок аренды
    фиксирован, а длительность прогона нет. Сотни кампаний, чтение состояния
    по каждой, таймаут запроса в две минуты — прогон легко живёт дольше часа,
    и тогда его аренда протухает НА ХОДУ. Следующий по расписанию прогон видит
    свободный ключ, стартует штатно, и оба шлют bidmodifiers.add по одной
    кампании: в кабинете два объекта, Id первого не знает никто. Это ровно тот
    сценарий, от которого аренда и заводилась.

    Защита обязана не зависеть от того, уложился ли прогон в срок, поэтому её
    две половины:
      * продление (renew) — пока прогон жив и работает, аренда не протухает, и
        второй прогон просто не стартует;
      * перепроверка владения перед КАЖДЫМ изменяющим запросом (guard) — если
        аренда всё-таки потеряна (пауза процесса, недоступность БД, ручное
        снятие), прогон узнаёт об этом ДО отправки, а не после.
    Первая половина закрывает штатный случай, вторая — всё остальное.
    Обе — один и тот же UPDATE, поэтому продление и есть перепроверка: пустой
    ответ означает, что аренда уже не наша.
    """

    def __init__(self, conn, name: str, holder: str, ttl_minutes: int):
        self._conn = conn
        self.name = name
        self.holder = holder
        self.ttl_minutes = int(ttl_minutes)

    def renew(self) -> bool:
        """Продлевает аренду. False — аренда больше не наша."""
        with self._conn.cursor() as cur:
            cur.execute(RENEW_RUN_LOCK_SQL, {"name": self.name, "holder": self.holder,
                                             "ttl": self.ttl_minutes})
            alive = cur.fetchone() is not None
        self._conn.commit()
        return alive

    def guard(self) -> None:
        """Проверка владения перед изменяющим запросом. Не наша — RunLeaseLost."""
        if not self.renew():
            raise RunLeaseLost(
                f"аренда прогона {self.name} потеряна (держатель {self.holder}): "
                f"параллельный прогон мог начать писать в тот же кабинет — "
                f"запись прекращена"
            )


@contextmanager
def run_lock(name: str, ttl_minutes: int = RUN_LOCK_TTL_MINUTES):
    """Аренда на прогон. RunLockBusy — если прогон уже идёт.

    Отдаёт RunLease: аренду мало взять, её надо продлевать по ходу прогона и
    перепроверять перед каждым изменяющим запросом (см. RunLease).

    Соединение держится на всё тело блока, но сама аренда живёт в таблице:
    так она переживает пулер (сессионная advisory-блокировка через
    транзакционный пулер не гарантируется) и не переживает смерть процесса
    дольше срока годности.
    """
    if name not in RUN_LOCK_NAMES:
        raise ValueError(f"неизвестное имя аренды прогона: {name}")
    holder = _lock_holder()
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(ACQUIRE_RUN_LOCK_SQL, {
                "name": name, "holder": holder, "ttl": int(ttl_minutes)})
            got = cur.fetchone()
        conn.commit()
        if got is None:
            raise RunLockBusy(
                f"прогон {name} уже идёт: параллельный запуск запрещён — "
                f"два прогона на одном ключе создают в кабинете два объекта"
            )
        try:
            yield RunLease(conn, name, holder, int(ttl_minutes))
        finally:
            # Снимает аренду ТОЛЬКО её владелец: если аренда успела протухнуть
            # и её перехватил другой прогон, снимать чужую нельзя.
            # Сбой снятия не подменяет собой исходную ошибку прогона: аренда
            # всё равно протухнет сама, а потерять причину падения хуже.
            try:
                conn.rollback()
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM edu_agent_run_lock "
                        "WHERE lock_name = %s AND holder = %s",
                        (name, holder),
                    )
                conn.commit()
            except Exception:
                pass


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
