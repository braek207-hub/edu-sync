# -*- coding: utf-8 -*-
"""Сырьё AppMetrica LIME (4415407) из Data Stream API → те же appm_*, что пишет Logs API. Y2.

Logs API приложения встал (с 28.10 ни одна выгрузка не двигается с 0 %), а DSA у приложения
включён стороной LIME — потоки настроены не нами, настройки (POST .../datastream/settings) не
трогаем: перезапись сломала бы чужую выгрузку. Читаем тем же APPMETRICA_TOKEN.

Дока: https://appmetrica.yandex.ru/docs/ru/mobile-api/datastream/about. Что из неё следует:
  · данные — окна по 5 минут (stream_window_timestamp), отдаются 7 суток и истории не имеют:
    окна копятся в спуле на диске (SPOOL), иначе пропадают;
  · последние окна ещё дописываются — окно берётся, когда update_timestamp старше LAG_SEC;
    дописанное позже (update_timestamp вырос) скачивается заново;
  · 503 — повтор, 500 — нет.
Окно собирает строки по времени ПРИЁМА сервером (замер 29.10: receive_timestamp − окно от −193 до
+336 с), даты в строках — московские (receive_datetime − receive_timestamp = +10800 с).

Сборка дня. День D (МСК) потока пишется в appm_* целиком, как задание Logs API за один день:
  · покупки — по дате приёма (как date_dimension=receive): нужны окна, покрывающие сутки приёма D;
  · установки, диплинки, сессии — по дате события (default): берутся все строки спула с датой D,
    включая пришедшие позже; день собирается, когда спул покрывает приём с начала D до конца D.
Хвост (14 дн. установок, 7 — остальным, как у Logs API) пересобирается раз в московские сутки,
чтобы подобрать опоздавшие строки; новый закрывшийся день — в первый же прогон. Дни старше
хвоста не трогаются: спул их уже не покрывает целиком.

Запуск: python -m sync.appmetrica_dsa — скачать окна и собрать дни (юнит каждые 20 мин);
python -m sync.appmetrica_dsa ready — выход 0, если вчера покрыто всеми потоками (ворота витрин).
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

from sync import appmetrica_raw as raw

BASE = f"https://api.appmetrica.yandex.ru/datastream/v1/application/{raw.APP_ID}"
SPOOL = os.environ.get("APPM_DSA_DIR") or "/opt/panda/appm-dsa/raw"
_MSK = timezone(timedelta(hours=3))
LAG_SEC = 15 * 60          # дока: последние окна дописываются, брать с отставанием от 10 мин
MARGIN_SEC = 10 * 60       # строка лежит в окне ±5,6 мин от времени приёма (замер 29.10)
KEEP_DAYS = 16             # хвост установок 14 дн. + запас; старше — удаляется
# Из потока event на диск идут только события, которые читают синки LIME (spec 2026-10-22 §1):
# весь поток — ~4 ГБ gzip в сутки. view_item / section_tab_show / add_to_cart — для app-части
# разделов (этап 2), пока не грузятся в базу.
SPOOL_EVENTS = {"purchase", "view_item", "section_tab_show", "add_to_cart"}


@dataclass(frozen=True)
class Feed:
    stream: str        # поток сырья (appmetrica_raw.STREAMS)
    dtype: str         # тип данных DSA
    day_col: str       # колонка, по дате которой строка попадает в день
    event_name: str | None = None


FEEDS = (
    Feed("installs", "installation", "install_datetime"),
    Feed("purchases", "event", "event_receive_datetime", "purchase"),
    Feed("sessions", "session_start", "session_start_datetime"),
    Feed("deeplinks", "deeplink", "event_datetime"),
)
DTYPES = tuple(dict.fromkeys(f.dtype for f in FEEDS))


# ── спул окон ─────────────────────────────────────────────────────────────────────────────

def _get(url: str, token: str) -> bytes:
    for attempt in range(4):
        r = requests.get(url, headers={"Authorization": f"OAuth {token}"}, timeout=300)
        if r.status_code == 503 and attempt < 3:  # дока: 503 — повтор может помочь, 500 — нет
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        body = r.content
        return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
    raise RuntimeError("unreachable")


def spooled(dtype: str) -> dict[int, tuple[int, str]]:
    """→ {окно: (update_timestamp, путь)} — что уже лежит на диске."""
    d = os.path.join(SPOOL, dtype)
    out = {}
    if os.path.isdir(d):
        for f in os.listdir(d):
            if f.endswith(".csv.gz"):
                ts, upd = f[: -len(".csv.gz")].split("_")
                out[int(ts)] = (int(upd), os.path.join(d, f))
    return out


def _state_path(dtype: str) -> str:
    return os.path.join(SPOOL, dtype, "_range.json")


def load_range(dtype: str) -> dict | None:
    try:
        with open(_state_path(dtype), encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def covered_range(listed: list[int], ok: set[int], prev: dict | None) -> dict | None:
    """Отрезок окон [first, last], за который в спуле лежит всё, что отдавал status.

    listed — дописанные окна status, ok — те из них, что лежат в спуле в последней версии.
    Окон, которых нет в status, не бывает с данными (пустые status не перечисляет). first
    живёт между прогонами: старые окна уходят из status, а в спуле остаются. Первый прогон
    начинает с первого окна status — что истекло раньше, проверить уже нечем."""
    listed = sorted(listed)
    first = prev["first"] if prev else (listed[0] if listed else None)
    if prev and listed and listed[0] > prev["last"] + 300:
        # Между прошлым отрезком и status окна истекли не скачанными (простой дольше 7 суток,
        # 404 на самом старом окне): склеить нельзя — пустое окно от протухшего не отличить.
        first = listed[0]
    if first is None:
        return None
    missing = [t for t in listed if t >= first and t not in ok]
    last = missing[0] - 300 if missing else (listed[-1] if listed else prev["last"])
    if last < first:
        # Без сохранения: иначе окно, истёкшее до первого скачивания, стало бы невидимой дырой.
        return prev
    return {"first": first, "last": last}


def save_window(dtype: str, ts: int, upd: int, text: str, old: str | None) -> None:
    if dtype == "event":
        src = csv.DictReader(io.StringIO(text))
        out = io.StringIO()
        wr = csv.DictWriter(out, fieldnames=src.fieldnames or [])
        wr.writeheader()
        wr.writerows(r for r in src if r.get("event_name") in SPOOL_EVENTS)
        text = out.getvalue()
    d = os.path.join(SPOOL, dtype)
    tmp = os.path.join(d, f".{ts}.tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, os.path.join(d, f"{ts}_{upd}.csv.gz"))
    if old:
        os.remove(old)


def fetch(token: str, now: float, deadline: float) -> dict[str, dict | None]:
    """Скачать дописанные окна, которых нет в спуле или которые Яндекс дописал. → отрезки."""
    status = json.loads(_get(f"{BASE}/status", token))["status"]
    by_type = {s["data_type"]: s["stream_windows"] for s in status["streams"]}
    ranges = {}
    for dtype in DTYPES:
        os.makedirs(os.path.join(SPOOL, dtype), exist_ok=True)
        have = spooled(dtype)
        listed, ok = [], set()
        saved = 0
        # от старых к новым: старое окно первым уходит из хранения
        for w in sorted(by_type.get(dtype, []), key=lambda w: int(w["stream_window_timestamp"])):
            ts, upd = int(w["stream_window_timestamp"]), int(w["update_timestamp"])
            if now - upd < LAG_SEC:
                continue
            listed.append(ts)
            if have.get(ts, (-1, None))[0] >= upd:
                ok.add(ts)
                continue
            if time.monotonic() > deadline:
                continue
            try:
                text = _get(f"{BASE}/data?data_type={dtype}&stream_window_timestamp={ts}", token).decode("utf-8")
            except requests.HTTPError as e:
                # Самое старое окно истекает между status и скачиванием (29.10 мост упал на 404).
                if e.response is not None and e.response.status_code == 404:
                    continue
                raise
            save_window(dtype, ts, upd, text, have.get(ts, (0, None))[1])
            ok.add(ts)
            saved += 1
        rng = covered_range(listed, ok, load_range(dtype))
        if rng:
            with open(_state_path(dtype) + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(rng, fh)
            os.replace(_state_path(dtype) + ".tmp", _state_path(dtype))
        ranges[dtype] = rng
        print(f"  {dtype}: скачано окон {saved}, в спуле {_fmt_ts(rng)}", flush=True)
    return ranges


def _fmt_ts(rng: dict | None) -> str:
    if not rng:
        return "—"
    f = lambda t: datetime.fromtimestamp(t, _MSK).strftime("%d.%m %H:%M")
    return f"{f(rng['first'])}..{f(rng['last'])} МСК"


def prune(now: float) -> int:
    n = 0
    for dtype in DTYPES:
        for ts, (_, path) in spooled(dtype).items():
            if ts < now - KEEP_DAYS * 86400:
                os.remove(path)
                n += 1
    return n


# ── сборка дней ───────────────────────────────────────────────────────────────────────────

def day_start(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=_MSK).timestamp())


def complete_days(rng: dict | None, since: date, until: date) -> list[date]:
    """Дни, сутки приёма которых спул покрывает целиком (с запасом на разброс окна)."""
    if not rng:
        return []
    out = []
    d = since
    while d <= until:
        s = day_start(d)
        if rng["first"] <= s - MARGIN_SEC and rng["last"] >= s + 86400 + MARGIN_SEC:
            out.append(d)
        d += timedelta(days=1)
    return out


def built_today(conn, stream: str, since: date, until: date, today: date) -> set[date]:
    """Дни, уже собранные из DSA сегодня (МСК): их не пересобираем до завтра."""
    with conn.cursor() as cur:
        cur.execute("SELECT c.day FROM appm_coverage c JOIN appm_export_jobs j ON j.id = c.job_id "
                    "WHERE c.stream = %s AND c.day BETWEEN %s AND %s AND j.kind = 'dsa' "
                    "AND j.params->>'built_on' = %s", (stream, since, until, today.isoformat()))
        return {r[0] for r in cur.fetchall()}


def collapse_installs(rows):
    """Установка в DSA — версионная строка (sign/version, как CollapsingMergeTree): берётся
    последняя версия установки, отменённая (sign = −1) выпадает."""
    best: dict[str, dict] = {}
    for r in rows:
        key = r.get("installation_id") or f"{r.get('appmetrica_device_id')}|{r.get('install_datetime')}"
        if key not in best or int(r.get("version") or 0) >= int(best[key].get("version") or 0):
            best[key] = r
    return [r for r in best.values() if r.get("sign", "1") != "-1"]


def read_rows(feed: Feed, days: set[date], since_ts: int, until_ts: int | None = None):
    """Строки спула потока за дни days из окон [since_ts, until_ts]. → {день: [строки]}."""
    out: dict[date, list] = defaultdict(list)
    sess: dict[tuple[date, str], int] = defaultdict(int)
    for ts, (_, path) in sorted(spooled(feed.dtype).items()):
        if ts < since_ts or (until_ts is not None and ts > until_ts):
            continue
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                if feed.event_name and r.get("event_name") != feed.event_name:
                    continue
                v = r.get(feed.day_col) or ""
                if len(v) < 10:
                    continue
                d = date.fromisoformat(v[:10])
                if d not in days:
                    continue
                if feed.stream == "sessions":
                    if r.get("appmetrica_device_id"):
                        sess[(d, r["appmetrica_device_id"])] += 1
                else:
                    out[d].append(r)
    for (d, dev), n in sess.items():
        out[d].append({"appmetrica_device_id": dev, "session_start_datetime": d.isoformat(), "n": n})
    if feed.stream == "installs":
        for d in out:
            out[d] = collapse_installs(out[d])
    return out


def new_job(conn, stream: str, d: date, rng: dict, today: date) -> int:
    params = {"source": "dsa", "built_on": today.isoformat(), "windows": [rng["first"], rng["last"]]}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO appm_export_jobs (app_id, stream, kind, date_since, date_until, params, "
                    "status, requested_at) VALUES (%s, %s, 'dsa', %s, %s, %s, 'loaded', now()) RETURNING id",
                    (raw.APP_ID, stream, d, d, json.dumps(params)))
        return cur.fetchone()[0]


def build(conn, ranges: dict[str, dict | None], today: date) -> int:
    """Собрать закрывшиеся дни хвоста, ещё не собранные сегодня. → дней записано."""
    yesterday = today - timedelta(days=1)
    n = 0
    for feed in FEEDS:
        s = raw.STREAMS[feed.stream]
        rng = ranges.get(feed.dtype)
        days = complete_days(rng, today - timedelta(days=s.tail_days), yesterday)
        todo = sorted(set(days) - built_today(conn, feed.stream, days[0], days[-1], today)) if days else []
        if not todo:
            continue
        lo = day_start(todo[0]) - MARGIN_SEC
        # Покупки — по дате приёма: окна только своих суток; прочее — по дате события, до конца спула.
        hi = day_start(todo[-1]) + 86400 + MARGIN_SEC if feed.stream == "purchases" else None
        rows = read_rows(feed, set(todo), lo, hi)
        for d in todo:
            job = new_job(conn, feed.stream, d, rng, today)
            table_rows = raw.to_table_rows(s, rows.get(d, []), job, d, d)
            raw.store(conn, s, job, d, d, table_rows)
            print(f"  {feed.stream} {d}: записано {len(table_rows):,}", flush=True)
            n += 1
    return n


def today_msk() -> date:
    v = (os.environ.get("APPM_TODAY") or "").strip()
    return date.fromisoformat(v) if v else datetime.now(_MSK).date()


def main(argv: list[str]) -> int:
    if not os.environ.get("DATABASE_URL"):
        print("[appm-dsa] нет DATABASE_URL")
        return 1
    conn = raw.connect()
    try:
        if argv[:1] == ["ready"]:
            ok = raw.tails_loaded(conn, today_msk())
            print(f"[appm-dsa] вчера {'покрыто' if ok else 'НЕ покрыто'} всеми потоками", flush=True)
            return 0 if ok else 3
        token = os.environ.get("APPMETRICA_TOKEN")
        if not token:
            print("[appm-dsa] нет APPMETRICA_TOKEN")
            return 1
        budget = float(os.environ.get("APPM_DSA_MINUTES") or "40")
        now = time.time()
        ranges = fetch(token, now, time.monotonic() + budget * 60)
        n = build(conn, ranges, today_msk())
        print(f"[appm-dsa] дней собрано {n}, удалено старых окон {prune(now)}", flush=True)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
