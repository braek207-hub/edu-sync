# -*- coding: utf-8 -*-
"""
sync/agent/ideas/limits.py — пределы, общие для всех генераторов идей.

Здесь живёт то, что у генераторов ОДИНАКОВО по смыслу. Копия такого предела
в каждом генераторе разъезжалась бы при первой же правке одной из копий, и
разъезд был бы невидимым: оба числа выглядят осмысленными, просто идеи одного
генератора вдруг судятся строже идей другого.

**Предел срока.** Эксперимент, которому на вердикт нужно больше квартала,
соревнуется уже не со своей гипотезой, а с сезоном: за это время меняются
спрос, конкуренты и сами кампании, и любой исход можно объяснить чем угодно.
Смысл предела один и тот же и у выноса связок в отдельную кампанию, и у
A/B-теста, поэтому и ручка одна.

Ручка, а не константа: у одного направления квартал ожидания бессмыслен, у
другого нормален, и решать это должен человек в настройках, а не автор кода.
"""

from typing import Any, Dict, Optional

MAX_HORIZON_KEY = "idea_max_horizon_days"
DEFAULT_MAX_HORIZON_DAYS = 90


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None   # NaN — то же «неизвестно»


def max_horizon(ctx: Optional[Dict[str, Any]]) -> int:
    """Сколько дней идее позволено занять — из настроек такта или по умолчанию."""
    config = (ctx or {}).get("config") or {}
    value = _number(config.get(MAX_HORIZON_KEY))
    if value is None or value <= 0:
        return DEFAULT_MAX_HORIZON_DAYS
    return int(value)
