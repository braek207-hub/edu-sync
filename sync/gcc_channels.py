"""Маппинг каналов GCC (Triple Whale / GA4 source+medium) → единая таксономия дашборда.

Отдельно от sync.lime.classify() (тот заточен под source/medium Яндекс.Метрики RU).
Стартовый набор — Google/Meta/органика/CRM/директ; расширяется по зонду позже.
"""

# Маппер Метрики переехал в нейтральный модуль (его использует и KZ-синк).
# Реэкспорт, чтобы sync/lime_gcc.py и тесты GCC продолжали импортировать отсюда.
from sync.metrika_channels import map_metrika_channel  # noqa: F401


# Страны Залива: префикс поддомена витрины → название для дашборда.
# Один Shopify обслуживает все страны через домены *.limestore.com и *.lime-shop.com
# (зонды P1/P3, docs/GCC_CONTRACTS.md) → матчим ПРЕФИКС, а не полный хост.
GCC_DOMAIN_COUNTRY = {
    "ae": "ОАЭ",
    "bh": "Бахрейн",
    "kw": "Кувейт",
    "sa": "Саудовская Аравия",
    "qa": "Катар",
    "om": "Оман",
}


def map_domain_country(domain: str | None) -> str | None:
    """Домен витрины GCC → страна Залива.

    Args:
        domain: хост, напр. "ae.limestore.com" / "sa.lime-shop.com".

    Returns:
        Название страны или None, если префикс не из списка GCC
        (www./голый домен/пусто) — такие строки идут только в GCC-тотал.
    """
    prefix = (domain or "").strip().lower().split(".")[0]
    return GCC_DOMAIN_COUNTRY.get(prefix)


# Медиумы платного трафика GA4. Живые значения витрины (зонд 2026-08-20): google/cpc,
# facebook/paid, instagram/cpc; остальные — на вырост тем же словарём.
_GA4_PAID_MEDIUMS = {"cpc", "cpa", "cpm", "ppc", "paid", "paid_social", "display", "retargeting"}

# Соцсети в source (реферальный/огранический заход) → подканал как у TW-рефереров,
# иначе визиты GA4 и заказы TW одной сети встанут разными строками.
_GA4_SOCIAL_RULES = (
    ("instagram", "Instagram"),
    ("facebook", "Facebook"),
    ("tiktok", "Tiktok"),
    ("telegram", "Telegram"),
    ("t.me", "Telegram"),
    ("youtube", "Youtube"),
    ("pinterest", "Pinterest"),
    ("snap", "Snapchat"),
    ("twitter", "Twitter"),
)


def map_ga4_channel(source: str | None, medium: str | None) -> tuple[str, str, str]:
    """GA4 sessionSource/sessionMedium → (channel, subchannel, traffic_type) таксономии дашборда.

    Ключи каналов/подканалов совпадают с map_tw_source и map_metrika_channel — на этом
    держится мерж трафика с заказами/расходом по (channel, subchannel) в lime_gcc.
    """
    s = (source or "").lower().strip()
    m = (medium or "").lower().strip()

    if m in _GA4_PAID_MEDIUMS:
        if "google" in s:
            return "SEM", "Google.Adwords", "Платный"
        if "bing" in s:
            return "SEM", "Bing", "Платный"
        if any(x in s for x in ("facebook", "instagram", "meta")) or s in ("fb", "ig"):
            return "SMM paid", "Meta Ads", "Платный"
        if "tiktok" in s:
            return "SMM paid", "TikTok Ads", "Платный"
        if "snap" in s:
            return "SMM paid", "Snapchat Ads", "Платный"
        if "pinterest" in s:
            return "SMM paid", "Pinterest Ads", "Платный"
        return "Others", (source or "").strip() or "Ad", "Платный"

    if m == "organic":
        if "google" in s:
            return "SEO", "SEO Google", "Бесплатный"
        if "yandex" in s:
            return "SEO", "SEO Yandex", "Бесплатный"
        return "SEO", "SEO Others", "Бесплатный"

    if "mindbox" in s or "maestra" in s:
        return "CRM", "Mindbox", "Бесплатный"
    if m in ("email", "e-mail") or "klaviyo" in s:
        return "CRM", "Email", "Бесплатный"
    if m == "sms":
        return "CRM", "SMS", "Бесплатный"
    if m in ("push", "mobile_push"):
        return "CRM", "Push", "Бесплатный"

    if s in ("(direct)", "(none)", "direct", "") and m in ("(none)", "(not set)", "none", ""):
        return "Direct", "Direct", "Бесплатный"

    if any(own in s for own in _OWN_DOMAINS):
        return "Internal", "Internal", "Бесплатный"

    for needle, name in _GA4_SOCIAL_RULES:
        if needle in s:
            return "SMM (organic)", name, "Бесплатный"

    if s in ("qr", "qrcode") or m == "qr":
        return "Others", "QR", "Бесплатный"

    if m in ("referral", "social", "organic_social"):
        return "Referrals", (source or "").strip() or "Реферал", "Бесплатный"

    return "Others", (source or medium or "Unknown").strip(), "Бесплатный"


