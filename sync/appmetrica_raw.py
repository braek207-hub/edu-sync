# -*- coding: utf-8 -*-
"""Единый загрузчик сырья AppMetrica LIME (приложение 4415407) → Postgres. Y1.

Единственный, кто ходит в Logs API этого приложения. Витрины (lime_appmetrica,
lime_app_install_source, app-часть разделов) читают appm_* через функции read_* ниже.
Схема — EDU v2 supabase/migrations/20261024164700_appmetrica_raw.sql, дизайн —
EDU v2 docs/superpowers/specs/2026-10-22-appmetrica-single-loader.md.

Logs API (https://appmetrica.yandex.ru/docs/ru/mobile-api/logs/request-procedure.md, quotas.md):
очередь на приложение — не больше 3 запросов, обрабатываются строго по одному; первый GET
ставит подготовку (202), идентичный GET после готовности отдаёт файл (200), файл живёт 24 ч.
Отсюда правила загрузчика:
  · в очереди не больше ОДНОГО нашего запроса; новый заказывается, только когда очередь пуста;
  · заказ записан в appm_export_jobs с точными параметрами — следующий прогон опрашивает тот же
    запрос, а не заказывает новый;
  · закрытые дни повторно не заказываются: ежедневно только хвост (установки 14 дн., события 7 дн.)
    и недостающие по appm_coverage дни (бэкфилл), по одному заданию;
  · запрос в очереди старше STUCK_HOURS — мёртвый (10.10 три запроса встали на 0.99 на 12 дней):
    отменяется по возрасту, а не только на 429 (на идентичный зависший запрос API отвечает 202);
  · Shards-Unavailable ≠ 0 — неполные данные, задание падает без записи.

Запуск: python -m sync.appmetrica_raw. Выход 0 — хвост сегодняшнего дня загружен по всем
потокам (витрины можно считать); 3 — не загружен к сроку (витрины не запускать).
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import requests

BASE = "https://api.appmetrica.yandex.ru/logs/v1/export"
APP_ID = "4415407"
_MSK = timezone(timedelta(hours=3))  # create_time в get_active_queries — московское (замер 22.10)

POLL_SEC = int(os.environ.get("APPM_POLL_SEC") or "60")
STUCK_HOURS = float(os.environ.get("APPMETRICA_STUCK_HOURS") or "6")
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Stream:
    name: str
    endpoint: str
    fields: str
    dimension: str
    tail_days: int
    backfill_from: str          # первый день, который должен быть в покрытии
    chunk: str                  # month | week — нарезка бэкфилла
    fmt: str = "json"           # csv — стримом (сессии: сотни тысяч строк в день)
    event_name: str | None = None


# Поля — объединение того, что читали три витрины из API (установки: INSTALL_FIELDS
# appmetrica_logs.py + tracker_name разделов + гео; покупки: EVENT_FIELDS + гео + время приёма).
STREAMS: dict[str, Stream] = {s.name: s for s in (
    Stream("installs", "installations",
           "appmetrica_device_id,install_datetime,install_receive_datetime,publisher_name,"
           "tracker_name,click_url_parameters,is_reattribution,is_reinstallation,city,country_iso_code",
           "default", tail_days=14,
           # С запуска приложения: last-touch разделов падает на установку любой давности.
           backfill_from=os.environ.get("APPM_INSTALLS_FROM") or "2024-08-01", chunk="month"),
    Stream("deeplinks", "deeplinks",
           "appmetrica_device_id,event_datetime,publisher_name,tracker_name",
           "default", tail_days=7,
           backfill_from=os.environ.get("APPM_DEEPLINKS_FROM") or "2026-06-01", chunk="month"),
    Stream("purchases", "events",
           "appmetrica_device_id,event_datetime,event_receive_datetime,event_json,city,country_iso_code",
           "receive", tail_days=7,
           # Окно когорт lime_appmetrica (7 мес.) с запасом на сверку с витринами от 30.09.
           backfill_from=os.environ.get("APPM_PURCHASES_FROM") or "2026-03-01", chunk="month",
           event_name="purchase"),
    Stream("sessions", "sessions_starts",
           "appmetrica_device_id,session_start_datetime",
           "default", tail_days=7,
           backfill_from=os.environ.get("APPM_SESSIONS_FROM") or "2026-09-15", chunk="week", fmt="csv"),
)}
# Порядок заказа хвоста: установки первыми — без них не считается ни одна витрина.
STREAM_ORDER = ("installs", "purchases", "sessions", "deeplinks")


class RawNotReady(RuntimeError):
    """Сырья за окно витрины нет в appm_coverage — витрина не пишет ничего."""


class ShardsUnavailable(RuntimeError):
    pass


# ── планирование ──────────────────────────────────────────────────────────────────────────

def _d(s) -> date:
    return s if isinstance(s, date) else date.fromisoformat(str(s)[:10])


def chunks(since: date, until: date, chunk: str) -> list[tuple[date, date]]:
    """Окна бэкфилла: календарные месяцы или недели (с понедельника), обрезанные по [since, until]."""
    out: list[tuple[date, date]] = []
    if since > until:
        return out
    if chunk == "month":
        cur = date(since.year, since.month, 1)
        while cur <= until:
            nxt = date(cur.year + cur.month // 12, cur.month % 12 + 1, 1)
            out.append((max(cur, since), min(nxt - timedelta(days=1), until)))
            cur = nxt
    else:
        cur = since - timedelta(days=since.weekday())
        while cur <= until:
            out.append((max(cur, since), min(cur + timedelta(days=6), until)))
            cur += timedelta(days=7)
    return out


def missing_ranges(covered: set[date], since: date, until: date, chunk: str) -> list[tuple[date, date]]:
    """Окна бэкфилла, в которых есть хоть один непокрытый день; окно сужается до непокрытых краёв,
    чтобы не перезаказывать уже загруженное."""
    out = []
    for a, b in chunks(since, until, chunk):
        days = [a + timedelta(days=i) for i in range((b - a).days + 1)]
        miss = [d for d in days if d not in covered]
        if miss:
            out.append((min(miss), max(miss)))
    return out


def export_params(stream: Stream, since: date, until: date) -> dict:
    p = {
        "application_id": APP_ID,
        "date_since": f"{since.isoformat()} 00:00:00",
        "date_until": f"{until.isoformat()} 23:59:59",
        "date_dimension": stream.dimension,
        "fields": stream.fields,
    }
    if stream.event_name:
        p["event_name"] = stream.event_name
    return p


def plan(today: date, covered: dict[str, set[date]]) -> list[dict]:
    """Задания на сегодня: хвост каждого потока (закрытые дни до вчера) + бэкфилл непокрытого.

    Хвост — priority 0, в порядке STREAM_ORDER; бэкфилл — priority 1, новые окна первыми
    (витрины считают недавние периоды). Дни хвоста бэкфилл не планирует — их закроет хвост."""
    yesterday = today - timedelta(days=1)
    jobs = []
    for name in STREAM_ORDER:
        s = STREAMS[name]
        t_since = today - timedelta(days=s.tail_days)
        jobs.append(dict(stream=name, kind="tail", priority=0, date_since=t_since, date_until=yesterday,
                         params=export_params(s, t_since, yesterday)))
    for name in STREAM_ORDER:
        s = STREAMS[name]
        b_until = today - timedelta(days=s.tail_days + 1)
        for a, b in reversed(missing_ranges(covered.get(name, set()), _d(s.backfill_from), b_until, s.chunk)):
            jobs.append(dict(stream=name, kind="backfill", priority=1, date_since=a, date_until=b,
                             params=export_params(s, a, b)))
    return jobs


# ── Logs API ──────────────────────────────────────────────────────────────────────────────

def _headers(token: str) -> dict:
    return {"Authorization": f"OAuth {token}", "Accept-Encoding": "gzip"}


def active_queries(token: str) -> list[dict]:
    r = requests.get(f"{BASE}/get_active_queries", params={"application_id": APP_ID},
                     headers=_headers(token), timeout=60)
    r.raise_for_status()
    return r.json()


def cancel_query(token: str, request_id: str) -> str:
    r = requests.post(f"{BASE}/{request_id}/cancel", params={"application_id": APP_ID},
                      headers=_headers(token), timeout=60)
    r.raise_for_status()
    try:
        return r.json().get("message", "")
    except ValueError:
        return r.text[:200]


def stale_queries(queries: list[dict], now: datetime, hours: float = STUCK_HOURS) -> list[dict]:
    """Запросы очереди старше hours. Сами они не истекают (дока срока не называет, факт 10–22.10)."""
    out = []
    for q in queries:
        born = datetime.strptime(q["create_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_MSK)
        if (now - born).total_seconds() >= hours * 3600:
            out.append(q)
    return out


def check_shards(resp) -> None:
    v = (resp.headers.get("Shards-Unavailable") or "0").strip()
    if v not in ("0", "", "0.0"):
        raise ShardsUnavailable(f"Shards-Unavailable={v}")


def parse_body(stream: Stream, resp) -> list[dict]:
    if stream.fmt == "json":
        return resp.json().get("data", [])
    resp.raw.decode_content = True
    text = io.TextIOWrapper(resp.raw, encoding="utf-8", newline="")
    # Сессии сворачиваются по (день, устройство) прямо из потока: неделя — ~3,5 млн стартов,
    # списком словарей это ~1,5 ГБ памяти на VPS с 8 ГБ; свёрнутых пар на порядок меньше.
    acc: dict[tuple[str, str], int] = defaultdict(int)
    for r in csv.DictReader(text):
        dev = r.get("appmetrica_device_id")
        if dev:
            acc[(r["session_start_datetime"][:10], dev)] += 1
    return [{"appmetrica_device_id": dev, "session_start_datetime": d, "n": n} for (d, dev), n in acc.items()]


def fetch_once(stream: Stream, params: dict, token: str):
    """Один GET задания. → (status_code, rows | None). rows только при 200."""
    url = f"{BASE}/{stream.endpoint}.{stream.fmt}"
    r = requests.get(url, params=params, headers=_headers(token), timeout=1800,
                     stream=stream.fmt == "csv")
    try:
        if r.status_code != 200:
            return r.status_code, r.text[:300]
        check_shards(r)
        return 200, parse_body(stream, r)
    finally:
        r.close()


# ── строки → таблицы ──────────────────────────────────────────────────────────────────────

def _ts(s):
    return (s or "")[:19] or None


def to_table_rows(stream: Stream, rows, job_id: int, since: date, until: date):
    """Строки API → кортежи сырьевой таблицы. Строка вне окна задания — отказ: API вернул чужие
    дни, а запись переписывает ровно дни задания."""
    out = []

    def day_ok(d: str) -> date:
        dd = date.fromisoformat(d[:10])
        if not (since <= dd <= until):
            raise RuntimeError(f"{stream.name}: строка за {dd} вне окна {since}..{until}")
        return dd

    if stream.name == "sessions":
        acc: dict[tuple[date, str], int] = defaultdict(int)
        for r in rows:
            dev = r.get("appmetrica_device_id")
            if dev:
                acc[(day_ok(r["session_start_datetime"]), dev)] += int(r.get("n", 1))
        return [(d, dev, n, job_id) for (d, dev), n in acc.items()]
    for r in rows:
        dev = r.get("appmetrica_device_id")
        if not dev:
            continue
        if stream.name == "installs":
            out.append((day_ok(r["install_datetime"]), dev, _ts(r["install_datetime"]),
                        _ts(r.get("install_receive_datetime")), r.get("publisher_name"),
                        r.get("tracker_name"), r.get("click_url_parameters"),
                        str(r.get("is_reattribution", "")), str(r.get("is_reinstallation", "")),
                        r.get("country_iso_code"), r.get("city"), job_id))
        elif stream.name == "deeplinks":
            out.append((day_ok(r["event_datetime"]), dev, _ts(r["event_datetime"]),
                        r.get("publisher_name"), r.get("tracker_name"), job_id))
        elif stream.name == "purchases":
            out.append((day_ok(r["event_receive_datetime"]), dev, _ts(r["event_datetime"]),
                        _ts(r.get("event_receive_datetime")), r.get("event_json"),
                        r.get("country_iso_code"), r.get("city"), job_id))
    return out


TABLES = {
    "installs": ("appm_installs", "install_date",
                 "install_date,device_id,install_datetime,install_receive_datetime,publisher_name,"
                 "tracker_name,click_url_parameters,is_reattribution,is_reinstallation,country_iso_code,"
                 "city,job_id"),
    "deeplinks": ("appm_deeplinks", "event_date",
                  "event_date,device_id,event_datetime,publisher_name,tracker_name,job_id"),
    "purchases": ("appm_purchases", "receive_date",
                  "receive_date,device_id,event_datetime,event_receive_datetime,event_json,"
                  "country_iso_code,city,job_id"),
    "sessions": ("appm_sessions_day", "date", "date,device_id,sessions,job_id"),
}


def store(conn, stream: Stream, job_id: int, since: date, until: date, table_rows: list[tuple]) -> None:
    """Дни задания переписываются целиком одной транзакцией вместе с покрытием и статусом:
    оборванная запись не оставляет ни полудня, ни ложного покрытия."""
    table, day_col, cols = TABLES[stream.name]
    per_day: dict[date, int] = defaultdict(int)
    for row in table_rows:
        per_day[row[0]] += 1
    days = [since + timedelta(days=i) for i in range((until - since).days + 1)]
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {table} WHERE {day_col} BETWEEN %s AND %s", (since, until))
        if table_rows:
            psycopg2.extras.execute_values(cur, f"INSERT INTO {table} ({cols}) VALUES %s",
                                           table_rows, page_size=5000)
        psycopg2.extras.execute_values(
            cur, "INSERT INTO appm_coverage (stream, day, job_id, rows) VALUES %s "
                 "ON CONFLICT (stream, day) DO UPDATE SET job_id = EXCLUDED.job_id, rows = EXCLUDED.rows, "
                 "loaded_at = now()",
            [(stream.name, d, job_id, per_day.get(d, 0)) for d in days])
        cur.execute("UPDATE appm_export_jobs SET status = 'loaded', rows_loaded = %s, finished_at = now(), "
                    "error = NULL WHERE id = %s", (len(table_rows), job_id))
    conn.commit()


# ── журнал заказов ────────────────────────────────────────────────────────────────────────

def covered_days(conn) -> dict[str, set[date]]:
    with conn.cursor() as cur:
        cur.execute("SELECT stream, day FROM appm_coverage")
        out: dict[str, set[date]] = defaultdict(set)
        for s, d in cur.fetchall():
            out[s].add(d)
    return out


def save_plan(conn, jobs: list[dict]) -> int:
    n = 0
    with conn.cursor() as cur:
        for j in jobs:
            cur.execute(
                "INSERT INTO appm_export_jobs (app_id, stream, kind, date_since, date_until, params, priority) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (app_id, stream, date_since, date_until) WHERE status IN ('planned', 'requested') "
                "DO NOTHING",
                (APP_ID, j["stream"], j["kind"], j["date_since"], j["date_until"],
                 json.dumps(j["params"], ensure_ascii=False), j["priority"]))
            n += cur.rowcount
    conn.commit()
    return n


def drop_obsolete_tails(conn, today: date) -> None:
    """Вчерашний хвост, так и не заказанный, сегодня заменён новым: окно сдвинулось."""
    with conn.cursor() as cur:
        cur.execute("UPDATE appm_export_jobs SET status = 'cancelled', finished_at = now(), "
                    "error = 'заменён хвостом нового дня' WHERE app_id = %s AND kind = 'tail' "
                    "AND status = 'planned' AND date_until < %s", (APP_ID, today - timedelta(days=1)))
    conn.commit()


_JOB_COLS = "id, stream, kind, date_since, date_until, params, status, attempts, requested_at"


def _job(row) -> dict:
    keys = [c.strip() for c in _JOB_COLS.split(",")]
    j = dict(zip(keys, row))
    if isinstance(j["params"], str):
        j["params"] = json.loads(j["params"])
    return j


def current_job(conn) -> dict | None:
    """Заказанное и не забранное — его опрашиваем, новое не заказываем (одно место в очереди)."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_JOB_COLS} FROM appm_export_jobs WHERE app_id = %s AND status = 'requested' "
                    "ORDER BY requested_at LIMIT 1", (APP_ID,))
        row = cur.fetchone()
    return _job(row) if row else None


