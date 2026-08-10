# -*- coding: utf-8 -*-
"""Витрины разделов LIME, приложение: события AppMetrica за день + атрибуция.

Атрибуция last-touch: диплинк не старше 90 дней → установка → «Без атрибуции».
Карта устройств строится заново на каждый запуск из выгрузок installations и
deeplinks за lookback-окно — в базе она НЕ хранится (только готовые агрегаты).

События дня (Logs API events.csv, стримом):
  view_item        — карточки: DAU/просмотры раздела, «посетил» для кросса
  section_tab_show — вкладка раздела: дополняет DAU v1 (как каталог на сайте)
  add_to_cart      — корзина: пользователи и штуки
  purchase         — покупки: v1 по корзинам источника, v2 по паблишеру + тип товара

Методика — точный порт билдеров разовой заливки (сессия 9028b704):
  · v1: разделы women/men/kids, парфюм и неопознанное → other; раздел позиции
    покупки — богатый резолвер (артикулы → фид → поведение веба);
  · v2: раздел покупки — ТОЛЬКО по артикулу (как в build_app_v2), тип — из фида.
Обрыв стрима = повтор всего дня (частично прочитанный день занижает множества).
Дубль purchase transaction_id режется в пределах запускаемого окна.
"""
import bisect
import csv
import json
import os
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests

from sync.lime_sections_common import (
    APPMETRICA_APP, DEEPLINK_LOOKBACK_DAYS, SECTIONS_V2, TABMAP, bucket, pub_norm,
)

BASE = "https://api.appmetrica.yandex.ru/logs/v1/export"
ART = re.compile(r'"item_article"\s*:\s*"([^"]+)"')
# Установки тянутся с запуска приложения: last-touch падает на установку, когда
# диплинков в lookback-окне нет, а установка может быть сколь угодно старой.
# Диплинки — только за lookback (90 дней) от начала окна.
INSTALL_HISTORY_START = date(2024, 8, 1)

csv.field_size_limit(10 ** 8)


def _ts(s: str) -> datetime:
    return datetime.strptime((s or "")[:19], "%Y-%m-%d %H:%M:%S")


def _export_json(endpoint: str, params: dict, token: str) -> list:
    """Выгрузка .json c ожиданием готовности (202/429 → повтор).

    Очередь экспорта AppMetrica под нагрузкой держит месячный запрос установок
    дольше 40 минут — ждём до ~2 часов (240×30с), прогресс в лог каждые 10 минут."""
    attempts = int(os.environ.get("APPMETRICA_EXPORT_ATTEMPTS", "240"))
    for attempt in range(attempts):
        r = requests.get(f"{BASE}/{endpoint}.json", params=params,
                         headers={"Authorization": f"OAuth {token}"}, timeout=900)
        if r.status_code == 200:
            return r.json().get("data", [])
        if r.status_code not in (202, 429):
            raise RuntimeError(f"{endpoint} HTTP {r.status_code}: {r.text[:200]}")
        r.close()
        if attempt and attempt % 20 == 0:
            print(f"  {endpoint}: ждём готовности выгрузки, попытка {attempt}/{attempts}")
        time.sleep(30)
    raise TimeoutError(endpoint)


def _months(a, b):
    cur = a.replace(day=1)
    while cur <= b:
        nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield max(cur, a), min(nxt - timedelta(days=1), b)
        cur = nxt


