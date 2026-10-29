"""Версии входов по месяцам (P1 аудита Panda-BI 22.10) и запуск готовности витрин (C2).

Синк, записав данные, отмечает в ТОЙ ЖЕ транзакции: «источник X за дни [от; до] изменился» —
`bump()` зовёт `bump_data_version()` (миграция EduDash 20261028153741). Откатилась запись —
откатилась и версия, поэтому версия никогда не опережает данные. После COMMIT `ready()` зовёт
`data_ready()` (миграция 20261028154219): база сверяет версии входов витрин с посчитанным и
пересобирает только изменившиеся месяцы. Источник — имя таблицы-входа.

Миграция может быть ещё не накачена (выкладка кода и наката идут порознь), поэтому:
  • `bump` — под SAVEPOINT: отсутствие функции не валит транзакцию с данными;
  • `ready` — отсутствие функции = пропуск. Прочие ошибки готовности пробрасываются: данные уже
    закоммичены, а застывшая витрина без алерта хуже упавшего юнита (OnFailure шлёт хвост журнала).
"""

import os
from datetime import date
from typing import Any, Iterable, Optional, Tuple

import psycopg2

_UNDEFINED_FUNCTION = "42883"


def _day(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def date_span(dates: Iterable[Any]) -> Optional[Tuple[str, str]]:
    """Минимальная и максимальная дата набора (date или 'YYYY-MM-DD…'); пусто — None."""
    days = [_day(d) for d in dates if d]
    if not days:
        return None
    return min(days), max(days)


def bump(cur, project: str, source: str, date_from: Any, date_to: Any,
         rows: Optional[int] = None) -> Optional[int]:
    """Новая версия источника за месяцы диапазона — в транзакции курсора. Возвращает версию."""
    if not date_from or not date_to:
        return None
    cur.execute("SAVEPOINT data_version")
    try:
        cur.execute(
            "SELECT bump_data_version(%s, %s, %s::date, %s::date, %s)",
            (project, source, _day(date_from), _day(date_to), rows),
        )
        row = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT data_version")
    except psycopg2.Error as err:
        cur.execute("ROLLBACK TO SAVEPOINT data_version")
        print(f"data_versions: версия {project}/{source} не записана — {err.pgerror or err}")
        return None
    return row[0] if row else None


def bump_rows(cur, project: str, source: str, rows: Iterable[Any], key: str = "date") -> Optional[int]:
    """`bump` по диапазону дат строк (dict с полем key или кортеж с датой первым полем)."""
    rows = list(rows)
    span = date_span(r[key] if isinstance(r, dict) else r[0] for r in rows)
    if not span:
        return None
    return bump(cur, project, source, span[0], span[1], len(rows))


def bump_dates(cur, project: str, source: str, dates: Iterable[Any],
               rows: Optional[int] = None) -> Optional[int]:
    """`bump` по крайним датам набора: окно удаления, даты новых строк, старые края таблицы."""
    span = date_span(dates)
    if not span:
        return None
    return bump(cur, project, source, span[0], span[1], rows)


def month_fingerprints(cur, table: str, date_columns: Iterable[str],
                       columns: Iterable[str]) -> dict:
    """Отпечаток содержимого по месяцам каждой колонки дат: {(колонка, 'YYYY-MM-01'): md5}.

    Только по записываемым колонкам: служебные (updated_at DEFAULT now()) менялись бы на каждой
    перезаписи, и версия двигалась бы у всех месяцев без единой правки данных."""
    row = "row(" + ", ".join(columns) + ")::text"
    out = {}
    for c in date_columns:
        cur.execute(
            f"SELECT to_char(date_trunc('month', {c}), 'YYYY-MM-DD'), "
            f"md5(string_agg({row}, '|' ORDER BY {row})) FROM {table} GROUP BY 1"
        )
        for month, digest in cur.fetchall() or ():
            if month:
                out[(c, month)] = digest
    return out


def table_fingerprint(cur, table: str, columns: Iterable[str]) -> Optional[str]:
    """Отпечаток всего содержимого таблицы без дат (справочник) по записываемым колонкам."""
    row = "row(" + ", ".join(columns) + ")::text"
    cur.execute(f"SELECT md5(string_agg({row}, '|' ORDER BY {row})) FROM {table}")
    found = cur.fetchone()
    return found[0] if found else None


def bump_changed_months(cur, project: str, source: str, before: dict, after: dict,
                        rows: Optional[int] = None) -> list:
    """Версия только месяцам, чьё содержимое изменилось (появилось, ушло, поменялось).

    Для полной перезаписи таблицы: по одним датам строк не видно, что месяц не менялся, и
    каждая ночь двигала бы версию всех месяцев — кэш приложения остывал бы целиком."""
    months = sorted({key[1] for key in set(before) | set(after) if before.get(key) != after.get(key)})
    for month in months:
        bump(cur, project, source, month, month, rows)
    return months


# Месяц 1970-01 = «справочник, касается всех месяцев»: у справочника нет дат, а приложение
# примешивает его версию к ключу каждого месяца.
DIMENSION_DAY = "1970-01-01"


def bump_dimension(cur, project: str, source: str, rows: Optional[int] = None) -> Optional[int]:
    """`bump` справочника без дат. Звать, только если что-то реально записано."""
    return bump(cur, project, source, DIMENSION_DAY, DIMENSION_DAY, rows)


def bump_dimension_if_changed(cur, project: str, source: str, before: Optional[str],
                              columns: Iterable[str], rows: Optional[int] = None) -> Optional[int]:
    """`bump_dimension`, только если отпечаток справочника после записи отличается от `before`.

    Справочник переписывается каждым прогоном целиком, а его версия примешана к ключу КАЖДОГО
    месяца: безусловная отметка остужала бы весь кэш проекта при неизменном справочнике."""
    if table_fingerprint(cur, source, columns) == before:
        return None
    return bump_dimension(cur, project, source, rows)


def ready(project: str, source: str, url: Optional[str] = None) -> int:
    """После COMMIT: догнать витрины, которые питает источник. Возвращает число пересобранных месяцев."""
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        return 0
    conn = psycopg2.connect(url.split("?")[0], connect_timeout=30)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT mart, months FROM data_ready(%s, %s)", (project, source))
            done = cur.fetchall()
        conn.commit()
    except psycopg2.Error as err:
        conn.rollback()
        if getattr(err, "pgcode", None) == _UNDEFINED_FUNCTION:
            print("data_versions: data_ready() ещё нет в базе — готовность пропущена")
            return 0
        raise
    finally:
        conn.close()
    total = 0
    for mart, months in done:
        total += months or 0
        print(f"data_versions: {mart} — пересобрано месяцев: {months}")
    return total
