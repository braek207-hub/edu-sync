# -*- coding: utf-8 -*-
"""sync/lime_app_install_source.py — сессии и покупки приложения ПО ДАТЕ СОБЫТИЯ в разрезе
источника и кампании УСТАНОВКИ устройства → lime_app_install_source_daily.

Зачем. Витрина PROCONTEXT подписывает сессии приложения без UTM источником установки
(AppMetrica, attribution UA): `vk-ads-(ex.-mytarget)` / `yandex.direct` + medium
`(not set)` — 79 и 105 тыс. сессий за месяц (замер 12.08–10.09.2026) без campaign_id:
на их стороне у этих строк кампании нет вообще. У нас в AppMetrica связь по девайсу есть:
установка (с кампанией — Директ по campaign_id трекера, VK по справочнику групп) → все
сессии и покупки этого устройства. Дашборд делит строку витрины на дочерние по кампании
установки пропорционально этой витрине; итог строки остаётся за PROCONTEXT.

Атрибуция устройства = его ПЕРВАЯ установка в окне (first_install_attribution из
sync/lime_appmetrica.py, те же флаги переатрибуции/переустановки). Установка старше окна
или без партнёра → publisher 'unknown', campaign_id '' — остаток «без кампании».

ENV: DATABASE_URL, APPMETRICA_TOKEN, APPMETRICA_APP_ID (4415407),
APP_SOURCE_DAYS (10) — сколько последних дней пересобирать, APP_SOURCE_FROM/TO — бэкфилл,
APP_SOURCE_INSTALL_MONTHS (12) — глубина установок для атрибуции,
APP_KEEP_REATTR (1) / APP_KEEP_REINSTALL (0) — как у основного синка.
Запуск: python -m sync.lime_app_install_source
"""
import os
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta

import psycopg2
import psycopg2.extras

from sync import appmetrica_raw as raw
from sync import data_versions
from sync.appmetrica_logs import app_country, fetch_installations, fetch_purchase_events, fetch_sessions, only_country
from sync.lime_appmetrica import (
    _truthy, first_install_attribution, load_vk_entity_map, parse_dt, purchase_facts,
    sync_window, vk_entity_map_unusable, vk_resolve_stats,
)

TABLE = "lime_app_install_source_daily"
COLUMNS = ("date", "publisher", "campaign_id", "sessions", "devices", "orders", "revenue")
CHUNK_DAYS = 3  # ~80 тыс. стартов сессий в день; 3 дня — четверть миллиона строк в памяти

DDL = (
    f"""CREATE TABLE IF NOT EXISTS {TABLE} (
      date        date NOT NULL,
      publisher   text NOT NULL,
      campaign_id text NOT NULL DEFAULT '',
      sessions    integer NOT NULL DEFAULT 0,
      devices     integer NOT NULL DEFAULT 0,
      orders      integer NOT NULL DEFAULT 0,
      revenue     numeric(14,2) NOT NULL DEFAULT 0,
      updated_at  timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (date, publisher, campaign_id))""",
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_campaign ON {TABLE} (campaign_id, date)",
)


def day_chunks(since: str, until: str, size: int = CHUNK_DAYS) -> list[tuple[str, str]]:
    start = datetime.strptime(since, "%Y-%m-%d").date()
    end = datetime.strptime(until, "%Y-%m-%d").date()
    out: list[tuple[str, str]] = []
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=size - 1), end)
        out.append((cur.isoformat(), nxt.isoformat()))
        cur = nxt + timedelta(days=1)
    return out


def _attr(first: dict[str, tuple], dev: str, day: date) -> tuple[str, str]:
    """Кампания установки для события дня `day`. События ДО дня установки — остаток
    «unknown»: у переатрибутированного клиента (VK-ретаргет) история до клика не кампании."""
    info = first.get(dev)
    if info is None or day < info[0].date():
        return "unknown", ""
    return info[1] or "unknown", info[3] or ""


def aggregate_sessions(raw_sessions: list[dict]) -> list[tuple]:
    """Сырые старты Logs API (appmetrica_device_id, session_start_datetime) → [(device, date, n)] —
    та же свёртка, что лежит в appm_sessions_day."""
    acc: dict[tuple[str, date], int] = defaultdict(int)
    for s in raw_sessions:
        dev = s.get("appmetrica_device_id")
        if dev:
            acc[(dev, parse_dt(s["session_start_datetime"]).date())] += 1
    return [(dev, d, n) for (dev, d), n in acc.items()]


def build_source_daily(first: dict[str, tuple], sessions: list[tuple],
                       purchases: list[tuple]) -> list[tuple]:
    """Строки COLUMNS за даты, встретившиеся в sessions/purchases.

    sessions — старты, свёрнутые по (device, date, n): aggregate_sessions или appm_sessions_day;
    purchases — факты purchase_facts (device, dt, txn, amount).
    """
    acc: dict[tuple[date, str, str], dict] = defaultdict(
        lambda: {"sessions": 0, "devices": set(), "orders": 0, "revenue": 0.0})
    for dev, d, n in sessions:
        pub, cid = _attr(first, dev, d)
        a = acc[(d, pub, cid)]
        a["sessions"] += n
        a["devices"].add(dev)
    for dev, dt, _txn, amount in purchases:
        pub, cid = _attr(first, dev, dt.date())
        a = acc[(dt.date(), pub, cid)]
        a["orders"] += 1
        a["revenue"] += amount
    return [
        (d, pub, cid, a["sessions"], len(a["devices"]), a["orders"], round(a["revenue"], 2))
        for (d, pub, cid), a in sorted(acc.items())
    ]


