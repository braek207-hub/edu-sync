# -*- coding: utf-8 -*-
from sync.modules.reasons import REASON_CODES


def test_reason_codes_pinned():
    # Тот же список и порядок закреплены в Panda (lib/modules/reasons.ts).
    assert REASON_CODES == (
        "in_never_list", "system_allowlist", "user_protect", "already_excluded_not_robot",
        "list_full", "campaign_type_unsupported", "campaign_stopped", "over_cap",
        "in_never_minus", "conflicts_keyword", "covered_by_minus", "family_pays_off",
        "no_lead_goals", "goal_not_visible", "non_rub_money", "report_failed", "no_access",
        "direct_rejected", "too_many_goals", "rule_invalid",
        "phrase_invalid", "report_goals_over_cap",
    )
    assert len(set(REASON_CODES)) == len(REASON_CODES)