class Attribution:
    """device → паблишер last-touch на момент времени. Держит сырые имена
    паблишеров; корзина v1 получается через bucket()."""

    def __init__(self, token: str, date_from, date_to):
        self.inst = {}
        dl = defaultdict(list)
        for a, b in _months(INSTALL_HISTORY_START, date_to):
            data = _export_json("installations", {
                "application_id": APPMETRICA_APP,
                "date_since": f"{a} 00:00:00", "date_until": f"{b} 23:59:59",
                "date_dimension": "default",
                "fields": "appmetrica_device_id,install_datetime,publisher_name"}, token)
            for r in data:
                dev = r.get("appmetrica_device_id")
                if dev:
                    self.inst[dev] = (_ts(r["install_datetime"]), pub_norm(r.get("publisher_name")))
            print(f"  установки {a:%Y-%m}: +{len(data):,}")
        for a, b in _months(date_from - timedelta(days=DEEPLINK_LOOKBACK_DAYS), date_to):
            data2 = _export_json("deeplinks", {
                "application_id": APPMETRICA_APP,
                "date_since": f"{a} 00:00:00", "date_until": f"{b} 23:59:59",
                "date_dimension": "default",
                "fields": "appmetrica_device_id,event_datetime,publisher_name,tracker_name"}, token)
            for r in data2:
                dev = r.get("appmetrica_device_id")
                if dev:
                    dl[dev].append((_ts(r["event_datetime"]), pub_norm(r.get("publisher_name"))))
            print(f"  диплинки {a:%Y-%m}: +{len(data2):,}")
        for v in dl.values():
            v.sort()
        self.dl = dict(dl)
        self.dl_keys = {d: [t for t, _ in v] for d, v in self.dl.items()}
        print(f"  атрибуция готова: установок {len(self.inst):,}, устройств с диплинком {len(self.dl):,}")

    def publisher(self, dev: str, when: datetime) -> str:
        v = self.dl.get(dev)
        if v:
            i = bisect.bisect_right(self.dl_keys[dev], when) - 1
            if i >= 0 and (when - v[i][0]).days <= DEEPLINK_LOOKBACK_DAYS:
                return v[i][1]
        r = self.inst.get(dev)
        if r and r[0] <= when:
            return r[1]
        return "Без атрибуции"

    def source(self, dev: str, when: datetime) -> str:
        pub = self.publisher(dev, when)
        return bucket("" if pub == "Без атрибуции" else pub)


def _stream_event(day: str, event: str, token: str):
    """Стрим events.csv одного события за день; обрыв → исключение (день повторяется)."""
    params = {"application_id": APPMETRICA_APP,
              "date_since": f"{day} 00:00:00", "date_until": f"{day} 23:59:59",
              "date_dimension": "receive", "event_name": event,
              "fields": "appmetrica_device_id,event_datetime,event_json"}
    r = None
    for _ in range(120):
        try:
            r = requests.get(f"{BASE}/events.csv", params=params,
                             headers={"Authorization": f"OAuth {token}"},
                             timeout=1800, stream=True)
        except requests.RequestException:
            time.sleep(20)
            continue
        if r.status_code == 200:
            break
        if r.status_code not in (202, 429):
            raise RuntimeError(f"{day}/{event}: HTTP {r.status_code} {r.text[:150]}")
        r.close()
        time.sleep(20)
    if r is None or r.status_code != 200:
        raise RuntimeError(f"{day}/{event}: не дождались 200")
    try:
        lines = (ln.decode("utf-8", "replace") for ln in r.iter_lines() if ln)
        reader = csv.reader(lines)
        next(reader, None)
        yield from reader
    finally:
        r.close()


def _day_attempts(day: str, event: str, token: str, handler, attempts: int = 10):
    """Повтор всего дня при обрыве стрима: handler получает свежие аккумуляторы."""
    for attempt in range(attempts):
        try:
            return handler(_stream_event(day, event, token))
        except (requests.RequestException, ConnectionError, OSError):
            print(f"  {day}/{event}: стрим оборвался, повтор {attempt + 1}")
            time.sleep(30)
    raise RuntimeError(f"{day}/{event}: стрим не дочитан за {attempts} попыток")