def next_planned(conn) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_JOB_COLS} FROM appm_export_jobs WHERE app_id = %s AND status = 'planned' "
                    "ORDER BY priority, CASE WHEN kind = 'tail' THEN id END, date_since DESC, id LIMIT 1",
                    (APP_ID,))
        row = cur.fetchone()
    return _job(row) if row else None


def set_status(conn, job_id: int, status: str, error: str | None = None, polled: bool = False) -> None:
    sets = ["status = %s", "error = %s"]
    if status == "requested":
        sets.append("requested_at = COALESCE(requested_at, now())")
    if polled:
        sets += ["polls = polls + 1", "last_polled_at = now()"]
    if status in ("failed", "stuck", "cancelled"):
        sets.append("finished_at = now()")
    with conn.cursor() as cur:
        cur.execute(f"UPDATE appm_export_jobs SET {', '.join(sets)} WHERE id = %s", (status, error, job_id))
    conn.commit()


def retry_or_fail(conn, job: dict, error: str) -> None:
    """Сбой задания: до MAX_ATTEMPTS — снова в план (новый заказ), дальше — failed."""
    with conn.cursor() as cur:
        cur.execute("UPDATE appm_export_jobs SET attempts = attempts + 1, requested_at = NULL, error = %s, "
                    "status = CASE WHEN attempts + 1 >= %s THEN 'failed' ELSE 'planned' END, "
                    "finished_at = CASE WHEN attempts + 1 >= %s THEN now() END WHERE id = %s",
                    (error[:500], MAX_ATTEMPTS, MAX_ATTEMPTS, job["id"]))
    conn.commit()


