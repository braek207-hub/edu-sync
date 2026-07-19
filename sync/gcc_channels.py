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


def map_channel(source: str, medium: str) -> tuple[str, str]:
    s = (source or "").lower().strip()
    m = (medium or "").lower().strip()
    paid = m in ("cpc", "cpa", "cpm", "paid", "paid_social", "display")

    if "google" in s and paid:
        return "SEM", "Google.Adwords"
    if any(x in s for x in ("facebook", "instagram", "meta", "fb", "ig")) and paid:
        return "SMM paid", "Meta Ads"
    if "tiktok" in s and paid:
        return "SMM paid", "TikTok Ads"
    if any(x in s for x in ("snapchat", "snap")) and paid:
        return "SMM paid", "Snapchat Ads"

    if "google" in s and m == "organic":
        return "SEO", "SEO Google"
    if m == "organic":
        return "SEO", "SEO Others"

    if m == "email" or "klaviyo" in s:
        return "CRM", "Email"
    if m in ("sms",):
        return "CRM", "SMS"
    if m in ("push",):
        return "CRM", "Push"

    if any(x in s for x in ("facebook", "instagram", "tiktok", "youtube", "meta")) \
            and m in ("social", "organic_social", "referral", ""):
        return "SMM (organic)", s.capitalize()

    if m == "referral":
        return "Referrals", s.capitalize() or "Referral"

    if s in ("(direct)", "(none)", "", "direct") or m in ("(none)", "none", "(not set)"):
        return "Direct", "Direct"

    return "Others", (s or m or "Unknown").capitalize()


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
