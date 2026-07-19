# -*- coding: utf-8 -*-
"""Каналы и кампании Роистата → таксономия LIME.

Таксономия общая с sync/metrika_channels.py и каноном sync/lime.py classify(): срезы
kz_roistat, kz_metrika и kz описывают ОДНИ продажи разными глазами, и сравнить их можно
только в одинаковых названиях каналов. Разойдёмся в именах — один канал распадётся на две
строки, и вся затея со сводной таблицей потеряет смысл.

Незнакомый канал НЕ угадываем: он уходит в Others с собственным именем в subchannel, чтобы
появление нового источника было видно в дашборде, а не растворилось.

Подканал живёт на marker_level_2 и ложится на канон один в один (замер июня 2026):
  SEO             › Google 10 284 / Яндекс 1 076 / Bing 119 / Mail.Ru 67  → SEO Google|Yandex|Others
  Визиты с сайтов › 81 домен реферера (l.instagram.com 2 500, acs.tiptoppay.kz 65)  → домен
  manual_mindbox  › email  → Mindbox
  Прямые визиты   › пусто  → Direct
У платных каналов level_2 — это ТИП кампании (Поиск / КМС / PMax / РСЯ), а не подканал:
канон такой ступени не знает, поэтому там подканал задаётся каналом.
"""

PAID = "Платный"
FREE = "Бесплатный"

# Сделки без визита: колл-центр, ручное заведение, битый номер. 568 из 4 714 заявок
# июня (12%). Рекламе не принадлежат — отдельный канал, чтобы не искажали конверсию.
IS_OFFLINE = frozenset({
    "Сделки, созданные самостоятельно",
    "Сделки с некорректным номером визита",
})

_MAP = {
    "Google Ads 1":     ("SEM", "Google.Adwords", PAID),
    "Google Ads":       ("SEM", "Google.Adwords", PAID),
    "Яндекс.Директ 1":  ("SEM", "Яндекс.Директ", PAID),
    "Facebook":         ("SMM paid", "Meta Ads", PAID),
    "Прямые визиты":    ("Direct", "Direct", FREE),
    "direct":           ("Direct", "Direct", FREE),
    "SEO":              ("SEO", "SEO Others", FREE),
    "Визиты с сайтов":  ("Referrals", "Реферал", FREE),
    "web_sharing":      ("Referrals", "web_sharing", FREE),
    "mp_sharing":       ("Referrals", "mp_sharing", FREE),
    "ig":               ("SMM (organic)", "Instagram", FREE),
    "instagram":        ("SMM (organic)", "Instagram", FREE),
    "telegram":         ("SMM (organic)", "Telegram", FREE),
    "pinterest":        ("SMM (organic)", "Pinterest", FREE),
}

# Каналы, у которых кампания лежит на level_2, а level_3 — это адсет. Проверено на июне
# 2026: Facebook › «CPO: ЛЕТНИЙ SALE_ЖЕНЩИНЫ» › «CPO_Ж». У Google и Директа наоборот —
# level_2 это код типа кампании (g / d / x / search / context), а сама кампания на level_3.
CAMPAIGN_ON_LEVEL2 = frozenset({"Facebook"})

# Роистат пишет отсутствие значения литералом, а не пустой строкой.
NO_VALUE = frozenset({"", "Нет значения"})


def _norm(name: str) -> str:
    """Подписи Роистата приходят с неразрывным пробелом (U+00A0)."""
    return (name or "").replace("\xa0", " ").strip()


# Канон SEO знает три ступени; Роистат отдаёт движок в value уровня (google/yandex/bing/…).
_SEO_CANON = (("google", "SEO Google"), ("yandex", "SEO Yandex"))


def _seo_subchannel(level2_id: str, level2: str) -> str:
    probe = f"{_norm(level2_id)} {_norm(level2)}".lower()
    for needle, canon in _SEO_CANON:
        if needle in probe:
            return canon
    return "SEO Others"


def map_roistat_channel(name: str, level2: str = "", level2_id: str = "") -> tuple[str, str, str]:
    """(channel, subchannel, traffic_type) в таксономии LIME.

    Args:
        name: marker_level_1 (канал).
        level2: подпись marker_level_2 — подканал у SEO/Referrals/CRM, тип кампании у
            платных каналов, пусто у Direct.
        level2_id: value того же уровня (у SEO это код движка: google/yandex/bing).
    """
    key = _norm(name)
    if not key:
        return ("Others", "Unknown", FREE)
    if key in IS_OFFLINE:
        return ("Offline", key, FREE)

    if key == "SEO":
        return ("SEO", _seo_subchannel(level2_id, level2), FREE)
    if key == "Визиты с сайтов":
        # Канон: «Referrals → домен реферера»; у Роистата домен ровно на level_2.
        return ("Referrals", _norm(level2) or "Реферал", FREE)
    if key.lower().startswith(("mindbox", "manual_mindbox")):
        # level_2 у рассылок = «email»; канон витрины называет систему — Mindbox.
        return ("CRM", "Mindbox", FREE)

    if key in _MAP:
        return _MAP[key]
    return ("Others", key, FREE)


def campaign_of(channel: str, row: dict) -> tuple[str, str]:
    """(campaign_id, campaign_name) строки. Пустая пара — кампании нет.

    Id берём из `value` уровня: он совпадает с campaign_id наших кабинетов один в один
    (23237404958, 117776765, 23952158615 — проверено на июне), поэтому склейка по нему
    устойчива к переименованию кампании, в отличие от склейки по имени.
    """
    lvl = "level2" if _norm(channel) in CAMPAIGN_ON_LEVEL2 else "level3"
    cid = _norm(row.get(f"{lvl}_id"))
    name = _norm(row.get(lvl))
    if cid in NO_VALUE and name in NO_VALUE:
        return ("", "")
    return ("" if cid in NO_VALUE else cid, "" if name in NO_VALUE else name)
