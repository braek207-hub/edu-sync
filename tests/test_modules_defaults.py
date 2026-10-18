# -*- coding: utf-8 -*-
from sync.modules.defaults import ALLOWED_MODES, DEFAULTS, MODULE_KEYS, settings_for


def test_defaults_pinned():
    assert MODULE_KEYS == ("placements", "queries", "audit")
    assert DEFAULTS == {
        "placements": {"top_n": 30, "always_block": [], "extra_allow": [],
                       "spam_rule": False},
        "queries": {"window_days": 30, "cpa_limit": None, "multiplier": 2.0,
                    "goal_ids": [], "stop_words": [], "never_minus": [],
                    "max_per_run": 20},
        "audit": {"banned_words": [], "allowed_goal_ids": [],
                  "require_utm": True, "title_fill_min": 0.6},
    }


def test_settings_for_merges_and_coerces():
    s = settings_for("queries", {"window_days": "abc", "foo": 1, "multiplier": 3,
                                 "cpa_limit": 500})
    assert s["window_days"] == 30
    assert "foo" not in s
    assert s["multiplier"] == 3 and s["cpa_limit"] == 500
    assert settings_for("audit", None) == DEFAULTS["audit"]


def test_settings_for_does_not_share_lists():
    settings_for("placements", None)["always_block"].append("x")
    assert DEFAULTS["placements"]["always_block"] == []


def test_allowed_modes():
    assert ALLOWED_MODES["audit"] == ("off", "report")
    assert ALLOWED_MODES["queries"] == ("off", "report", "approve", "auto")