# Короткие алиасы соцсетей — только точным совпадением: подстрока «ig» ловила бы
# digital/night, «fb» — левые домены.
_SOCIAL_ALIASES = {"ig": "Instagram", "fb": "Facebook", "yt": "Youtube", "tt": "Tiktok"}


def _social_net(s: str) -> str | None:
    """Имя соцсети из source: точный алиас (ig/fb) или подстрока (_GA4_SOCIAL_RULES)."""
    alias = _SOCIAL_ALIASES.get(s)
    if alias:
        return alias
    for needle, name in _GA4_SOCIAL_RULES:
        if needle in s:
            return name
    return None


def map_ga4_channel_grouped(group: str | None, source: str | None,
                            medium: str | None) -> tuple[str, str, str]:
    """GA4 sessionDefaultChannelGroup (+source/medium) → таксономия дашборда.

    Группа каналов GA4 — АВТОРИТЕТ границы платный/органика и канала: ручной отчёт Залива
    собирается из отчётов GA4 по группам, и только так раскладка совпадает с ним по
    построению (решение 2026-08-20). source уточняет подканал (площадку/сеть/поисковик).
    Неизвестная группа → fallback на map_ga4_channel(source, medium).
    """
    g = (group or "").strip()
    s = (source or "").lower().strip()

    if g in ("Paid Search", "Cross-network", "Paid Shopping", "Display"):
        if "bing" in s:
            return "SEM", "Bing", "Платный"
        if any(x in s for x in ("facebook", "instagram", "meta")):
            return "SMM paid", "Meta Ads", "Платный"
        return "SEM", "Google.Adwords", "Платный"

    if g in ("Paid Social", "Paid Video", "Paid Other", "Audio"):
        for needle, sub in (("facebook", "Meta Ads"), ("instagram", "Meta Ads"),
                            ("meta", "Meta Ads"), ("tiktok", "TikTok Ads"),
                            ("snap", "Snapchat Ads"), ("pinterest", "Pinterest Ads")):
            if needle in s:
                return "SMM paid", sub, "Платный"
        if "google" in s or "youtube" in s:
            return "SEM", "Google.Adwords", "Платный"
        return "SMM paid", (source or "").strip() or "Social Ads", "Платный"

    if g in ("Organic Search", "Organic Shopping"):
        if "yandex" in s:
            return "SEO", "SEO Yandex", "Бесплатный"
        if "google" in s or g == "Organic Shopping":
            return "SEO", "SEO Google", "Бесплатный"
        return "SEO", "SEO Others", "Бесплатный"

    if g in ("Organic Social", "Organic Video"):
        name = _social_net(s)
        if name:
            return "SMM (organic)", name, "Бесплатный"
        return "SMM (organic)", (source or "").strip().capitalize() or "Social", "Бесплатный"

    if g == "Email":
        if "mindbox" in s or "maestra" in s:
            return "CRM", "Mindbox", "Бесплатный"
        return "CRM", "Email", "Бесплатный"
    if g == "SMS":
        return "CRM", "SMS", "Бесплатный"
    if g in ("Mobile Push Notifications", "Push"):
        return "CRM", "Push", "Бесплатный"

    if g == "Direct":
        return "Direct", "Direct", "Бесплатный"

    if g == "Referral":
        if any(own in s for own in _OWN_DOMAINS):
            return "Internal", "Internal", "Бесплатный"
        name = _social_net(s)
        if name:
            return "SMM (organic)", name, "Бесплатный"
        if "mindbox" in s or "maestra" in s:
            return "CRM", "Mindbox", "Бесплатный"
        return "Referrals", (source or "").strip() or "Реферал", "Бесплатный"

    if g in ("AI Assistant", "Affiliates"):
        return "Referrals", (source or "").strip() or g, "Бесплатный"
    if g == "Unassigned":
        return "Others", "Unassigned", "Бесплатный"

    return map_ga4_channel(source, medium)