def tails_loaded(conn, today: date) -> bool:
    """Покрыт ли вчерашний день всеми потоками — тогда витрины можно считать."""
    yesterday = today - timedelta(days=1)
    with conn.cursor() as cur:
        cur.execute("SELECT count(DISTINCT stream) FROM appm_coverage WHERE day = %s AND stream = ANY(%s)",
                    (yesterday, list(STREAM_ORDER)))
        return cur.fetchone()[0] == len(STREAM_ORDER)


# ── прогон ────────────────────────────────────────────────────────────────────────────────

def clean_queue(token: str, now: datetime | None = None) -> tuple[int, list[str]]:
    """→ (активных после очистки, отменённые id). Очередь читается без побочных эффектов."""
    now = now or datetime.now(_MSK)
    queries = active_queries(token)
    cancelled = []
    for q in stale_queries(queries, now):
        rid = str(q["request_id"])
        msg = cancel_query(token, rid)
        cancelled.append(rid)
        print(f"  очередь: отменён {rid} от {q['create_time']} (прогресс {q.get('total_progress')}): {msg}",
              flush=True)
    return len(queries) - len(cancelled), cancelled


def step(conn, token: str, job: dict | None, active: int) -> tuple[str, dict | None]:
    """Один шаг автомата. → (что произошло, задание, которое опрашиваем дальше)."""
    if job is None:
        if active > 0:
            return "queue-busy", None
        job = next_planned(conn)
        if job is None:
            return "idle", None
    s = STREAMS[job["stream"]]
    since, until = _d(job["date_since"]), _d(job["date_until"])
    try:
        code, body = fetch_once(s, job["params"], token)
    except ShardsUnavailable as e:
        retry_or_fail(conn, job, str(e))
        print(f"  {s.name} {since}..{until}: {e} — без записи", flush=True)
        return "shards", None
    except (requests.RequestException, OSError, ValueError) as e:
        # Обрыв: заказ на стороне API жив — тот же GET в следующий раз отдаст файл.
        set_status(conn, job["id"], "requested", f"обрыв: {e}"[:500], polled=True)
        return "network", job
    if code == 202:
        set_status(conn, job["id"], "requested", polled=True)
        return "wait", job
    if code == 429:
        set_status(conn, job["id"], job["status"] if job["status"] == "requested" else "planned",
                   f"429: {body}"[:500])
        return "429", None
    if code != 200:
        retry_or_fail(conn, job, f"HTTP {code}: {body}")
        print(f"  {s.name} {since}..{until}: HTTP {code} {body}", flush=True)
        return "error", None
    rows = to_table_rows(s, body, job["id"], since, until)
    del body
    store(conn, s, job["id"], since, until, rows)
    print(f"  {s.name} {job['kind']} {since}..{until}: записано {len(rows):,}", flush=True)
    return "loaded", None


