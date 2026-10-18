# -*- coding: utf-8 -*-
"""Правила модуля audit: 15 проверок кабинета по снимку, без обращений к Директу.

Правило получает Snapshot и настройки проекта и возвращает находки без action:
аудит только сообщает. object_id везде — id объекта (или нормализованная фраза),
а не его текст: исправленное правило перестаёт срабатывать на тот же отпечаток,
и reconcile закрывает находку как resolved.

Чего правило не проверило (тип без нужных полей, выключено настройкой, поле не принято
API), оно записывает в snap.skipped: молчание правила не должно читаться как чистый кабинет.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

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
GRID_URL = "https://direct.yandex.ru/dna/grid/campaigns"
CAMPAIGN_EDIT_URL = "https://direct.yandex.ru/dna/campaigns-edit"
BANNER_EDIT_URL = "https://direct.yandex.ru/dna/banners-edit"

# Поля, которые запрашиваются с запасным путём: не принял API — зависимое правило не судит.
SHARED_SETS = "NegativeKeywordSharedSetIds"
UNIFIED_TRACKING = "UnifiedTrackingParams"
START_DATE = "StartDate"

# Формы глагола «занять»: «займёт 2 года» — не реклама микрозаймов, а обычная речь. Корень
# «займ» из banned_words ловит и их, поэтому исключаем явно (ё сведена к е).
_ZANYAT_FORMS = frozenset({"займу", "займешь", "займет", "займем", "займете", "займут"})
_WORD = re.compile(r"\w+")


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
    # отклонённые неархивные объявления (отдельный запрос Statuses=REJECTED); None — запрос не удался
    rejected_ads: Optional[List[dict]] = field(default_factory=list)
    # комбинаторные объявления ЕПК из v501; None — v501 не ответил
    responsive_ads: Optional[List[dict]] = field(default_factory=list)
    # поля, которые API не принял: SHARED_SETS, UNIFIED_TRACKING
    unavailable: Dict[str, str] = field(default_factory=dict)
    # начало окна показов (ГГГГ-ММ-ДД): кампания, стартовавшая позже, не успела показаться
    impressions_from: str = ""
    # правило -> причина -> сколько объектов не проверено
    skipped: Dict[str, Dict[str, int]] = field(default_factory=dict)
    # правила, не проверившие кабинет целиком (вход не прочитан или правило выключено):
    # их открытые находки в этом кабинете прогон не закрывает
    unverified: Set[str] = field(default_factory=set)


Check = Callable[[Snapshot, dict], List[Finding]]


def skip(snap: Snapshot, rule: str, reason: str, count: int = 1, whole: bool = False) -> None:
    """whole=True — правило не получило свой вход или выключено, а не просто не судит объект
    неподходящего типа (такой объект находки и не давал): молчание правила тогда не «чисто»."""
    by = snap.skipped.setdefault(rule, {})
    by[reason] = by.get(reason, 0) + count
    if whole:
        snap.unverified.add(rule)


def _block(campaign: dict) -> dict:
    block = campaign.get(TYPE_BLOCKS.get(campaign.get("Type"), ""))
    return block if isinstance(block, dict) else {}


def _items(value: Any) -> List[Any]:
    # Директ отдаёт списки то обёрткой {"Items": [...]}, то голым списком.
    if isinstance(value, dict):
        value = value.get("Items")
    return list(value) if isinstance(value, list) else []


def url(login: str, campaign_id: Any, *, group: Any = None, banner: Any = None) -> str:
    if banner is not None:
        return "%s?ulogin=%s&campaigns-ids=%s&banners-ids=%s" % (
            BANNER_EDIT_URL, login, campaign_id, banner)
    if group is not None:
        return "%s?ulogin=%s&campaigns-ids=%s&groups-ids=%s" % (
            GRID_URL, login, campaign_id, group)
    return "%s?ulogin=%s&campaigns-ids=%s" % (CAMPAIGN_EDIT_URL, login, campaign_id)


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


def _ad_link(snap: Snapshot, camp: dict, ad: dict) -> str:
    return url(snap.login, camp["Id"], banner=ad["Id"])


def _ads(snap: Snapshot) -> Iterable[Tuple[dict, dict]]:
    """(кампания, объявление) для всех прочитанных объявлений ON-кампаний."""
    cmap = _camp_map(snap)
    # v5 может отдать комбинаторное объявление ЕПК как TextAd, а v501 — как ResponsiveAd:
    # один Id — одна запись, версия v501 полнее (все заголовки и тексты).
    by_id: Dict[Any, dict] = {}
    for ad in list(snap.ads) + list(snap.responsive_ads or []):
        by_id[ad.get("Id")] = ad
    for ad in by_id.values():
        camp = cmap.get(int(ad.get("CampaignId") or 0))
        if camp is not None:
            yield camp, ad


def _texts(ad: dict) -> Optional[Tuple[List[str], List[str]]]:
    """(заголовки, тексты): TextAd — Title/Title2/Text, ResponsiveAd — Titles/Texts."""
    text = ad.get("TextAd")
    if isinstance(text, dict):
        titles = [str(t) for t in (text.get("Title"), text.get("Title2")) if t]
        return titles, [str(text["Text"])] if text.get("Text") else []
    resp = ad.get("ResponsiveAd")
    if isinstance(resp, dict):
        def pick(items, key):
            out = []
            for it in _items(items):
                value = it.get(key) if isinstance(it, dict) else it
                if value:
                    out.append(str(value))
            return out
        return pick(resp.get("Titles"), "Title"), pick(resp.get("Texts"), "Text")
    return None


def _count_unread_ads(snap: Snapshot) -> int:
    """Объявления v5 без TextAd (ЕПК): их тексты читает только v501."""
    cmap = _camp_map(snap)
    return sum(1 for ad in snap.ads
               if int(ad.get("CampaignId") or 0) in cmap and _texts(ad) is None)


def _epk_blind(snap: Snapshot, rule: str) -> bool:
    """v501 не ответил: объявление ЕПК v5 отдаёт то как TextAd, то без него, и вердикт правила
    по нему зависел бы от того, какая версия API ответила (находка то есть, то закрыта). Такие
    объявления правило в этом прогоне не судит, а его открытые находки не закрываются."""
    if snap.responsive_ads is not None:
        return False
    n = sum(1 for camp, _ in _ads(snap) if camp.get("Type") == "UNIFIED_CAMPAIGN")
    if n:
        skip(snap, rule, "объявления ЕПК: v501 не ответил, вердикт зависел бы от версии API", n,
             whole=True)
    return True


def _group_keywords(snap: Snapshot) -> Dict[int, List[dict]]:
    out: Dict[int, List[dict]] = {}
    for k in snap.keywords:
        out.setdefault(int(k.get("AdGroupId") or 0), []).append(k)
    return out


def _is_auto(k: dict) -> bool:
    return str(k.get("Keyword") or "").strip().lower() == AUTOTARGETING


def _phrase(k: dict) -> str:
    return " ".join(str(k.get("Keyword") or "").lower().split())


def _norm(text: str) -> str:
    return text.casefold().replace("ё", "е")


def group_no_targeting(snap: Snapshot, settings: dict) -> List[Finding]:
    cmap = _camp_map(snap)
    kws = _group_keywords(snap)
    out = []
    for g in snap.groups:
        camp = cmap.get(int(g.get("CampaignId") or 0))
        if camp is None:
            continue
        # Динамические и товарные группы таргетируются фильтрами и фидом, а не фразами:
        # судить их по ключам значило бы ложно кричать «показов не будет».
        if g.get("Type") != TEXT_GROUP:
            skip(snap, "group_no_targeting", "группа не текстовая (таргет не по фразам)")
            continue
        # Черновик ещё не запущен: условий показа у него может не быть законно.
        if g.get("Status") == "DRAFT":
            skip(snap, "group_no_targeting", "группа в черновике")
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
    if snap.rejected_ads is None:
        skip(snap, "ads_rejected", "запрос отклонённых объявлений не удался", whole=True)
        return out
    for ad in snap.rejected_ads:
        camp = cmap.get(int(ad.get("CampaignId") or 0))
        if camp is None or ad.get("State") == "ARCHIVED":
            continue
        title = (ad.get("TextAd") or {}).get("Title") or ""
        out.append(_finding(
            snap, "ads_rejected", "critical", camp, "ad", ad["Id"],
            "Объявление %s отклонено модерацией" % ad["Id"],
            "Откройте причину отклонения в интерфейсе, поправьте текст и отправьте на повторную проверку.",
            _ad_link(snap, camp, ad), {"title": title, "state": ad.get("State")}))
    return out


def no_metrika_counter(snap: Snapshot, settings: dict) -> List[Finding]:
    out = []
    for c in snap.campaigns:
        # CounterIds запрашивается только у текстовых и ЕПК; у остальных типов
        # отсутствие поля в ответе ничего не значит.
        if c.get("Type") not in TYPE_BLOCKS:
            skip(snap, "no_metrika_counter", "тип кампании без запроса счётчика")
            continue
        if _items(_block(c).get("CounterIds")):
            continue
        out.append(_finding(
            snap, "no_metrika_counter", "critical", c, "campaign", c["Id"],
            "У кампании нет счётчика Метрики",
            "Привяжите счётчик Метрики в настройках кампании.",
            url(snap.login, c["Id"])))
    return out


def _banned_hits(text: str, words: List[str]) -> List[str]:
    """Слова из banned_words, с которых начинается слово текста (ё = е, регистр не важен)."""
    norm = _norm(text)
    hit = []
    for w in words:
        for m in re.finditer(r"(?<!\w)" + re.escape(w), norm):
            token = _WORD.match(norm, m.start())
            if token and token.group(0) in _ZANYAT_FORMS:
                continue
            hit.append(w)
            break
    return hit


def banned_words(snap: Snapshot, settings: dict) -> List[Finding]:
    words = [_norm(str(w).strip()) for w in settings.get("banned_words") or []
             if str(w).strip()]
    if not words:
        skip(snap, "banned_words", "список banned_words пуст — правило выключено",
             sum(1 for _ in _ads(snap)), whole=True)
        return []
    unread = _count_unread_ads(snap) if snap.responsive_ads is None else 0
    if unread:
        skip(snap, "banned_words", "объявления ЕПК: v501 не ответил, читать нечем", unread,
             whole=True)
    out = []
    for camp, ad in _ads(snap):
        texts = _texts(ad)
        if texts is None:
            continue
        titles, bodies = texts
        hit: Dict[str, List[str]] = {}
        for fld, values in (("Title", titles), ("Text", bodies)):
            for value in values:
                for w in _banned_hits(value, words):
                    if fld not in hit.setdefault(w, []):
                        hit[w].append(fld)
        if not hit:
            continue
        out.append(_finding(
            snap, "banned_words", "critical", camp, "ad", ad["Id"],
            "В объявлении %s запрещённое слово: %s" % (ad["Id"], ", ".join(sorted(hit))),
            "Уберите слово из заголовка и текста: тема запрещена для проекта.",
            _ad_link(snap, camp, ad), {"words": sorted(hit), "fields": hit}))
    return out


def on_no_impressions(snap: Snapshot, settings: dict) -> List[Finding]:
    out = []
    for c in snap.campaigns:
        # Кампания не прошла модерацию или в черновике — это не «включена и молчит».
        if c.get("Status") not in (None, "ACCEPTED"):
            skip(snap, "on_no_impressions", "кампания не прошла модерацию или в черновике")
            continue
        # Стартовавшая внутри окна или позже не обязана была показываться все три дня.
        if START_DATE in snap.unavailable:
            skip(snap, "on_no_impressions", "поле StartDate не принято API", whole=True)
            continue
        start = str(c.get("StartDate") or "")
        if start and snap.impressions_from and start > snap.impressions_from:
            skip(snap, "on_no_impressions", "кампания стартовала после начала окна показов")
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
    search = (_block(campaign).get("BiddingStrategy") or {}).get("Search") or {}
    return search.get("BiddingStrategyType") != "SERVING_OFF"


def no_negatives(snap: Snapshot, settings: dict) -> List[Finding]:
    out = []
    for c in snap.campaigns:
        if c.get("Type") not in TYPE_BLOCKS or not _searches(c):
            skip(snap, "no_negatives", "не поисковая кампания или тип без минус-фраз")
            continue
        if SHARED_SETS in snap.unavailable:
            # Без наборов общих минус-слов «пусто» может быть ложным: не судим.
            skip(snap, "no_negatives", "поле %s не принято API" % SHARED_SETS, whole=True)
            continue
        if _items(c.get("NegativeKeywords")) or _items(c.get(SHARED_SETS)):
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
    groups = {int(g["Id"]): g for g in snap.groups}
    if not settings.get("require_utm", True):
        skip(snap, "href_no_utm", "require_utm выключен — правило выключено",
             sum(1 for _ in _ads(snap)), whole=True)
        return []
    blind = _epk_blind(snap, "href_no_utm")
    out = []
    for camp, ad in _ads(snap):
        if blind and camp.get("Type") == "UNIFIED_CAMPAIGN":
            continue
        text = ad.get("TextAd")
        if not isinstance(text, dict):
            continue
        block = _block(camp)
        ctype = camp.get("Type")
        if ctype not in TYPE_BLOCKS:
            skip(snap, "href_no_utm", "тип кампании без чтения TrackingParams")
            continue
        if ctype == "UNIFIED_CAMPAIGN" and UNIFIED_TRACKING in snap.unavailable:
            skip(snap, "href_no_utm", "поле TrackingParams не принято API (ЕПК)", whole=True)
            continue
        if str(block.get("TrackingParams") or "").strip():
            continue
        group = groups.get(int(ad.get("AdGroupId") or 0)) or {}
        if str(group.get("TrackingParams") or "").strip():
            continue
        href = str(text.get("Href") or "").strip()
        if not href or _has_utm(href):
            continue
        out.append(_finding(
            snap, "href_no_utm", "warning", camp, "ad", ad["Id"],
            "Ссылка объявления %s без utm_source и utm_campaign" % ad["Id"],
            "Добавьте разметку в ссылку или задайте TrackingParams на кампанию.",
            _ad_link(snap, camp, ad), {"href": href}))
    return out


def strategy_goal_not_allowed(snap: Snapshot, settings: dict) -> List[Finding]:
    allowed = set()
    for g in settings.get("allowed_goal_ids") or []:
        try:
            allowed.add(int(g))
        except (TypeError, ValueError):
            continue
    if not allowed:
        skip(snap, "strategy_goal_not_allowed",
             "список allowed_goal_ids пуст — правило выключено", len(snap.campaigns), whole=True)
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
    blind = _epk_blind(snap, "no_sitelinks")
    out = []
    for camp, ad in _ads(snap):
        if blind and camp.get("Type") == "UNIFIED_CAMPAIGN":
            continue
        text = ad.get("TextAd")

        if not isinstance(text, dict):
            # SitelinkSetId читается из TextAd; у комбинаторных его в запросе нет.
            if ad.get("ResponsiveAd") is not None:
                skip(snap, "no_sitelinks", "объявление ЕПК: быстрые ссылки не читаются")
            continue
        if text.get("SitelinkSetId"):
            continue
        out.append(_finding(
            snap, "no_sitelinks", "info", camp, "ad", ad["Id"],
            "У объявления %s нет быстрых ссылок" % ad["Id"],
            "Добавьте набор быстрых ссылок.", _ad_link(snap, camp, ad)))
    return out


def title_underused(snap: Snapshot, settings: dict) -> List[Finding]:
    try:
        fill = float(settings.get("title_fill_min", 0.6))
    except (TypeError, ValueError):
        fill = 0.6
    unread = _count_unread_ads(snap) if snap.responsive_ads is None else 0
    if unread:
        skip(snap, "title_underused", "объявления ЕПК: v501 не ответил, читать нечем", unread,
             whole=True)
    out = []
    for camp, ad in _ads(snap):
        texts = _texts(ad)
        if texts is None or not texts[0]:
            continue
        if isinstance(ad.get("TextAd"), dict):
            # Title2 короче по лимиту и в заполнение заголовка не входит.
            longest = len(str(ad["TextAd"].get("Title") or "").strip())
        else:
            # У комбинаторного объявления заголовков несколько: судим по самому длинному.
            longest = max(len(t.strip()) for t in texts[0])
        if not longest or longest >= TITLE_LIMIT * fill:
            continue
        out.append(_finding(
            snap, "title_underused", "info", camp, "ad", ad["Id"],
            "Заголовок объявления %s занимает %d из %d символов" % (ad["Id"], longest, TITLE_LIMIT),
            "Расширьте заголовок: добавьте выгоду или уточнение.",
            _ad_link(snap, camp, ad), {"length": longest, "limit": TITLE_LIMIT}))
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
        if c.get("Type") not in TYPE_BLOCKS:
            skip(snap, "area_of_interest_on", "тип кампании без чтения Settings")
            continue
        on = any(isinstance(o, dict) and o.get("Option") == AREA_OF_INTEREST
                 and o.get("Value") == "YES" for o in _items(_block(c).get("Settings")))
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
