# -*- coding: utf-8 -*-
"""Общие словари и резолверы витрин разделов LIME (веб + приложение).

Все константы — порт проверенных билдеров разбора разделов (сессия 9028b704),
числа сверены с продом при первичной заливке. Менять словари — только вместе
с пересчётом истории, иначе ряды в витринах разъедутся по методике.
"""
import json
import os
import re

# Счётчик Метрики сайта и цели корзины/оформления.
COUNTER = "23504302"
CART_GOAL, CHECKOUT_GOAL = "194380276", "340817822"
# Иностранные витрины на том же домене: не наш контур.
FOREIGN = re.compile(r"limestore\.com/(?!ru_ru/)[a-z]{2}_[a-z]{2}/")

APPMETRICA_APP = "4415407"
DEEPLINK_LOOKBACK_DAYS = 90

SECTIONS_V1 = ("women", "men", "kids", "perfume")   # витрина v1 (+other)
SECTIONS_V2 = ("women", "men", "kids")              # витрины v2 и кросс
SEC_ALIAS = {"unknown_product": "other", "?": "other"}

# ── v1: пять корзин источника, общих для сайта и приложения ────────────────
WEB_BUCKET = {"ad": "paid", "organic": "organic",
              "direct": "owned", "internal": "owned", "saved": "owned", "email": "owned",
              "referral": "other", "social": "other", "recommend": "other",
              "messenger": "other"}
SRC_PRIORITY = {"paid": 0, "organic": 1, "owned": 2, "other": 3, "unknown": 4}

# ── v2 веб: каналы номенклатуры PROCONTEXT ─────────────────────────────────
# Приоритет канала дня: платное дороже органики (якорь дня клиента).
CHANNEL_PRIORITY = ["SEM", "SMM paid", "Retargeting", "CRM", "SEO", "SMM (organic)",
                    "Referrals", "Direct", "Others"]
CH_IDX = {c: i for i, c in enumerate(CHANNEL_PRIORITY)}


def channel_of(traffic: str, utm_src: str, utm_med: str, direct_order: str) -> str:
    """Канал визита по словарю PROCONTEXT. Порядок правил важен."""
    src = (utm_src or "").strip().lower()
    med = (utm_med or "").strip().lower()
    if direct_order and direct_order.strip():
        return "SEM"
    if src in ("yandex", "ya_direct", "yandex_direct", "ya.direct", "ya_go", "ya_maps") and med in ("cpc", "cpm", "banner"):
        return "SEM"
    if src in ("google", "google_adwords", "google_ads", "adwords") and med in ("cpc", "cpm"):
        return "SEM"
    if src in ("vk", "vk_ads", "vkads", "vk-ads", "mytarget", "vk.com",
               "instagram", "ig", "facebook", "fb", "telegram", "tg") and med in (
            "cpc", "cpm", "social_cpc", "paid_social", "banner"):
        return "SMM paid"
    if src == "soloway" or "solow" in src:
        return "Retargeting"
    if "mindbox" in src or src in ("email", "edm", "e-mail", "newsletter") or med == "email":
        return "CRM"
    if src in ("telegram", "instagram", "vkontakte", "youtube", "tiktok", "zen_yandex", "dzen") and med not in ("cpc", "cpm"):
        return "SMM (organic)"
    if traffic == "organic":
        return "SEO"
    if traffic == "social":
        return "SMM (organic)"
    if traffic in ("referral", "recommend"):
        return "Referrals"
    if traffic == "direct":
        return "Direct"
    if traffic == "ad":
        # рекламный переход без знакомой метки — SEM по source Метрики
        return "SEM"
    return "Others"


# ── приложение: паблишеры и их корзины v1 ──────────────────────────────────
PAID_PUB = {"Yandex.Direct", "Yandex.Direct Auto-Tracking", "VK Ads (ex. myTarget)", "myTarget",
            "VKAds_custom", "Google Ads", "Facebook", "Turbotarget", "Huawei Ads",
            "Apple Search Ads", "Unknown (source: Advertisement Engine)"}
ORGANIC_PUB = {"Yandex Search", "Google Search"}
OWNED_PUB = {"Website", "Mindbox", "SMM", "Email", "Push", "Retail", "E-com", "PR", "CS bot",
             "in-app QR - Presale banner"}
# Вкладки разделов в событиях приложения.
TABMAP = {"woman": "women", "women": "women", "man": "men", "men": "men",
          "kids": "kids", "kid": "kids", "child": "kids"}


def bucket(pub: str) -> str:
    """Паблишер приложения → корзина источника v1 (пустой = органика, как в отчёте)."""
    pub = (pub or "").strip()
    if not pub or pub in ORGANIC_PUB:
        return "organic"
    if pub in PAID_PUB:
        return "paid"
    return "owned" if pub in OWNED_PUB else "other"


def pub_norm(pub: str) -> str:
    """Паблишер как канал v2 приложения; пусто — «Без атрибуции»."""
    return (pub or "").strip() or "Без атрибуции"


# ── раздел товара: фид → карта артикулов → поведенческая карта ─────────────
_CFG_DIR = os.path.join(os.path.dirname(__file__), "..", "config", "lime_sections")


class SectionResolver:
    """Опознание раздела/типа товара. Статичные карты (артикулы из истории и
    поведенческий мост по названию) дополняют свежий фид: они закрывают товары,
    выпавшие из ассортимента, новинки опознаёт сам фид."""

    def __init__(self, feedmap):
        self.fm = feedmap
        with open(os.path.join(_CFG_DIR, "article_map.json"), encoding="utf-8") as f:
            self.artmap = json.load(f)
        with open(os.path.join(_CFG_DIR, "behavior_map.json"), encoding="utf-8") as f:
            self.behavior = json.load(f)["behavior"]

    def label_name(self, name: str) -> str:
        """Раздел по названию товара (веб-заказы): фид → поведенческая карта."""
        g = self.fm.gender_of_product(name=name)
        if g not in SECTIONS_V1:
            g = self.behavior.get(name) or self.behavior.get((name or "").lower()) or "?"
        return g if g in SECTIONS_V1 else "other"

    def label_v2(self, name: str) -> str:
        """То же, но для витрин v2: perfume схлопывается в other."""
        g = self.label_name(name)
        return g if g in SECTIONS_V2 else "other"

    def section_of_article(self, art: str) -> str:
        """Раздел по артикулу (события приложения)."""
        g = self.artmap.get(art) or self.fm.gender_of_product(article=art)
        return g if g in SECTIONS_V1 else "?"

    def gender_of_item(self, it: dict) -> str:
        """Раздел позиции покупки приложения: артикулы → фид → поведение веба."""
        art = (it.get("item_article") or "").strip()
        if art and art in self.artmap:
            return self.artmap[art]
        g = self.fm.gender_of_product(name=it.get("item_name"), article=art,
                                      item_id=it.get("item_id") or it.get("item_variant"))
        if g in SECTIONS_V1:
            return g
        name = (it.get("item_name") or "").strip()
        return self.behavior.get(name) or self.behavior.get(name.lower()) or "?"