def run(conn, token: str, today: date, deadline: float, sleep=time.sleep, now=None) -> bool:
    drop_obsolete_tails(conn, today)
    added = save_plan(conn, plan(today, covered_days(conn)))
    print(f"[appm-raw] {today}: в план добавлено {added}", flush=True)
    job = current_job(conn)
    active, _ = clean_queue(token, now)
    if job and job["requested_at"] is not None:
        born = job["requested_at"]
        if born.tzinfo is None:
            born = born.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - born).total_seconds() >= STUCK_HOURS * 3600:
            # Своё задание висит дольше порога: очередь уже очищена clean_queue по возрасту,
            # заказываем заново.
            retry_or_fail(conn, job, f"висело дольше {STUCK_HOURS} ч")
            job = None
    if job:
        active = max(active - 1, 0)  # наш собственный запрос не мешает его же опросу
    while time.monotonic() < deadline:
        what, job = step(conn, token, job, active)
        if what == "loaded":
            active = 0
            continue
        if what == "idle":
            break
        if what in ("queue-busy", "429"):
            active, _ = clean_queue(token, now)
        sleep(POLL_SEC)
    ok = tails_loaded(conn, today)
    print(f"[appm-raw] хвост {today - timedelta(days=1)} {'загружен' if ok else 'НЕ загружен'}", flush=True)
    return ok


