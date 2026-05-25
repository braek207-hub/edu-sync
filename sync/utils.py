"""Утилиты — порт фрагментов GAS utils (даты, ID, числа)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import List


def pick_index_loose(headers: List[str], variants: List[str], fallback: int = -1) -> int:
    h = [str(x or "").strip().lower() for x in headers]
    vars_lower = [str(v or "").strip().lower() for v in variants]
    for i, hh in enumerate(h):
        for v in vars_lower:
            if v and v in hh:
                return i
    return fallback


def normalize_campaign_id(raw) -> str:
    s = str(raw if raw is not None else "").strip()
    if not s:
        return ""
    s = s.replace("\u00a0", "").replace(" ", "")
    if re.fullmatch(r"\d+\.0+", s):
        s = re.sub(r"\.0+$", "", s)
    return s


def to_num(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return 0.0
    s = s.replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        n = float(s)
        return n if n == n else 0.0  # NaN check
    except ValueError:
        return 0.0


def to_iso_date(v, tz: str = "Europe/Moscow") -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    if not s:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", s):
        dd, mm, yy = s.split(".")
        return f"{yy}-{mm}-{dd}"
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    if re.fullmatch(r"\d{8}", s):
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    try:
        from zoneinfo import ZoneInfo

        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=ZoneInfo(tz))
        return d.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        pass
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d")
        return d.strftime("%Y-%m-%d")
    except ValueError:
        return ""
