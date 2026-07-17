import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.gcc_channels import map_channel


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
