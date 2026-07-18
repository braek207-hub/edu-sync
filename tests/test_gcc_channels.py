import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.gcc_channels import map_channel, map_metrika_channel


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