def _write(conn, since: str, until: str, rows: list[tuple]) -> None:
    with conn.cursor() as cur:
        table = raw.target_table(cur, TABLE)
        cur.execute(f"DELETE FROM {table} WHERE date >= %s AND date <= %s", (since, until))
        if rows:
            psycopg2.extras.execute_values(
                cur, f"INSERT INTO {table} ({', '.join(COLUMNS)}) VALUES %s", rows, page_size=1000)
        # Теневая копия (APPM_TABLE_SUFFIX) — только сверка, витрины её не читают.
        if table == TABLE:
            data_versions.bump_dates(cur, "lime", TABLE, [since, until] + [r[0] for r in rows], len(rows))
    conn.commit()


def _window(today: date) -> tuple[str, str]:
    frm = (os.environ.get("APP_SOURCE_FROM") or "").strip()
    to = (os.environ.get("APP_SOURCE_TO") or "").strip()
    if frm:
        return frm, to or (today - timedelta(days=1)).isoformat()
    days = int(os.environ.get("APP_SOURCE_DAYS") or "10")
    return (today - timedelta(days=days)).isoformat(), (today - timedelta(days=1)).isoformat()


def sync_lime_app_install_source() -> int:
    # С Y1 сырьё — из appm_* (sync.appmetrica_raw); APPM_SOURCE=api — откат на Logs API.
    use_db = raw.source() == "db"
    token = os.environ.get("APPMETRICA_TOKEN")
    if not use_db and not token:
        print("[lime-app-source] APPMETRICA_TOKEN не задан — пропуск")
        return 0
    app_id = os.environ.get("APPMETRICA_APP_ID") or "4415407"
    event_name = os.environ.get("APPMETRICA_EVENT_NAME") or "purchase"
    keep_reattr = _truthy(os.environ.get("APP_KEEP_REATTR") or "1")
    keep_reinstall = _truthy(os.environ.get("APP_KEEP_REINSTALL") or "0")
    install_months = int(os.environ.get("APP_SOURCE_INSTALL_MONTHS") or "12")

    today = raw.today() if use_db else date.today()
    since, until = _window(today)
    inst_since, inst_until = sync_window(install_months, today)
    if use_db:
        inst_until = (today - timedelta(days=1)).isoformat()
    country = app_country()
    print(f"[lime-app-source] окно {since}..{until}, установки {inst_since}..{inst_until}, "
          f"app={app_id}, страна покупок={country or 'все'}")

    entity_map = load_vk_entity_map()
    if use_db:
        with closing(raw.connect()) as c:
            installs_raw = raw.read_installations(c, inst_since, inst_until)
    else:
        installs_raw = fetch_installations(app_id, token, inst_since, inst_until)
    vk_total, vk_with_c, vk_resolved = vk_resolve_stats(installs_raw, entity_map)
    print(f"[lime-app-source] установок {len(installs_raw)}; VK {vk_total}, с `c` {vk_with_c}, "
          f"резолвнулось {vk_resolved}")
    if vk_entity_map_unusable(vk_with_c, vk_resolved):
        raise RuntimeError("[lime-app-source] справочник lime_vk_entities пуст/протух — отказ от записи")
    first = first_install_attribution(installs_raw, keep_reattr, keep_reinstall, entity_map)
    del installs_raw
    if not first:
        raise RuntimeError("[lime-app-source] нет установок за окно атрибуции — отказ от записи")

    conn = psycopg2.connect(os.environ["DATABASE_URL"].split("?")[0], connect_timeout=30)
    total = 0
    try:
        with conn.cursor() as cur:
            for ddl in DDL:
                cur.execute(ddl)
        conn.commit()
        for c_since, c_until in day_chunks(since, until):
            # Покупки по времени СОБЫТИЯ (не приёма): строки витрины — по дате покупки, чанк
            # переписывает ровно свои даты. Поздние события подхватит ежедневное окно 10 дней.
            # Из сырья: событие дня могло прийти позже — read_purchase_events требует покрытие
            # по дате приёма до вчера, иначе RawNotReady и витрина не пишет ничего.
            if use_db:
                sessions = raw.read_sessions_day(conn, c_since, c_until)
                events = raw.read_purchase_events(conn, c_since, c_until, "default")
            else:
                sessions = aggregate_sessions(fetch_sessions(app_id, token, c_since, c_until))
                events = fetch_purchase_events(app_id, token, c_since, c_until, event_name,
                                               date_dimension="default", country=bool(country))
            purchases = purchase_facts(only_country(events))
            del events
            rows = build_source_daily(first, sessions, purchases)
            outside = [r for r in rows if not (c_since <= r[0].isoformat() <= c_until)]
            if outside:
                raise RuntimeError(f"[lime-app-source] {c_since}..{c_until}: {len(outside)} строк "
                                   f"вне окна чанка (первая {outside[0][:3]}) — Logs API вернул чужие даты")
            if not sessions:
                raise RuntimeError(f"[lime-app-source] {c_since}..{c_until}: сессий 0 — "
                                   f"пустой ответ Logs API, день не переписываем")
            _write(conn, c_since, c_until, rows)
            attributed = sum(r[3] for r in rows if r[1] != "unknown")
            print(f"[lime-app-source] {c_since}..{c_until}: сессий={sum(r[3] for r in rows)}, "
                  f"атрибутировано={attributed}, покупок={len(purchases)}, строк={len(rows)}", flush=True)
            total += len(rows)
            del sessions, purchases
    finally:
        conn.close()
    print(f"[lime-app-source] записано строк: {total}")
    return total


if __name__ == "__main__":
    sync_lime_app_install_source()
