# -*- coding: utf-8 -*-
"""Каналы и кампании Роистата → таксономия LIME.

Таксономия общая с sync/metrika_channels.py и каноном sync/lime.py classify(): срезы
kz_roistat, kz_metrika и kz описывают ОДНИ продажи разными глазами, и сравнить их можно
только в одинаковых названиях каналов. Разойдёмся в именах — один канал распадётся на две
строки, и вся затея со сводной таблицей потеряет смысл.

Незнакомый канал НЕ угадываем: он уходит в Others с собственным именем в subchannel, чтобы
появление нового источника было видно в дашборде, а не растворилось.

Ограничение, которое надо знать: Роистат не различает поисковики внутри SEO — у него один
канал «SEO», тогда как Метрика даёт «SEO Google» и «SEO Yandex» отдельно. Поэтому по SEO
сводная сходится на уровне канала, но не подканала.
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


def map_roistat_channel(name: str) -> tuple[str, str, str]:
    """(channel, subchannel, traffic_type) в таксономии LIME."""
    key = _norm(name)
    if not key:
        return ("Others", "Unknown", FREE)
    if key in IS_OFFLINE:
        return ("Offline", key, FREE)
    if key in _MAP:
        return _MAP[key]
    if key.lower().startswith(("mindbox", "manual_mindbox")):
        return ("CRM", key, FREE)
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
