# -*- coding: utf-8 -*-
"""Коды отказов и ошибок модулей.

Python пишет только код, текст и совет рисует Panda (lib/modules/reasons.ts).
Список и порядок закреплены тестами в обоих репо: новый код без пары на той
стороне показал бы человеку «неизвестную причину».
"""

from typing import Tuple

REASON_CODES: Tuple[str, ...] = (
    # Защиты и границы записи.
    "in_never_list", "system_allowlist", "user_protect", "already_excluded_not_robot",
    "list_full", "campaign_type_unsupported", "campaign_stopped", "over_cap",
    # Защиты минус-фраз.
    "in_never_minus", "conflicts_keyword", "covered_by_minus", "family_pays_off",
    # Данные и доступ.
    "no_lead_goals", "goal_not_visible", "non_rub_money", "report_failed", "no_access",
    "direct_rejected", "too_many_goals", "rule_invalid",
)
