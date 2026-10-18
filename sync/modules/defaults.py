# -*- coding: utf-8 -*-
"""Умолчания модулей Директа и приведение сырых настроек к ним.

Те же значения закреплены тестом в Panda-BI: расхождение между репозиториями
молча меняет поведение прогона, поэтому правятся только вместе.
"""

from typing import Any, Dict, Optional, Tuple

MODULE_KEYS: Tuple[str, ...] = ("placements", "queries", "audit")

# audit только сообщает: записи в Директ у него нет, поэтому approve/auto ему закрыты.
ALLOWED_MODES: Dict[str, Tuple[str, ...]] = {
    "placements": ("off", "report", "approve", "auto"),
    "queries": ("off", "report", "approve", "auto"),
    "audit": ("off", "report"),
}

DEFAULTS: Dict[str, Dict[str, Any]] = {
    "placements": {"top_n": 30, "always_block": [], "extra_allow": [],
                   "spam_rule": False},
    "queries": {"window_days": 30, "cpa_limit": None, "multiplier": 2.0,
                "goal_ids": [], "stop_words": [], "never_minus": [],
                "max_per_run": 20},
    "audit": {"banned_words": [], "allowed_goal_ids": [], "require_utm": True,
              "title_fill_min": 0.6},
}


def _fits(default: Any, value: Any) -> bool:
    if default is None:
        # cpa_limit: пусто или число; bool — не число, хоть и подкласс int.
        return value is None or (isinstance(value, (int, float))
                                 and not isinstance(value, bool))
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return (isinstance(value, (int, float))
                and not isinstance(value, bool))
    if isinstance(default, list):
        return isinstance(value, list)
    return isinstance(value, type(default))


def settings_for(module_key: str, raw: Optional[dict]) -> dict:
    """Настройки модуля: умолчания + то, что подошло по типу. Чужие ключи отбрасываются."""
    base = DEFAULTS[module_key]
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, Any] = {}
    for key, default in base.items():
        value = raw.get(key, default)
        if not _fits(default, value):
            value = default
        out[key] = list(value) if isinstance(value, list) else value
    return out
