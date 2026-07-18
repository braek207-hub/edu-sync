import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.gcc_channels import map_channel, map_metrika_channel, map_tw_source


def test_google_ads_paid():
    assert map_channel("google", "cpc") == ("SEM", "Google.Adwords")


def test_meta_paid():
    assert map_channel("facebook", "cpc") == ("SMM paid", "Meta Ads")
    assert map_channel("instagram", "paid") == ("SMM paid", "Meta Ads")


def test_organic_search():
    assert map_channel("google", "organic") == ("SEO", "SEO Google")


def test_direct():
    assert map_channel("(direct)", "(none)") == ("Direct", "Direct")


def test_email_crm():
    assert map_channel("klaviyo", "email") == ("CRM", "Email")


def test_unknown_fallback():
    ch, sub = map_channel("weirdsource", "")
    assert ch == "Others"


# Tests for Metrika channel mapping
def test_metrika_ad_google():
    assert map_metrika_channel("ad", "Google Ads") == ("SEM", "Google.Adwords", "Платный")


def test_metrika_ad_meta():
    assert map_metrika_channel("ad", "Instagram") == ("SMM paid", "Meta Ads", "Платный")
    assert map_metrika_channel("ad", "Facebook") == ("SMM paid", "Meta Ads", "Платный")


def test_metrika_organic():
    assert map_metrika_channel("organic", "Google") == ("SEO", "SEO Google", "Бесплатный")


def test_metrika_direct_none_engine():
    assert map_metrika_channel("direct", None) == ("Direct", "Direct", "Бесплатный")


def test_metrika_email():
    assert map_metrika_channel("email", None) == ("CRM", "Email", "Бесплатный")


def test_metrika_referral():
    assert map_metrika_channel("referral", "limestore.com") == ("Referrals", "limestore.com", "Бесплатный")


def test_metrika_unknown():
    ch, sub, tt = map_metrika_channel(None, None)
    assert ch == "Others" and tt == "Бесплатный"


# Tests for Triple Whale source mapping
def test_tw_google():
    assert map_tw_source("google-ads") == ("SEM", "Google.Adwords", "Платный")


def test_tw_meta():
    assert map_tw_source("facebook-ads") == ("SMM paid", "Meta Ads", "Платный")


def test_tw_snapchat():
    assert map_tw_source("snapchat-ads") == ("SMM paid", "Snapchat Ads", "Платный")


def test_tw_tiktok():
    assert map_tw_source("tiktok-ads") == ("SMM paid", "TikTok Ads", "Платный")


def test_tw_bing():
    assert map_tw_source("bing") == ("SEM", "Bing", "Платный")
    assert map_tw_source("microsoft-ads") == ("SEM", "Bing", "Платный")


def test_tw_organic_social():
    assert map_tw_source("organic_and_social") == ("SEO", "Organic & Social", "Бесплатный")


def test_tw_mindbox():
    assert map_tw_source("manual_mindbox") == ("CRM", "Mindbox", "Бесплатный")


def test_tw_klaviyo_email():
    assert map_tw_source("klaviyo") == ("CRM", "Email", "Бесплатный")
    assert map_tw_source("email") == ("CRM", "Email", "Бесплатный")


def test_tw_direct():
    assert map_tw_source("Direct") == ("Direct", "Direct", "Бесплатный")


def test_tw_referral_domain():
    assert map_tw_source("copilot.com") == ("Referrals", "copilot.com", "Бесплатный")


def test_tw_non_attributed():
    assert map_tw_source("non-attributed") == ("Others", "Non-attributed", "Бесплатный")


def test_tw_none():
    ch, sub, tt = map_tw_source(None)
    assert ch == "Others" and tt == "Бесплатный"


def test_tw_catch_all():
    ch, sub, tt = map_tw_source("some_weird_source")
    assert ch == "Others" and sub == "some_weird_source" and tt == "Бесплатный"


def test_tw_pinterest():
    assert map_tw_source("pinterest-ads") == ("SMM paid", "Pinterest Ads", "Платный")
