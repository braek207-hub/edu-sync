# -*- coding: utf-8 -*-
"""Витрины разделов LIME, сайт: все пять таблиц одним днём за два запроса Logs API.

Хиты дня → аудитория раздела (каталог ∪ карточка, уникальные clientID).
Визиты дня → источник/канал/устройство/цели/кампании/покупки — один проход.

Из агрегатов дня собираются строки:
  lime_section_daily            (v1: день × раздел × источник-корзина)
  lime_section_daily_v2         (день × канал PROCONTEXT × устройство × раздел)
  lime_section_product_daily    (день × канал × раздел × тип товара)
  lime_section_cross_channel_daily (день × канал × посетил × купил, + revenue_split)
  lime_section_campaign_daily   (день × кампания Директ/VK × раздел)

В базу уходят только эти агрегаты — сырые логи нигде не сохраняются.
Оговорка инкремента: дубль заказа режется в пределах одного дня (в разовой
заливке — в пределах всей истории); повтор purchase id на другой день — редкая
ретрансляция Метрики, на неделях расхождение было нулевым.
"""
import ast
import re
from collections import defaultdict

from sync.lime_feedmap import slug_and_menu
from sync.lime_sections_common import (
    CART_GOAL, CH_IDX, CHECKOUT_GOAL, COUNTER, FOREIGN, SEC_ALIAS, SECTIONS_V1,
    SECTIONS_V2, SRC_PRIORITY, WEB_BUCKET, channel_of,
)
from sync.logs_api import clean_request, create_request, iter_part_lines, wait_processed

HITS_FIELDS = "ym:pv:clientID,ym:pv:date,ym:pv:URL"
VISIT_FIELDS = ",".join([
    "ym:s:clientID", "ym:s:date", "ym:s:startURL", "ym:s:lastsignTrafficSource",
    "ym:s:UTMSource", "ym:s:UTMMedium", "ym:s:UTMCampaign", "ym:s:lastsignDirectClickOrder",
    "ym:s:goalsID", "ym:s:productsPurchaseID", "ym:s:productsName", "ym:s:productsQuantity",
    "ym:s:productsPrice", "ym:s:purchaseCurrency", "ym:s:deviceCategory",
])
PROD_URL = re.compile(r"/product/\d+-([0-9a-z]+)_([0-9a-z]+)", re.I)
DEVICE = {"1": "desktop", "2": "mobile", "3": "tablet", "4": "tv"}


def _fetch_day(day: str, fields: str, source: str, token: str):
    """Генератор строк выгрузки одного дня. clean — всегда, даже при ошибке."""
    req_id = create_request(COUNTER, day, day, fields, token, source=source)
    try:
        parts = wait_processed(COUNTER, req_id, token, poll_s=20, max_polls=180)
        for part in parts:
            yield from iter_part_lines(COUNTER, req_id, part, token)
    finally:
        try:
            clean_request(COUNTER, req_id, token)
        except Exception as e:  # noqa: BLE001 — неубранный request съедает квоту, но день важнее
            print(f"  (clean_request {req_id} не удался: {e})")


def _audience(day: str, token: str, resolver):
    """Хиты дня → раздел → множество clientID (каталог раздела ∪ карточка товара)."""
    fm = resolver.fm
    sets = defaultdict(set)
    header = None
    for line in _fetch_day(day, HITS_FIELDS, "hits", token):
        if header is None:
            header = line.split("\t")
            ix = {n: i for i, n in enumerate(header)}
            C, URL = ix["ym:pv:clientID"], ix["ym:pv:URL"]
            continue
        p = line.split("\t")
        if len(p) <= max(C, URL) or not p[C].isdigit():
            continue
        url = p[URL]
        if "/ru_ru/" not in url:
            continue
        m = PROD_URL.search(url)
        if m:
            art = f"{m.group(1)}-{m.group(2)}".lower()
            g = resolver.artmap.get(art) or fm.gender_of_product(article=art)
            sets[g if g in SECTIONS_V1 else "unknown_product"].add(int(p[C]))
            continue
        slug, menu = slug_and_menu(url)
        if not slug:
            continue
        g = fm.gender_of_slug(slug, menu)
        if g in SECTIONS_V1:
            sets[g].add(int(p[C]))
    return dict(sets)


