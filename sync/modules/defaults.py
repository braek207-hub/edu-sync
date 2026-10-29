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
                   "spam_rule": False, "lead_goal_ids": [], "rules": [],
                   "max_per_run": 100,
                   # Пустой список категорий — словарь выключен; отдельного enabled нет.
                   "dictionary": {"categories": ["app", "dsp", "game", "junk"]},
                   # min_cr — проценты, дробный порог законен: при целом умолчании
                   # 7.5 молча заменялся бы на 8. Panda хранит то же число 8.
                   "spam": {"min_leads": 5, "min_cr": 8.0, "ratio": 2.5,
                            "goals": "strategy"}},
    "queries": {"window_days": 30, "cpa_limit": None, "multiplier": 2.0,
                "lead_goal_ids": [], "stop_words": [], "never_minus": [],
                "max_per_run": 20, "rules": [],
                "builtin": {"phrase_waste": True, "word_zero_conv": True}},
    "audit": {"banned_words": [], "allowed_goal_ids": [], "require_utm": True,
              "title_fill_min": 0.6},
}

# Допустимые значения внутри вложенных настроек: тип проверяет _fits, а смысл —
# здесь. Опечатка в goals иначе молча считалась бы «не leads», а неизвестная
# категория висела бы в настройке, ничего не включая.
CHOICES: Dict[Tuple[str, ...], Tuple[str, ...]] = {
    ("placements", "dictionary", "categories"): ("app", "dsp", "game", "junk"),
    ("placements", "spam", "goals"): ("strategy", "leads"),
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


def _coerce(path: Tuple[str, ...], default: Any, value: Any) -> Any:
    # Вложенная настройка сливается с умолчанием поключево, а не целиком: иначе
    # spam {"min_leads": 3} без остальных порогов оставил бы прогон без ratio.
    if not _fits(default, value):
        value = default
    if isinstance(default, dict):
        return {k: _coerce(path + (k,), d, value.get(k, d)) for k, d in default.items()}
    allowed = CHOICES.get(path)
    if isinstance(value, list):
        return [x for x in value if x in allowed] if allowed else list(value)
    if allowed and value not in allowed:
        return default
    return value


def settings_for(module_key: str, raw: Optional[dict]) -> dict:
    """Настройки модуля: умолчания + то, что подошло по типу. Чужие ключи отбрасываются."""
    raw = raw if isinstance(raw, dict) else {}
    return {key: _coerce((module_key, key), default, raw.get(key, default))
            for key, default in DEFAULTS[module_key].items()}
