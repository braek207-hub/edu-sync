"""Пересчёт витрин Panda-BI сразу после синка.

Зачем это здесь, а не только в кроне базы. Витрина — снимок: пока её не пересчитали,
дашборд показывает данные на момент прошлого пересчёта. Крон `pg_cron` в 05:20 UTC
закрывает ночь, но любой ручной прогон синка днём иначе оставлял бы витрину вчерашней,
и человек видел бы старый расход при свежем сырье.

Пересчёт идёт через `refresh_mart()` (миграция 20260828130000 в EduDash), которая в той
же транзакции пишет факт в `mart_refresh_log`. Из этого журнала приложение берёт версию
данных — то есть пересчёт двигает ключ кэша. Голый REFRESH ключ не двигает, и новые числа
лежали бы в базе, не доезжая до экрана.

Отсутствие витрины или функции — не ошибка синка: миграция может быть ещё не применена.
Такой случай печатается и пропускается, синк остаётся зелёным.
"""

import os
import sys

import psycopg2

MARTS = ["mart_lime_cabinet_daily"]


def refresh_marts(marts: list[str] | None = None) -> int:
    names = marts or MARTS
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("refresh_marts: нет DATABASE_URL — пропускаю")
        return 0

    done = 0
    conn = psycopg2.connect(url.split("?")[0], connect_timeout=30)
    try:
        for name in names:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT refresh_mart(%s)", (name,))
                conn.commit()
                done += 1
                print(f"refresh_marts: {name} пересчитана")
            except psycopg2.Error as err:
                conn.rollback()
                # Миграция не применена либо витрина переименована — синк не роняем,
                # но и не молчим: тихий пропуск выглядел бы как успешный пересчёт.
                print(f"refresh_marts: {name} не пересчитана — {err.pgerror or err}")
    finally:
        conn.close()
    return done


if __name__ == "__main__":
    refresh_marts(sys.argv[1:] or None)
