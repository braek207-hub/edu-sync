"""Маппинг каналов GCC (Triple Whale / GA4 source+medium) → единая таксономия дашборда.

Отдельно от sync.lime.classify() (тот заточен под source/medium Яндекс.Метрики RU).
Стартовый набор — Google/Meta/органика/CRM/директ; расширяется по зонду позже.
"""


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


def map_metrika_channel(
    traffic_source_id: str | None, source_engine: str | None
) -> tuple[str, str, str]:
    """Маппинг Яндекс.Метрики traffic_source + sourceEngine → единая таксономия дашборда.

    Args:
        traffic_source_id: из dimensions[1].id (напр. "ad", "organic", "direct", None)
        source_engine: из dimensions[2].name (напр. "Google Ads", "Instagram", None)

    Returns:
        (channel, subchannel, traffic_type) где traffic_type ∈ {"Платный", "Бесплатный"}
    """
    source_id = (traffic_source_id or "").lower().strip()
    engine = (source_engine or "").lower().strip()

    # Ветки в порядке приоритета (проверяем engine перед генерик fallback для ad)

    # === AD (платный трафик) ===
    if source_id == "ad":
        # engine может быть None ("" после .lower().strip())
        if "google" in engine:
            return "SEM", "Google.Adwords", "Платный"
        if any(x in engine for x in ("instagram", "facebook", "meta")):
            return "SMM paid", "Meta Ads", "Платный"
        if "yandex" in engine:
            return "SEM", "Яндекс.Директ", "Платный"
        if "tiktok" in engine:
            return "SMM paid", "TikTok Ads", "Платный"
        if "snapchat" in engine:
            return "SMM paid", "Snapchat Ads", "Платный"
        # fallback для прочие ad-источники
        return "SEM", (source_engine or "Ad"), "Платный"

    # === ORGANIC (SEO) ===
    if source_id == "organic":
        if source_engine:
            return "SEO", f"SEO {source_engine}", "Бесплатный"
        return "SEO", "SEO", "Бесплатный"

    # === DIRECT ===
    if source_id == "direct":
        return "Direct", "Direct", "Бесплатный"

    # === SOCIAL (бесплатный социал) ===
    if source_id == "social":
        return "SMM (organic)", (source_engine or "Social"), "Бесплатный"

    # === REFERRAL (рефереры) ===
    if source_id == "referral":
        return "Referrals", (source_engine or "Referral"), "Бесплатный"

    # === EMAIL (CRM) ===
    if source_id == "email":
        return "CRM", "Email", "Бесплатный"

    # === MESSENGER ===
    if source_id == "messenger":
        return "SMM (organic)", "Messenger", "Бесплатный"

    # === INTERNAL ===
    if source_id == "internal":
        return "Internal", "Internal", "Бесплатный"

    # === DEFAULT (прочее, включая None) ===
    return "Others", (source_engine or "Unknown"), "Бесплатный"
