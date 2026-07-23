"""Утилиты — порт фрагментов GAS utils (даты, ID, числа)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import List


def pick_index_loose(headers: List[str], variants: List[str], fallback: int = -1) -> int:
    h = [str(x or "").strip().lower() for x in headers]
    # Сначала более длинные варианты («дата создания» до «дата»)
    vars_lower = sorted(
        {str(v or "").strip().lower() for v in variants if str(v or "").strip()},
        key=len,
        reverse=True,
    )
    for i, hh in enumerate(h):
        for v in vars_lower:
            if v in hh:
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


def to_num_gas(v) -> float:
    """Строго как GAS toNum_: запятая — десятичный разделитель (2,666 → 2.666)."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        n = float(v)
        return n if n == n else 0.0
    s = str(v).strip().replace("\u00a0", "").replace(" ", "")
    if not s:
        return 0.0
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        n = float(s)
        return n if n == n else 0.0
    except ValueError:
        return 0.0


def to_num(v) -> float:
    """План/деньги: тысячи «1.222.433». Метрики Директа — to_num_gas."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        n = float(v)
        return n if n == n else 0.0
    s = str(v).strip().replace("\u00a0", "")
    if not s:
        return 0.0
    s = s.replace(" ", "")
    if "," in s and "." in s:
        # 1.234,56 → decimal comma; 1,234.56 → decimal dot
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2:
            s = parts[0] + "." + parts[1]
        else:
            s = "".join(parts)
    elif "." in s:
        parts = s.split(".")
        # 1.222.433 или 12.574 (тысячи) — точки как разделитель тысяч
        if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3):
            s = "".join(parts)
    try:
        n = float(s)
        return n if n == n else 0.0
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
    m_dot = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})", s)
    if m_dot:
        dd, mm, yy = m_dot.group(1), m_dot.group(2), m_dot.group(3)
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


def to_iso_datetime(v, tz: str = "Europe/Moscow") -> str:
    """ISO timestamp с временем. Ячейки Sheets: datetime, 'ДД.ММ.ГГГГ ЧЧ:ММ[:СС]',
    ISO, серийное число (дни от 1899-12-30). Дата без времени → T00:00:00."""
    if v is None or v == "":
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%dT%H:%M:%S")
    s = str(v).strip()
    if not s:
        return ""
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?", s)
    if m:
        dd, mm, yy = m.group(1), m.group(2), m.group(3)
        hh, mi, ss = m.group(4) or "00", m.group(5) or "00", m.group(6) or "00"
        return f"{yy}-{mm}-{dd}T{hh}:{mi}:{ss}"
    m2 = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?", s)
    if m2:
        hh, mi, ss = m2.group(2) or "00", m2.group(3) or "00", m2.group(4) or "00"
        return f"{m2.group(1)}T{hh}:{mi}:{ss}"
    try:
        serial = float(s)
        base = datetime(1899, 12, 30)
        dt = base + timedelta(days=serial)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except (ValueError, OverflowError):
        return ""


def to_datetime_ms(v, tz: str = "Europe/Moscow") -> int | None:
    """Миллисекунды UTC для datetime из Sheets (как GAS toDateTimeMsSafe_)."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        d = v
    else:
        iso = to_iso_date(v, tz)
        if not iso:
            return None
        try:
            from zoneinfo import ZoneInfo

            d = datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=ZoneInfo(tz))
        except (ValueError, TypeError):
            return None
    try:
        from zoneinfo import ZoneInfo

        if d.tzinfo is None:
            d = d.replace(tzinfo=ZoneInfo(tz))
        return int(d.timestamp() * 1000)
    except (ValueError, TypeError, OSError):
        return None
