# -*- coding: utf-8 -*-
from sync.modules.defaults import ALLOWED_MODES, DEFAULTS, MODULE_KEYS, settings_for


def test_defaults_pinned():
    assert MODULE_KEYS == ("placements", "queries", "audit")
    assert DEFAULTS == {
        "placements": {"logins": [], "campaigns": {}, "top_n": 30, "always_block": [], "extra_allow": [],
                       "spam_rule": False, "lead_goal_ids": [], "rules": [],
                       "max_per_run": 100,
                       "dictionary": {"categories": ["app", "dsp", "game", "junk"]},
                       "spam": {"min_leads": 5, "min_cr": 8, "ratio": 2.5,
                                "goals": "strategy"}},
        "queries": {"logins": [], "campaigns": {}, "window_days": 30, "cpa_limit": None, "multiplier": 2.0,
                    "lead_goal_ids": [], "stop_words": [], "never_minus": [],
                    "max_per_run": 20, "rules": [],
                    "builtin": {"phrase_waste": True, "word_zero_conv": True}},
        "audit": {"logins": [], "campaigns": {}, "banned_words": [], "allowed_goal_ids": [],
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


def test_goal_ids_key_gone():
    assert "goal_ids" not in settings_for("queries", {"goal_ids": [1]})


def test_nested_dict_merged_per_key():
    s = settings_for("placements", {"spam": {"min_leads": 3, "ratio": "x", "junk": 1,
                                             "min_cr": 7.5}})
    assert s["spam"] == {"min_leads": 3, "min_cr": 7.5, "ratio": 2.5, "goals": "strategy"}
    s = settings_for("queries", {"builtin": {"phrase_waste": False}})
    assert s["builtin"] == {"phrase_waste": False, "word_zero_conv": True}
    s = settings_for("placements", {"dictionary": {"categories": ["app"], "enabled": 1}})
    assert s["dictionary"] == {"categories": ["app"]}
    for bad in ("app", ["app"], None, 5):
        assert settings_for("placements", {"dictionary": bad})["dictionary"] == {
            "categories": ["app", "dsp", "game", "junk"]}
    s = settings_for("placements", {"dictionary": {"categories": "app"}})
    assert s["dictionary"]["categories"] == ["app", "dsp", "game", "junk"]


def test_nested_lists_not_shared():
    settings_for("placements", None)["dictionary"]["categories"].append("site")
    assert DEFAULTS["placements"]["dictionary"]["categories"] == ["app", "dsp", "game", "junk"]
    raw = {"dictionary": {"categories": ["app"]}}
    settings_for("placements", raw)["dictionary"]["categories"].append("dsp")
    assert raw["dictionary"]["categories"] == ["app"]


def test_rules_list_passes_through():
    rule = {"id": "abc123", "name": "x"}
    assert settings_for("placements", {"rules": [rule]})["rules"] == [rule]
    assert settings_for("queries", {"rules": {"id": "abc123"}})["rules"] == []


def test_nested_choices_validated():
    s = settings_for("placements", {"dictionary": {"categories": ["game", "casino", "site", 1]}})
    assert s["dictionary"]["categories"] == ["game"]
    # Пустой список — словарь выключен, умолчанием не подменяется.
    assert settings_for("placements", {"dictionary": {"categories": []}})["dictionary"] == {
        "categories": []}
    assert settings_for("placements", {"spam": {"goals": "leads"}})["spam"]["goals"] == "leads"
    for bad in ("lead", "LEADS", ""):
        assert settings_for("placements", {"spam": {"goals": bad}})["spam"]["goals"] == "strategy"


def test_logins_list_kept_and_bad_type_falls_back():
    assert settings_for("placements", {"logins": ["a", "b"]})["logins"] == ["a", "b"]
    assert settings_for("audit", {"logins": "a"})["logins"] == []
    assert settings_for("queries", None)["logins"] == []


def test_campaigns_map_kept_ids_normalized():
    s = settings_for("queries", {"campaigns": {" vuz ": ["123", 45, "x", 0, True, 45],
                                               "empty": [], "bad": "1", 5: [1]}})
    assert s["campaigns"] == {"vuz": [123, 45]}
    for bad in (None, [], "x", 5):
        assert settings_for("audit", {"campaigns": bad})["campaigns"] == {}
    assert settings_for("placements", None)["campaigns"] == {}
