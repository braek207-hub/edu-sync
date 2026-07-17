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