def build_app_day(day: str, token: str, resolver, attr: Attribution, seen_tx: set):
    """Все агрегаты приложения за день. seen_tx — дедуп заказов в пределах окна."""

    def fold(g: str) -> str:
        """Раздел v1 приложения: women/men/kids, остальное (включая парфюм) → other."""
        return g if g in SECTIONS_V2 else "other"

    # ── view_item: просмотры, DAU, «посетил» ───────────────────────────────
    def read_views(rows):
        views = defaultdict(float)          # (sec-fold, src) -> просмотров
        devs_v1 = defaultdict(set)          # sec (women/men/kids) -> devices
        devviews = defaultdict(lambda: defaultdict(float))  # dev -> sec -> просмотров (v2)
        last = {}                           # dev -> время последнего события
        for row in rows:
            if len(row) < 3:
                continue
            dev, when_s, blob = row[0], row[1], row[2]
            m = ART.search(blob)
            if not m:
                continue
            g = resolver.section_of_article(m.group(1))
            when = _ts(when_s)
            views[(fold(g), attr.source(dev, when))] += 1
            if g in SECTIONS_V2:
                devs_v1[g].add(dev)
                devviews[dev][g] += 1
                if dev not in last or when > last[dev]:
                    last[dev] = when
        return views, devs_v1, devviews, last

    views_src, devs_v1, devviews, last = _day_attempts(day, "view_item", token, read_views)

    # ── section_tab_show: вкладка раздела дополняет DAU v1 ────────────────
    def read_tabs(rows):
        devs = defaultdict(set)
        tab_last = {}
        for row in rows:
            if len(row) < 3:
                continue
            dev, when_s, blob = row[0], row[1], row[2]
            low = blob.lower()
            g = next((v for k, v in TABMAP.items() if f'"{k}"' in low), None)
            if g is None:
                continue
            devs[g].add(dev)
            when = _ts(when_s)
            if dev not in tab_last or when > tab_last[dev]:
                tab_last[dev] = when
        return devs, tab_last

    tab_devs, tab_last = _day_attempts(day, "section_tab_show", token, read_tabs)
    for dev, when in tab_last.items():
        if dev not in last or when > last[dev]:
            last[dev] = when

    # ── add_to_cart ────────────────────────────────────────────────────────
    def read_cart(rows):
        cart_items = defaultdict(float)     # (sec-fold, src) -> штук
        cart_users = defaultdict(set)       # (sec women/men/kids, src) -> devices
        for row in rows:
            if len(row) < 3:
                continue
            dev, when_s = row[0], row[1]
            try:
                j = json.loads(row[2] or "{}")
            except ValueError:
                continue
            src = attr.source(dev, _ts(when_s))
            for it in (j.get("items") or [j]):
                g = resolver.gender_of_item(it)
                cart_items[(fold(g), src)] += float(it.get("quantity") or 1)
                if g in SECTIONS_V2:
                    cart_users[(g, src)].add(dev)
        return cart_items, cart_users

    cart_items, cart_users = _day_attempts(day, "add_to_cart", token, read_cart)

    # ── purchase ───────────────────────────────────────────────────────────
    def read_purchases(rows):
        out = []
        for row in rows:
            if len(row) < 3:
                continue
            try:
                j = json.loads(row[2] or "{}")
            except ValueError:
                continue
            out.append((row[0], row[1], j))
        return out

    purchases = _day_attempts(day, "purchase", token, read_purchases)

    v1 = defaultdict(lambda: defaultdict(float))            # (sec-fold, src) -> меры
    buy_users = defaultdict(set)
    prod = defaultdict(lambda: [set(), set(), 0.0, 0.0])    # (ch, sec, type)
    cross = defaultdict(lambda: [set(), set(), 0.0, 0.0, 0.0])  # (ch, visited, bought)
    for dev, when_s, j in purchases:
        tx = j.get("transaction_id")
        if tx is not None:
            if str(tx) in seen_tx:
                continue
            seen_tx.add(str(tx))
        when = _ts(when_s)
        src = attr.source(dev, when)
        ch = attr.publisher(dev, when)
        vis = sorted(g for g in SECTIONS_V2 if devviews.get(dev, {}).get(g)) or ["none"]
        touched = set()
        by_sec = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
        for it in (j.get("items") or []):
            qy = float(it.get("quantity") or 1)
            rv = float(it.get("price") or 0) * qy
            # v1: богатый резолвер раздела (как app_daily разовой заливки)
            g1 = fold(resolver.gender_of_item(it))
            k = (g1, src)
            v1[k]["items"] += qy
            v1[k]["revenue"] += rv
            touched.add(k)
            if g1 != "other":
                buy_users[k].add(dev)
            # v2: раздел только по артикулу, тип — из фида (как build_app_v2)
            art = (it.get("item_article") or "").strip()
            g2 = fold(resolver.section_of_article(art))
            cell = by_sec[g2][resolver.fm.type_of_article(art)]
            cell[0] += qy
            cell[1] += rv
        for k in touched:
            v1[k]["orders"] += 1
        tx_id = str(tx) if tx is not None else f"{dev}:{when_s}"
        for b, types in by_sec.items():
            sec_items = sum(t[0] for t in types.values())
            sec_rev = sum(t[1] for t in types.values())
            for tp, (its, rev) in types.items():
                cell = prod[(ch, b, tp)]
                cell[0].add(dev)
                cell[1].add(tx_id)
                cell[2] += its
                cell[3] += rev
            for v in vis:
                cell = cross[(ch, v, b)]
                cell[0].add(dev)
                cell[1].add(tx_id)
                cell[2] += sec_items
                cell[3] += sec_rev
                cell[4] += sec_rev / len(vis)

    # ── сведение v1 ────────────────────────────────────────────────────────
    # DAU раздела = устройства с карточкой ∪ вкладкой; источник — на момент
    # ПОСЛЕДНЕГО события устройства за день (как в mk_app_audience).
    for g in set(devs_v1) | set(tab_devs):
        by_src = defaultdict(int)
        for dev in devs_v1.get(g, set()) | tab_devs.get(g, set()):
            by_src[attr.source(dev, last[dev])] += 1
        for src, n in by_src.items():
            v1[(g, src)]["dau"] += n
    for (g, src), n in views_src.items():
        v1[(g, src)]["views"] += n
    for (g, src), n in cart_items.items():
        v1[(g, src)]["cart_items"] += n
    for (g, src), devs in cart_users.items():
        v1[(g, src)]["cart_users"] += len(devs)
    for (g, src), devs in buy_users.items():
        v1[(g, src)]["buy_users"] += len(devs)

    # ── v2 daily: канал = паблишер на полдень дня ──────────────────────────
    noon = datetime.fromisoformat(day + "T12:00:00")
    daily_v2 = defaultdict(lambda: [set(), 0.0])   # (ch, sec) -> devices, просмотров
    for dev, secviews in devviews.items():
        ch = attr.publisher(dev, noon)
        for g, n in secviews.items():
            daily_v2[(ch, g)][0].add(dev)
            daily_v2[(ch, g)][1] += n

    rows_v1 = [
        (day, "app", s, src, int(m["dau"]), int(m["views"]), int(m["cart_users"]), 0,
         int(m["buy_users"]), round(m["orders"], 3), round(m["items"], 3),
         round(m["cart_items"], 3), round(m["revenue"], 2))
        for (s, src), m in sorted(v1.items())
    ]
    rows_daily_v2 = [
        (day, "app", ch, "app", s, len(d), int(round(n)), None, None)
        for (ch, s), (d, n) in sorted(daily_v2.items())
    ]
    rows_product = [
        (day, "app", ch, s, tp, len(b), len(o), round(its, 2), round(rev, 2))
        for (ch, s, tp), (b, o, its, rev) in sorted(prod.items())
    ]
    rows_cross = [
        (day, "app", ch, v, b, len(bu), len(o), round(its, 2), round(rev, 2), round(sp, 2))
        for (ch, v, b), (bu, o, its, rev, sp) in sorted(cross.items())
    ]
    print(f"  app {day}: устройств с карточками {len(devviews):,}, "
          f"строк v1/{len(rows_v1)} v2/{len(rows_daily_v2)} prod/{len(rows_product)} cross/{len(rows_cross)}")
    return {
        "daily": rows_v1,
        "daily_v2": rows_daily_v2,
        "product": rows_product,
        "cross_channel": rows_cross,
    }