def build_web_day(day: str, token: str, resolver, with_hits: bool = True):
    """Считает все веб-агрегаты одного дня. Возвращает словарь списков строк
    в порядке колонок целевых таблиц (см. lime_sections_db.TABLES).

    with_hits=False — без аудитории (хиты не качаются): дешёвый режим для
    бэкфилла когорты, где нужны только визиты (клики+покупки). Витринные
    строки в этом режиме неполны и писаться в базу НЕ должны."""
    aud_raw = _audience(day, token, resolver) if with_hits else {}
    aud = defaultdict(set)          # v1: perfume остаётся, unknown_product → other
    for s, ids in aud_raw.items():
        aud[SEC_ALIAS.get(s, s)] |= ids
    all_ids = set().union(*aud.values()) if aud else set()

    # ── один проход по визитам дня ─────────────────────────────────────────
    src_of = {}                     # cid -> корзина v1 (приоритетная)
    ch_of = {}                      # cid -> канал PROCONTEXT (приоритетный)
    dev_sets = defaultdict(set)     # cid -> устройства дня
    camp_of = defaultdict(set)      # cid -> кампании (direct:<id> / vk:<utm>)
    cart, checkout = set(), set()
    order_seen = {}                 # oid -> (cid, positions)

    header = None
    for line in _fetch_day(day, VISIT_FIELDS, "visits", token):
        if header is None:
            header = line.split("\t")
            ix = {n: i for i, n in enumerate(header)}
            C, TS = ix["ym:s:clientID"], ix["ym:s:lastsignTrafficSource"]
            URL, GOALS = ix["ym:s:startURL"], ix["ym:s:goalsID"]
            PID, NAM = ix["ym:s:productsPurchaseID"], ix["ym:s:productsName"]
            QTY, PRC = ix["ym:s:productsQuantity"], ix["ym:s:productsPrice"]
            CUR, DEV = ix["ym:s:purchaseCurrency"], ix["ym:s:deviceCategory"]
            US, UM, UC = ix["ym:s:UTMSource"], ix["ym:s:UTMMedium"], ix["ym:s:UTMCampaign"]
            DO = ix["ym:s:lastsignDirectClickOrder"]
            wide = max(C, TS, URL, GOALS, PID, NAM, QTY, PRC, CUR, DEV, US, UM, UC, DO)
            continue
        p = line.split("\t")
        if len(p) <= wide or not p[C].isdigit() or FOREIGN.search(p[URL]):
            continue
        cid = int(p[C])

        b = WEB_BUCKET.get(p[TS].strip(), "unknown")
        if SRC_PRIORITY[b] < SRC_PRIORITY.get(src_of.get(cid), 99):
            src_of[cid] = b
        ch = channel_of(p[TS], p[US], p[UM], p[DO])
        if CH_IDX[ch] < CH_IDX.get(ch_of.get(cid), 99):
            ch_of[cid] = ch
        dev_sets[cid].add(DEVICE.get(p[DEV], p[DEV] or "?"))

        do = p[DO].strip()
        if do and do not in ("0", "-1"):
            camp_of[cid].add("direct:" + do)
        elif ch == "SMM paid" and p[UC].strip():
            camp_of[cid].add("vk:" + p[UC].strip()[:64])

        goals = p[GOALS]
        if CART_GOAL in goals:
            cart.add(cid)
        if CHECKOUT_GOAL in goals:
            checkout.add(cid)

        if p[PID] == "[]" or not p[PID]:
            continue
        try:
            pids = ast.literal_eval(p[PID])
            names = ast.literal_eval(p[NAM])
            qty = ast.literal_eval(p[QTY])
            prc = ast.literal_eval(p[PRC])
            curs = ast.literal_eval(p[CUR]) if p[CUR] not in ("", "[]") else []
        except (ValueError, SyntaxError):
            continue
        cur_of = {str(o): str(c).strip() for o, c in zip(pids, curs)}
        by_order = defaultdict(list)
        for oid, nm, q, pr in zip(pids, names, qty, prc):
            # Только рублёвые заказы: казахстанская витрина шлёт тенге на том же домене.
            if cur_of.get(str(oid), "RUB") != "RUB":
                continue
            by_order[str(oid)].append((nm, q, pr))
        for oid, positions in by_order.items():
            order_seen.setdefault(oid, (cid, positions))

    # устройство дня: одно → оно, несколько → multi (без произвольного выбора)
    device_of = {cid: (next(iter(v)) if len(v) == 1 else "multi") for cid, v in dev_sets.items()}

    # ── v1: заказы по разделам и покупатели ────────────────────────────────
    buyers = defaultdict(set)       # раздел -> клиенты
    orders_v1 = defaultdict(set)    # раздел -> заказы
    items_v1 = defaultdict(float)
    revenue_v1 = defaultdict(float)
    for oid, (cid, positions) in order_seen.items():
        for nm, q, pr in positions:
            g = resolver.label_name(nm)
            buyers[g].add(cid)
            orders_v1[g].add(oid)
            items_v1[g] += float(q or 0)
            revenue_v1[g] += float(q or 0) * float(pr or 0) / 1e6

    v1 = defaultdict(lambda: defaultdict(float))    # (sec, src) -> меры
    for s, ids in aud.items():
        by_src = defaultdict(set)
        for cid in ids:
            by_src[src_of.get(cid, "unknown")].add(cid)
        for src, sub in by_src.items():
            a = v1[(s, src)]
            a["dau"] += len(sub)
            # Корзину и оформление к разделу привязывает только аудитория: цель общая,
            # товара в ней нет.
            a["cart_users"] += len(sub & cart)
            a["checkout_users"] += len(sub & checkout)
    for s, allb in buyers.items():
        if not allb:
            continue
        # Покупатели — все за день, не только сегодняшняя аудитория: часть покупает
        # из корзины, набранной раньше. Заказы/штуки/деньги делятся по доле покупателей.
        b_src = defaultdict(set)
        for cid in allb:
            b_src[src_of.get(cid, "unknown")].add(cid)
        for src, bset in b_src.items():
            share = len(bset) / len(allb)
            a = v1[(s, src)]
            a["buy_users"] += len(bset)
            a["orders"] += len(orders_v1[s]) * share
            a["items"] += items_v1[s] * share
            a["revenue"] += revenue_v1[s] * share

    # ── v2: аудитория по каналам/устройствам + кампании ───────────────────
    per_cid_secs = defaultdict(set)
    for g in SECTIONS_V2:
        for cid in aud_raw.get(g, ()):
            per_cid_secs[cid].add(g)
    daily_v2 = defaultdict(lambda: defaultdict(float))  # (ch, dev, sec)
    camp_aud = defaultdict(set)                          # (ch, camp, sec) -> клиенты
    for cid, gs in per_cid_secs.items():
        ch = ch_of.get(cid, "Others")
        dev = device_of.get(cid, "all")
        in_cart, in_checkout = cid in cart, cid in checkout
        for g in gs:
            a = daily_v2[(ch, dev, g)]
            a["dau"] += 1
            if in_cart:
                a["cart"] += 1
            if in_checkout:
                a["checkout"] += 1
        # campaign='' — остаток канала без кампании: единая грань канал × кампания,
        # по каналам сходится без задвоения (клиент ровно в одной строке грани).
        for cmp_ in (camp_of.get(cid) or ("",)):
            for g in gs:
                camp_aud[(ch, cmp_, g)].add(cid)

    # ── v2: покупки → тип товара, кросс, кампании ─────────────────────────
    product = defaultdict(lambda: [set(), set(), 0.0, 0.0])     # (ch, sec, type)
    cross = defaultdict(lambda: [set(), set(), 0.0, 0.0, 0.0])  # (ch, visited, bought)
    camp_buy = defaultdict(lambda: [set(), set(), 0.0, 0.0])    # (camp, sec)
    camp_type = defaultdict(lambda: [set(), set(), 0.0, 0.0])   # (camp, sec, type)
    camp_cross = defaultdict(lambda: [set(), set(), 0.0, 0.0, 0.0])  # (camp, visited, bought)
    cohort_orders = []      # (cid, {sec: [items, revenue]}) на заказ — вход когорты
    nb_orders = []          # (cid, канал, кампании, {sec: [шт, деньги]}) — вход новых/повторных
    for oid, (cid, positions) in order_seen.items():
        ch = ch_of.get(cid, "Others")
        visited = sorted(g for g in SECTIONS_V2 if cid in aud_raw.get(g, ())) or ["none"]
        by_sec = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
        for nm, q, pr in positions:
            cell = by_sec[resolver.label_v2(nm)][resolver.fm.type_of_name(nm)]
            cell[0] += float(q or 0)
            cell[1] += float(q or 0) * float(pr or 0) / 1e6
        sec_totals = {
            b: [sum(t[0] for t in types.values()), sum(t[1] for t in types.values())]
            for b, types in by_sec.items()
        }
        cohort_orders.append((cid, sec_totals))
        nb_orders.append((cid, ch, tuple(camp_of.get(cid) or ("",)), sec_totals))
        for b, types in by_sec.items():
            sec_items = sum(t[0] for t in types.values())
            sec_rev = sum(t[1] for t in types.values())
            for tp, (its, rev) in types.items():
                k = product[(ch, b, tp)]
                k[0].add(cid)
                k[1].add(oid)
                k[2] += its
                k[3] += rev
            for v in visited:
                k = cross[(ch, v, b)]
                k[0].add(cid)
                k[1].add(oid)
                k[2] += sec_items
                k[3] += sec_rev
                k[4] += sec_rev / len(visited)
            for cmp_ in (camp_of.get(cid) or ("",)):
                k = camp_buy[(ch, cmp_, b)]
                k[0].add(cid)
                k[1].add(oid)
                k[2] += sec_items
                k[3] += sec_rev
                for tp, (its, rev) in types.items():
                    kt = camp_type[(ch, cmp_, b, tp)]
                    kt[0].add(cid)
                    kt[1].add(oid)
                    kt[2] += its
                    kt[3] += rev
                for v in visited:
                    kc = camp_cross[(ch, cmp_, v, b)]
                    kc[0].add(cid)
                    kc[1].add(oid)
                    kc[2] += sec_items
                    kc[3] += sec_rev
                    kc[4] += sec_rev / len(visited)

    # ── строки таблиц ──────────────────────────────────────────────────────
    r3 = lambda x: round(x, 3)  # noqa: E731
    rows_v1 = [
        (day, "web", s, src, int(m["dau"]), 0, int(m["cart_users"]), int(m["checkout_users"]),
         int(m["buy_users"]), r3(m["orders"]), r3(m["items"]), 0, r3(m["revenue"]))
        for (s, src), m in sorted(v1.items())
    ]
    rows_daily_v2 = [
        (day, "web", ch, dev, s, int(m["dau"]), 0, int(m["cart"]), int(m["checkout"]))
        for (ch, dev, s), m in sorted(daily_v2.items())
    ]
    rows_product = [
        (day, "web", ch, s, tp, len(b), len(o), round(its, 2), round(rev, 2))
        for (ch, s, tp), (b, o, its, rev) in sorted(product.items())
    ]
    rows_cross = [
        (day, "web", ch, v, b, len(bu), len(o), round(its, 2), round(rev, 2), round(sp, 2))
        for (ch, v, b), (bu, o, its, rev, sp) in sorted(cross.items())
    ]
    rows_campaign = [
        (day, "web", ch, c, s, len(camp_aud.get((ch, c, s), ())),
         len(camp_aud.get((ch, c, s), set()) & cart), len(b), len(o),
         round(its, 2), round(rev, 2))
        for (ch, c, s), (b, o, its, rev) in sorted(camp_buy.items())
    ]
    # кампании с аудиторией без покупок тоже нужны (dau без строк покупок)
    for (ch, c, s), ids in sorted(camp_aud.items()):
        if (ch, c, s) not in camp_buy:
            rows_campaign.append(
                (day, "web", ch, c, s, len(ids), len(ids & cart), 0, 0, 0.0, 0.0))
    rows_camp_type = [
        (day, "web", ch, c, sec, tp, len(b), len(o), round(its, 2), round(rev, 2))
        for (ch, c, sec, tp), (b, o, its, rev) in sorted(camp_type.items())
    ]
    rows_camp_cross = [
        (day, "web", ch, c, v, b, len(bu), len(o), round(its, 2), round(rev, 2), round(sp, 2))
        for (ch, c, v, b), (bu, o, its, rev, sp) in sorted(camp_cross.items())
    ]

    print(f"  web {day}: аудитория {len(all_ids):,}, заказов {len(order_seen):,}, "
          f"строк v1/{len(rows_v1)} v2/{len(rows_daily_v2)} prod/{len(rows_product)} "
          f"cross/{len(rows_cross)} camp/{len(rows_campaign)}")
    # Вход когорты (фаза 2): платный клик дня клиента — одна кампания с
    # приоритетом Директ > VK (стабильный выбор ради идемпотентности стейта).
    paid_clicks = {}
    for cid, camps in camp_of.items():
        best = sorted(camps, key=lambda c: (0 if c.startswith("direct:") else 1, c))[0]
        paid_clicks[cid] = (best, "SEM" if best.startswith("direct:") else "SMM paid")

    # Атрибуция покупателей дня (канал + кампании|'' ) — для new_buyers.
    buyer_attr = {}
    for _oid, (cid, _positions) in order_seen.items():
        if cid not in buyer_attr:
            buyer_attr[cid] = (ch_of.get(cid, "Others"), sorted(camp_of.get(cid) or ("",)))

    return {
        "daily": rows_v1,
        "daily_v2": rows_daily_v2,
        "product": rows_product,
        "cross_channel": rows_cross,
        "campaign": rows_campaign,
        "campaign_type": rows_camp_type,
        "campaign_cross": rows_camp_cross,
        "cohort_input": {"clicks": paid_clicks, "orders": cohort_orders, "buyer_attr": buyer_attr},
        "nb_orders": nb_orders,
    }