# Партнёр AppMetrica (publisher_name касания, last-touch) → таксономия. Ключи каналов те же,
# что у web-заказов TW (map_tw_source) — app-строки в дашборде группируются в те же каналы.
_APP_PUBLISHER_RULES = (
    ("google ads", ("SEM", "Google.Adwords", "Платный")),
    ("yandex.direct", ("SEM", "Яндекс.Директ", "Платный")),
    ("bing", ("SEM", "Bing", "Платный")),
    ("facebook", ("SMM paid", "Meta Ads", "Платный")),
    ("instagram", ("SMM paid", "Meta Ads", "Платный")),
    ("meta ads", ("SMM paid", "Meta Ads", "Платный")),
    ("tiktok", ("SMM paid", "TikTok Ads", "Платный")),
    ("snapchat", ("SMM paid", "Snapchat Ads", "Платный")),
    ("vk ads", ("SMM paid", "VK.Ads", "Платный")),
    ("vkads", ("SMM paid", "VK.Ads", "Платный")),
    ("mytarget", ("SMM paid", "VK.Ads", "Платный")),
)


def map_app_publisher(publisher: str | None) -> tuple[str, str, str]:
    """AppMetrica publisher_name → (channel, subchannel, traffic_type).

    Пустой publisher = касаний нет = человек открыл приложение сам → Direct (как прямые
    заходы web). Непустой, но не рекламная сеть (трекер-партнёр, размещение) → Referrals,
    как у партнёрских меток TW.
    """
    p = (publisher or "").strip()
    if not p:
        return "Direct", "Direct", "Бесплатный"
    p_lower = p.lower()
    for needle, mapped in _APP_PUBLISHER_RULES:
        if needle in p_lower:
            return mapped
    return "Referrals", p, "Бесплатный"


# Домены-рефереры TW у organic_and_social → канон подканалов. Зонд P4 (GCC_CONTRACTS.md):
# у organic_and_social поле `campaignId` несёт НЕ id кампании, а домен-реферер, причём
# в смешанном формате — то голое имя движка ("google", "instagram"), то FQDN
# ("shopify.com", "sa.limestore.com"). Матчим вхождением подстроки.
_TW_REFERRER_RULES = (
    ("google", ("SEO", "SEO Google")),
    ("yandex", ("SEO", "SEO Yandex")),
    ("bing", ("SEO", "SEO Others")),
    ("duckduckgo", ("SEO", "SEO Others")),
    ("yahoo", ("SEO", "SEO Others")),
    ("ecosia", ("SEO", "SEO Others")),
    ("instagram", ("SMM (organic)", "Instagram")),
    ("facebook", ("SMM (organic)", "Facebook")),
    ("tiktok", ("SMM (organic)", "Tiktok")),
    ("telegram", ("SMM (organic)", "Telegram")),
    ("youtube", ("SMM (organic)", "Youtube")),
    ("pinterest", ("SMM (organic)", "Pinterest")),
    ("snapchat", ("SMM (organic)", "Snapchat")),
    # Maestra = международный бренд Mindbox, ESP самого LIME (limeshop-uae.maestra.io).
    ("maestra", ("CRM", "Mindbox")),
    ("mindbox", ("CRM", "Mindbox")),
)

# Свои витрины: переход с одного домена магазина на другой — внутренний трафик,
# не органика и не соцсеть. Совпадает с Internal у Метрики.
_OWN_DOMAINS = ("limestore.com", "lime-shop.com", "lime-shop-prod")