# ── чтение для витрин ─────────────────────────────────────────────────────────────────────

def connect():
    return psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)


def require_coverage(conn, stream: str, since: date, until: date) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT day FROM appm_coverage WHERE stream = %s AND day BETWEEN %s AND %s",
                    (stream, since, until))
        have = {r[0] for r in cur.fetchall()}
    miss = [since + timedelta(days=i) for i in range((until - since).days + 1)]
    miss = [d for d in miss if d not in have]
    if miss:
        raise RawNotReady(f"сырья {stream} нет за {len(miss)} дн. ({miss[0]}..{miss[-1]}) — "
                          f"загрузчик lime_appmetrica_raw ещё не дотянул окно")


def _fmt(v):
    return v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v


def cutoff() -> str | None:
    """APPM_CUTOFF='YYYY-MM-DD HH:MM:SS' — только сверка: видеть сырьё так, как его отдал бы API
    в этот момент (строки, принятые сервером позже, отбрасываются)."""
    return (os.environ.get("APPM_CUTOFF") or "").strip() or None


def read_installations(conn, since, until) -> list[dict]:
    since, until = _d(since), _d(until)
    require_coverage(conn, "installs", since, until)
    cut = cutoff()
    sql = ("SELECT device_id, install_datetime, install_receive_datetime, publisher_name, tracker_name, "
           "click_url_parameters, is_reattribution, is_reinstallation, city, country_iso_code "
           "FROM appm_installs WHERE install_date BETWEEN %s AND %s")
    args = [since, until]
    if cut:
        sql += " AND COALESCE(install_receive_datetime, install_datetime) <= %s"
        args.append(cut)
    # Порядок API не гарантирован (дока: «идентичные запросы» не обязаны давать тот же порядок):
    # витрины, которым он важен (первая/последняя установка), получают детерминированный.
    sql += " ORDER BY install_datetime, device_id, is_reinstallation, is_reattribution"
    keys = ("appmetrica_device_id", "install_datetime", "install_receive_datetime", "publisher_name",
            "tracker_name", "click_url_parameters", "is_reattribution", "is_reinstallation", "city",
            "country_iso_code")
    with conn.cursor(name="appm_installs_read") as cur:
        cur.itersize = 50000
        cur.execute(sql, args)
        return [dict(zip(keys, map(_fmt, r))) for r in cur]


