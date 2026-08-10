# -*- coding: utf-8 -*-
"""Справочник разделов и типов товара из YML-фида LIME.

Порт feedmap.py из разбора разделов (сессия 9028b704): раздел (women/men/kids/
perfume) опознаётся по menu_id категории, названию, артикулу и id оффера; тип
товара («Брюки», «Поло»…) — по typePrefix фида. Фид качается заново на каждый
запуск: ассортимент меняется, а карта весит ~14 МБ и в репо ей не место.
"""
import re
import time
import xml.etree.ElementTree as ET

import requests

FEED_URL = "https://limestore.com/storage/feed_yandex.xml"
ROOTS = {1001: "women", 1002: "men", 1003: "kids", 1010: "perfume"}
GENDERS = ("women", "men", "kids")


def fetch_feed(dest: str, url: str = FEED_URL) -> str:
    """Скачивает фид с ретраями, возвращает путь. Фид публичный, токен не нужен."""
    for attempt in range(6):
        try:
            r = requests.get(url, timeout=300)
            if r.status_code == 200 and r.content.startswith(b"<?xml"):
                with open(dest, "wb") as f:
                    f.write(r.content)
                return dest
            raise RuntimeError(f"feed HTTP {r.status_code}")
        except (requests.RequestException, RuntimeError) as e:
            if attempt == 5:
                raise
            print(f"  фид: попытка {attempt + 1} не удалась ({e}), повтор")
            time.sleep(15 * (attempt + 1))
    raise RuntimeError("unreachable")


class FeedMap:
    """Раздел и тип товара по данным фида. Ключи опознания раздела:
    menu_id категории → артикул → id оффера → название (в порядке приоритета)."""

    def __init__(self, path: str):
        shop = ET.parse(path).getroot().find("shop")
        self.cat_name, self.parent = {}, {}
        for c in shop.find("categories"):
            cid = int(c.get("id"))
            self.cat_name[cid] = (c.text or "").strip()
            if c.get("parentId"):
                self.parent[cid] = int(c.get("parentId"))

        self.menu2gender = {c: self.root_of(c) for c in self.cat_name}
        self.name2gender, self.article2gender, self.id2gender = {}, {}, {}
        self.name2type, self.article2type = {}, {}
        for o in shop.find("offers"):
            cid = o.findtext("categoryId")
            g = self.root_of(int(cid)) if cid and cid.isdigit() else "?"
            tp = (o.findtext("typePrefix") or "").strip()
            art = (o.findtext("vendorCode") or "").strip()
            if art and tp:
                self.article2type.setdefault(art, tp)
            for f in ("model", "PrettyName", "name"):
                v = (o.findtext(f) or "").strip().lower()
                if not v:
                    continue
                if g != "?":
                    self.name2gender.setdefault(v, g)
                if tp:
                    self.name2type.setdefault(v, tp)
            if g == "?":
                continue
            if art:
                self.article2gender.setdefault(art, g)
            oid = o.get("id")
            if oid:
                self.id2gender.setdefault(str(oid), g)

    def root_of(self, cid, depth=0):
        while cid in self.parent and depth < 20:
            cid = self.parent[cid]
            depth += 1
        return ROOTS.get(cid, "?")

    def gender_of_slug(self, slug: str, menu_id=None) -> str:
        """Раздел страницы каталога: префикс слага, затем дерево фида по menu id."""
        for g in GENDERS:
            if slug.startswith(g + "_") or slug == g:
                return g
        if menu_id is not None:
            return self.menu2gender.get(int(menu_id), "?")
        return "?"

    def gender_of_product(self, name=None, article=None, item_id=None) -> str:
        """Раздел товара. Приоритет: артикул → id оффера → название."""
        if article and article in self.article2gender:
            return self.article2gender[article]
        if item_id is not None and str(item_id) in self.id2gender:
            return self.id2gender[str(item_id)]
        if name:
            return self.name2gender.get(name.strip().lower(), "?")
        return "?"

    def type_of_name(self, name: str) -> str:
        return self.name2type.get((name or "").strip().lower(), "Прочее")

    def type_of_article(self, article: str) -> str:
        return self.article2type.get((article or "").strip(), "Прочее")


def slug_and_menu(url: str):
    """Из URL каталога вытащить (slug, menu_id|None). Не каталог → (None, None)."""
    m = re.search(r"/catalog/([a-z0-9_]+)", url or "")
    if not m:
        return None, None
    menu = re.search(r"[?&]menu=(\d+)", url or "")
    return m.group(1), (int(menu.group(1)) if menu else None)