def split_organic_and_social(referrer: str | None) -> tuple[str, str, str]:
    """Расщепить TW `organic_and_social` на органику/соцсети/своё по домену-реферу.

    До 2026-07-19 все такие заказы падали в SEO одной кучей: SMM (organic) стоял
    с нулём заказов при живом трафике, а SEO получал чужие. Делим ПО ФАКТУ —
    пропорция по визитам не нужна (зонд P4).

    Args:
        referrer: значение `campaignId` тачпоинта organic_and_social.

    Returns:
        (channel, subchannel, traffic_type). Реферер пустой/незнакомый → Referrals/«Реферал»
        (канон витрины), т.к. переход был, но опознать площадку нечем.
    """
    ref = (referrer or "").strip().lower()
    if not ref:
        return "Others", "Organic & Social", "Бесплатный"
    if any(own in ref for own in _OWN_DOMAINS):
        return "Internal", "Internal", "Бесплатный"
    for needle, (channel, subchannel) in _TW_REFERRER_RULES:
        if needle in ref:
            return channel, subchannel, "Бесплатный"
    return "Referrals", "Реферал", "Бесплатный"


def map_tw_source(source: str | None, referrer: str | None = None) -> tuple[str, str, str]:
    """Маппинг Triple Whale attribution `source` (per-order, last touchpoint) → таксономия дашборда.

    Args:
        source: значение `attribution.<model>[0].source` (напр. "google-ads",
            "organic_and_social", "manual_mindbox", "Direct", "copilot.com", None)

    Returns:
        (channel, subchannel, traffic_type) где traffic_type ∈ {"Платный", "Бесплатный"}.
        Ключи channel/subchannel совпадают с map_metrika_channel — нужно для мержа B4.
    """
    s = (source or "").strip()
    s_lower = s.lower()

    # === Платные платформы (точные имена сервисов TW) ===
    if s_lower == "google-ads":
        return "SEM", "Google.Adwords", "Платный"
    if s_lower == "facebook-ads":
        return "SMM paid", "Meta Ads", "Платный"
    if s_lower == "pinterest-ads":
        return "SMM paid", "Pinterest Ads", "Платный"
    if s_lower == "snapchat-ads":
        return "SMM paid", "Snapchat Ads", "Платный"
    if s_lower == "tiktok-ads":
        return "SMM paid", "TikTok Ads", "Платный"
    if s_lower in ("bing", "microsoft-ads"):
        return "SEM", "Bing", "Платный"

    # === CRM (mindbox шлёт несколько source-веток: manual_mindbox, mindbox_*) ===
    if "mindbox" in s_lower:
        return "CRM", "Mindbox", "Бесплатный"
    if s_lower == "klaviyo" or s_lower == "email":
        return "CRM", "Email", "Бесплатный"

    # === Органика/соцсети — TW сводит их в один source, расщепляем по реферу ===
    if s_lower == "organic_and_social":
        return split_organic_and_social(referrer)

    # === Direct ===
    if s_lower == "direct":
        return "Direct", "Direct", "Бесплатный"

    # === Referral-домены (source = сам домен, напр. copilot.com, shop.app) ===
    if "." in s_lower:
        return "Referrals", s, "Бесплатный"

    # === QR (офлайн → онлайн) ===
    # Метрика зовёт этот источник `qrcode`, TW — `qr`; имя подканала общее, иначе визиты
    # по QR и заказы по QR стоят разными строками и CR не считается.
    if s_lower in ("qr", "qrcode"):
        return "Others", "QR", "Бесплатный"

    # === Не атрибутировано / неизвестное ===
    if not s_lower or s_lower == "non-attributed":
        return "Others", "Non-attributed", "Бесплатный"

    # === Артефакты данных, а не источники ===
    # `Excluded` — служебная пометка TW; `{{...}}` — неразвёрнутый макрос рекламной
    # системы; строка с utm_medium=/%26 — склеенный URL, попавший в поле источника.
    # Такое в Referrals пускать нельзя: это не партнёр, а мусор, и его надо видеть.
    if s_lower == "excluded" or "{{" in s or "utm_medium=" in s_lower or "%26" in s_lower:
        return "Others", s, "Бесплатный"

    # === Партнёры, PR, спецпроекты ===
    # Всё остальное, что TW не отнёс к платформе/CRM/органике/директу, — это метка
    # партнёра или размещения (shopmy, followish, pr_gcc_retail_posm, grazia_magazine).
    # По решению Павла (2026-07-19) такие источники идут в Referrals, а не в Others:
    # Others должен означать «не разобрались», а тут мы как раз разобрались.
    return "Referrals", s, "Бесплатный"