def read_purchase_events(conn, since, until, date_dimension: str = "receive") -> list[dict]:
    """Покупки окна по дате приёма (receive) или события (default) — как отдавал бы API.

    default: события окна могли прийти позже — покрытие нужно по дате приёма от since до
    вчера (или до конца окна при сверке с отсечкой)."""
    since, until = _d(since), _d(until)
    cut = cutoff()
    if date_dimension == "receive":
        require_coverage(conn, "purchases", since, until)
        where, args = "receive_date BETWEEN %s AND %s", [since, until]
    else:
        last = date.fromisoformat(cut[:10]) if cut else today() - timedelta(days=1)
        require_coverage(conn, "purchases", since, max(until, min(last, until + timedelta(days=60))))
        where = "event_datetime >= %s AND event_datetime < %s"
        args = [datetime.combine(since, datetime.min.time()),
                datetime.combine(until + timedelta(days=1), datetime.min.time())]
    if cut:
        where += " AND COALESCE(event_receive_datetime, event_datetime) <= %s"
        args.append(cut)
    keys = ("appmetrica_device_id", "event_datetime", "event_receive_datetime", "event_json", "city",
            "country_iso_code")
    with conn.cursor() as cur:
        cur.execute(f"SELECT device_id, event_datetime, event_receive_datetime, event_json, city, "
                    f"country_iso_code FROM appm_purchases WHERE {where} "
                    f"ORDER BY event_receive_datetime, event_datetime, device_id", args)
        out = []
        for r in cur.fetchall():
            d = dict(zip(keys, map(_fmt, r)))
            d["event_name"] = "purchase"
            out.append(d)
        return out


