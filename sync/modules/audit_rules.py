# -*- coding: utf-8 -*-
"""Правила модуля audit: 15 проверок кабинета по снимку, без обращений к Директу.

Правило получает Snapshot и настройки проекта и возвращает находки без action:
аудит только сообщает. object_id везде — id объекта (или нормализованная фраза),
а не его текст: исправленное правило перестаёт срабатывать на тот же отпечаток,
и reconcile закрывает находку как resolved.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sync.modules.queries import TYPE_BLOCKS, strategy_goals
from sync.modules.types import Finding
from sync.placements.direct import sites_limit

AUTOTARGETING = "---autotargeting"
TEXT_GROUP = "TEXT_AD_GROUP"
# Доля лимита, с которой список запретов считается почти полным (900 из 1000, 90 из 100).
SITES_NEAR = 0.9
# Лимит символов минус-фраз кампании в Директе — 20 000; предупреждаем с 18 000.
NEGATIVES_LIMIT_CHARS = 20000
NEGATIVES_NEAR_CHARS = 18000
# Лимит Title объявления в Директе — 56 символов.
TITLE_LIMIT = 56
AREA_OF_INTEREST = "ENABLE_AREA_OF_INTEREST_TARGETING"
BASE_URL = "https://direct.yandex.ru/dna/grid/campaigns"


@dataclass
class Snapshot:
    login: str
    campaigns: List[dict]
    groups: List[dict]
    ads: List[dict]
    keywords: List[dict]
    # id группы -> число включённых аудиторных условий показа
    audiences_by_group: Dict[int, int] = field(default_factory=dict)
    # id кампании -> показы за 3 дня; нет записи = 0
    impressions_3d: Dict[int, int] = field(default_factory=dict)


Check = Callable[[Snapshot, dict], List[Finding]]


def _block(campaign: dict) -> dict:
    block = campaign.get(TYPE_BLOCKS.get(campaign.get("Type"), ""))
    return block if isinstance(block, dict) else {}


def _items(value: Any) -> List[Any]:
    # Директ отдаёт списки то обёрткой {"Items": [...]}, то голым списком.
    if isinstance(value, dict):
        value = value.get("Items")
    return list(value) if isinstance(value, list) else []


def url(login: str, campaign_id: Any, *, group: Any = None, banner: Any = None) -> str:
    out = "%s?ulogin=%s&campaigns-ids=%s" % (BASE_URL, login, campaign_id)
    if group is not None:
        out += "&groups-ids=%s" % group
    if banner is not None:
        out += "&banners-ids=%s" % banner
    return out


def _camp_map(snap: Snapshot) -> Dict[int, dict]:
    return {int(c["Id"]): c for c in snap.campaigns}


def _finding(snap: Snapshot, rule: str, severity: str, camp: dict, object_type: str,
             object_id: Any, title: str, fix: str, link: str,
             extra: Optional[dict] = None) -> Finding:
    details = dict(extra or {})
    details["fix"] = fix
    details["url"] = link
    return Finding(rule=rule, severity=severity, title=title, login=snap.login,
                   campaign_id=int(camp["Id"]), campaign_name=camp.get("Name") or "",
                   object_type=object_type, object_id=str(object_id), details=details,
                   action=None)


def _text_ads(snap: Snapshot) -> Iterable[Tuple[dict, dict, dict]]:
    """(кампания, объявление, его TextAd) — только текстовые объявления ON-кампаний."""
    cmap = _camp_map(snap)
    for ad in snap.ads:
        camp = cmap.get(int(ad.get("CampaignId") or 0))
        text = ad.get("TextAd")
        if camp is not None and isinstance(text, dict):
            yield camp, ad, text


def _group_keywords(snap: Snapshot) -> Dict[int, List[dict]]:
    out: Dict[int, List[dict]] = {}
    for k in snap.keywords:
        out.setdefault(int(k.get("AdGroupId") or 0), []).append(k)
    return out


def _is_auto(k: dict) -> bool:
    return str(k.get("Keyword") or "").strip().lower() == AUTOTARGETING


def _phrase(k: dict) -> str:
    return " ".join(str(k.get("Keyword") or "").lower().split())


def group_no_targeting(snap: Snapshot, settings: dict) -> List[Finding]:
    cmap = _camp_map(snap)
    kws = _group_keywords(snap)
    out = []
    for g in snap.groups:
        camp = cmap.get(int(g.get("CampaignId") or 0))
        # Динамические и товарные группы таргетируются фильтрами и фидом, а не фразами:
        # судить их по ключам значило бы ложно кричать «показов не будет».
        if camp is None or g.get("Type") != TEXT_GROUP:
            continue
        gid = int(g["Id"])
        on = [k for k in kws.get(gid, []) if k.get("State", "ON") == "ON"]
        if on or snap.audiences_by_group.get(gid, 0) > 0:
            continue
        out.append(_finding(
            snap, "group_no_targeting", "critical", camp, "adgroup", gid,
            "Группа «%s» без условий показа" % (g.get("Name") or gid),
            "Добавьте ключевые фразы, включите автотаргетинг или подключите аудиторию.",
            url(snap.login, camp["Id"], group=gid), {"group_name": g.get("Name") or ""}))
    return out


def ads_rejected(snap: Snapshot, settings: dict) -> List[Finding]:
    cmap = _camp_map(snap)
    out = []
    for ad in snap.ads:
        camp = cmap.get(int(ad.get("CampaignId") or 0))
        if camp is None or ad.get("Status") != "REJECTED" or ad.get("State", "ON") != "ON":
            continue
        title = (ad.get("TextAd") or {}).get("Title") or ""
        out.append(_finding(
            snap, "ads_rejected", "critical", camp, "ad", ad["Id"],
            "Объявление %s отклонено модерацией" % ad["Id"],
            "Откройте причину отклонения в интерфейсе, поправьте текст и отправьте на повторную проверку.",
            url(snap.login, camp["Id"], group=ad.get("AdGroupId"), banner=ad["Id"]),
            {"title": title}))
    return out


def no_metrika_counter(snap: Snapshot, settings: dict) -> List[Finding]:
    out = []
    for c in snap.campaigns:
        # CounterIds запрашивается только у текстовых и ЕПК; у остальных типов
        # отсутствие поля в ответе ничего не значит.
        if c.get("Type") not in TYPE_BLOCKS:
            continue
        if _items(_block(c).get("CounterIds")):
            continue
        out.append(_finding(
            snap, "no_metrika_counter", "critical", c, "campaign", c["Id"],
            "У кампании нет счётчика Метрики",
            "Привяжите счётчик Метрики в настройках кампании.",
            url(snap.login, c["Id"])))
    return out


def banned_words(snap: Snapshot, settings: dict) -> List[Finding]:
    words = [str(w).strip().casefold() for w in settings.get("banned_words") or []
             if str(w).strip()]
    if not words:
        return []
    out = []
    for camp, ad, text in _text_ads(snap):
        hit: Dict[str, List[str]] = {}
        for fld in ("Title", "Title2", "Text"):
            value = str(text.get(fld) or "").casefold()
            for w in words:
                if w in value:
                    hit.setdefault(w, []).append(fld)
        if not hit:
            continue
        out.append(_finding(
            snap, "banned_words", "critical", camp, "ad", ad["Id"],
            "В объявлении %s запрещённое слово: %s" % (ad["Id"], ", ".join(sorted(hit))),
            "Уберите слово из заголовка и текста: тема запрещена для проекта.",
            url(snap.login, camp["Id"], group=ad.get("AdGroupId"), banner=ad["Id"]),
            {"words": sorted(hit), "fields": hit}))
    return out


def on_no_impressions(snap: Snapshot, settings: dict) -> List[Finding]:
    out = []
    for c in snap.campaigns:
        # Кампания не прошла модерацию или в черновике — это не «включена и молчит».
        if c.get("Status") not in (None, "ACCEPTED"):
            continue
        if snap.impressions_3d.get(int(c["Id"]), 0) > 0:
            continue
        out.append(_finding(
            snap, "on_no_impressions", "warning", c, "campaign", c["Id"],
            "Кампания включена, но показов за 3 дня нет",
            "Проверьте бюджет, ставки, регионы и условия показа.",
            url(snap.login, c["Id"])))
    return out


def autotargeting_with_keywords(snap: Snapshot, settings: dict) -> List[Finding]:
    cmap = _camp_map(snap)
    kws = _group_keywords(snap)
    out = []
    for g in snap.groups:
        camp = cmap.get(int(g.get("CampaignId") or 0))
        if camp is None:
            continue
        on = [k for k in kws.get(int(g["Id"]), []) if k.get("State", "ON") == "ON"]
        phrases = [k for k in on if not _is_auto(k)]
        if not phrases or not any(_is_auto(k) for k in on):
            continue
        out.append(_finding(
            snap, "autotargeting_with_keywords", "warning", camp, "adgroup", g["Id"],
            "В группе «%s» с фразами включён автотаргетинг" % (g.get("Name") or g["Id"]),
            "Отключите автотаргетинг в группе или оставьте его осознанно отдельной группой.",
            url(snap.login, camp["Id"], group=g["Id"]), {"phrases": len(phrases)}))
    return out


def _searches(campaign: dict) -> bool:
    if campaign.get("Type") not in TYPE_BLOCKS:
        return False
    search = (_block(campaign).get("BiddingStrategy") or {}).get("Search") or {}
    return search.get("BiddingStrategyType") != "SERVING_OFF"


def no_negatives(snap: Snapshot, settings: dict) -> List[Finding]:
    out = []
    for c in snap.campaigns:
        if not _searches(c) or _items(c.get("NegativeKeywords")):
            continue
        out.append(_finding(
            snap, "no_negatives", "warning", c, "campaign", c["Id"],
            "У поисковой кампании нет минус-фраз",
            "Добавьте минус-фразы по отчёту поисковых запросов.",
            url(snap.login, c["Id"])))
    return out


def _has_utm(href: str) -> bool:
    return "utm_source" in href and "utm_campaign" in href


def href_no_utm(snap: Snapshot, settings: dict) -> List[Finding]:
    if not settings.get("require_utm", True):
        return []
    out = []
    for camp, ad, text in _text_ads(snap):
        block = _block(camp)
        # TrackingParams запрашивается только у текстовых кампаний; у остальных его
        # отсутствие в ответе ничего не значит, и судить ссылку нельзя.
        if camp.get("Type") != "TEXT_CAMPAIGN" and "TrackingParams" not in block:
            continue
        if str(block.get("TrackingParams") or "").strip():
            continue
        href = str(text.get("Href") or "").strip()
        if not href or _has_utm(href):
            continue
        out.append(_finding(
            snap, "href_no_utm", "warning", camp, "ad", ad["Id"],
            "Ссылка объявления %s без utm_source и utm_campaign" % ad["Id"],
            "Добавьте разметку в ссылку или задайте TrackingParams на кампанию.",
            url(snap.login, camp["Id"], group=ad.get("AdGroupId"), banner=ad["Id"]),
            {"href": href}))
    return out


def strategy_goal_not_allowed(snap: Snapshot, settings: dict) -> List[Finding]:
    allowed = set()
    for g in settings.get("allowed_goal_ids") or []:
        try:
            allowed.add(int(g))
        except (TypeError, ValueError):
            continue
    if not allowed:
        return []
    out = []
    for c in snap.campaigns:
        bad = [g for g in strategy_goals(c) if g not in allowed]
        if not bad:
            continue
        out.append(_finding(
            snap, "strategy_goal_not_allowed", "warning", c, "campaign", c["Id"],
            "Цель стратегии не из списка проекта: %s" % ", ".join(map(str, bad)),
            "Выберите в стратегии цель из разрешённых проектом.",
            url(snap.login, c["Id"]), {"goal_ids": bad}))
    return out


def excluded_sites_near_limit(snap: Snapshot, settings: dict) -> List[Finding]:
    out = []
    for c in snap.campaigns:
        limit = sites_limit(c.get("Type") or "")
        count = len(_items(c.get("ExcludedSites")))
        if count < limit * SITES_NEAR:
            continue
        out.append(_finding(
            snap, "excluded_sites_near_limit", "warning", c, "campaign", c["Id"],
            "Запрещённых площадок %d из %d" % (count, limit),
            "Уберите устаревшие площадки из списка запретов, пока он не переполнился.",
            url(snap.login, c["Id"]), {"count": count, "limit": limit}))
    return out


def negatives_near_limit(snap: Snapshot, settings: dict) -> List[Finding]:
    out = []
    for c in snap.campaigns:
        chars = sum(len(str(p or "").strip()) for p in _items(c.get("NegativeKeywords")))
        if chars < NEGATIVES_NEAR_CHARS:
            continue
        out.append(_finding(
            snap, "negatives_near_limit", "warning", c, "campaign", c["Id"],
            "Минус-фразы занимают %d из %d символов" % (chars, NEGATIVES_LIMIT_CHARS),
            "Сократите список: уберите дубли и фразы, которые уже покрыты общими минус-словами.",
            url(snap.login, c["Id"]), {"chars": chars, "limit": NEGATIVES_LIMIT_CHARS}))
    return out


def no_sitelinks(snap: Snapshot, settings: dict) -> List[Finding]:
    out = []
    for camp, ad, text in _text_ads(snap):
        if text.get("SitelinkSetId"):
            continue
        out.append(_finding(
            snap, "no_sitelinks", "info", camp, "ad", ad["Id"],
            "У объявления %s нет быстрых ссылок" % ad["Id"],
            "Добавьте набор быстрых ссылок.",
            url(snap.login, camp["Id"], group=ad.get("AdGroupId"), banner=ad["Id"])))
    return out


def title_underused(snap: Snapshot, settings: dict) -> List[Finding]:
    try:
        fill = float(settings.get("title_fill_min", 0.6))
    except (TypeError, ValueError):
        fill = 0.6
    out = []
    for camp, ad, text in _text_ads(snap):
        title = str(text.get("Title") or "").strip()
        if not title or len(title) >= TITLE_LIMIT * fill:
            continue
        out.append(_finding(
            snap, "title_underused", "info", camp, "ad", ad["Id"],
            "Заголовок объявления %s занимает %d из %d символов" % (ad["Id"], len(title), TITLE_LIMIT),
            "Расширьте заголовок: добавьте выгоду или уточнение.",
            url(snap.login, camp["Id"], group=ad.get("AdGroupId"), banner=ad["Id"]),
            {"length": len(title), "limit": TITLE_LIMIT}))
    return out


def duplicate_keywords(snap: Snapshot, settings: dict) -> List[Finding]:
    cmap = _camp_map(snap)
    seen: Dict[Tuple[int, str], set] = {}
    for k in snap.keywords:
        cid = int(k.get("CampaignId") or 0)
        if cid not in cmap or _is_auto(k) or k.get("State", "ON") != "ON":
            continue
        phrase = _phrase(k)
        if phrase:
            seen.setdefault((cid, phrase), set()).add(int(k.get("AdGroupId") or 0))
    out = []
    for (cid, phrase), groups in sorted(seen.items()):
        if len(groups) < 2:
            continue
        camp = cmap[cid]
        out.append(_finding(
            snap, "duplicate_keywords", "info", camp, "phrase", phrase,
            "Фраза «%s» повторяется в %d группах" % (phrase, len(groups)),
            "Оставьте фразу в одной группе или разведите их минус-словами.",
            url(snap.login, cid), {"group_ids": sorted(groups)}))
    return out


def area_of_interest_on(snap: Snapshot, settings: dict) -> List[Finding]:
    out = []
    for c in snap.campaigns:
        opts = _block(c).get("Settings")
        on = isinstance(opts, list) and any(
            isinstance(o, dict) and o.get("Option") == AREA_OF_INTEREST
            and o.get("Value") == "YES" for o in opts)
        if not on:
            continue
        out.append(_finding(
            snap, "area_of_interest_on", "info", c, "campaign", c["Id"],
            "Включён расширенный геотаргетинг",
            "Выключите вручную в настройках кампании: API это не меняет.",
            url(snap.login, c["Id"])))
    return out


RULES: List[Tuple[str, str, Check]] = [
    ("group_no_targeting", "critical", group_no_targeting),
    ("ads_rejected", "critical", ads_rejected),
    ("no_metrika_counter", "critical", no_metrika_counter),
    ("banned_words", "critical", banned_words),
    ("on_no_impressions", "warning", on_no_impressions),
    ("autotargeting_with_keywords", "warning", autotargeting_with_keywords),
    ("no_negatives", "warning", no_negatives),
    ("href_no_utm", "warning", href_no_utm),
    ("strategy_goal_not_allowed", "warning", strategy_goal_not_allowed),
    ("excluded_sites_near_limit", "warning", excluded_sites_near_limit),
    ("negatives_near_limit", "warning", negatives_near_limit),
    ("no_sitelinks", "info", no_sitelinks),
    ("title_underused", "info", title_underused),
    ("duplicate_keywords", "info", duplicate_keywords),
    ("area_of_interest_on", "info", area_of_interest_on),
]