def read_sessions_day(conn, since, until) -> list[tuple]:
    """→ [(device, date, sessions)] — форма sync.lime_app_install_source.aggregate_sessions."""
    since, until = _d(since), _d(until)
    require_coverage(conn, "sessions", since, until)
    with conn.cursor() as cur:
        cur.execute("SELECT device_id, date, sessions FROM appm_sessions_day WHERE date BETWEEN %s AND %s",
                    (since, until))
        return [(dev, d, n) for dev, d, n in cur.fetchall()]


def read_deeplinks(conn, since, until) -> list[dict]:
    since, until = _d(since), _d(until)
    require_coverage(conn, "deeplinks", since, until)
    keys = ("appmetrica_device_id", "event_datetime", "publisher_name", "tracker_name")
    with conn.cursor() as cur:
        cur.execute("SELECT device_id, event_datetime, publisher_name, tracker_name FROM appm_deeplinks "
                    "WHERE event_date BETWEEN %s AND %s ORDER BY event_datetime, device_id", (since, until))
        return [dict(zip(keys, map(_fmt, r))) for r in cur.fetchall()]


def target_table(cur, name: str) -> str:
    """Имя таблицы витрины для записи. APPM_TABLE_SUFFIX=_shadow — только сверка: витрина
    пишет в копию <name>_shadow той же структуры, боевая не трогается."""
    sfx = (os.environ.get("APPM_TABLE_SUFFIX") or "").strip()
    if not sfx:
        return name
    if not re.fullmatch(r"_[a-z0-9_]{1,20}", sfx):
        raise ValueError(f"APPM_TABLE_SUFFIX={sfx!r}: только _[a-z0-9_]")
    t = name + sfx
    cur.execute(f"CREATE TABLE IF NOT EXISTS {t} (LIKE {name} INCLUDING ALL)")
    cur.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
    cur.execute(f"REVOKE ALL ON {t} FROM anon, authenticated")
    return t


def source() -> str:
    """APPM_SOURCE=db (по умолчанию) — витрины из сырья; api — старый путь, только откат."""
    return (os.environ.get("APPM_SOURCE") or "db").strip().lower()


def today() -> date:
    """APPM_TODAY — только сверка: посчитать витрину «как в тот день»."""
    v = (os.environ.get("APPM_TODAY") or "").strip()
    return date.fromisoformat(v) if v else date.today()


def main() -> int:
    token = os.environ.get("APPMETRICA_TOKEN")
    if not token or not os.environ.get("DATABASE_URL"):
        print("[appm-raw] нет APPMETRICA_TOKEN или DATABASE_URL")
        return 1
    budget_min = float(os.environ.get("APPM_RUN_MINUTES") or "300")
    conn = connect()
    try:
        ok = run(conn, token, today(), time.monotonic() + budget_min * 60)
    finally:
        conn.close()
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
