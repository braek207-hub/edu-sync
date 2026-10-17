"""Лимит Reports API Директа: повторный запрос отчёта — не чаще раза в 10 секунд.

С ~11.10.2026 Директ отвечает 400 с error_code 56 («This method can't be requested more
than 1 times in 10 seconds») на опрос по retryIn=1–2 с, который сам же и выдаёт в 201/202.
Синки, ждавшие между опросами от 30 с (polinarepik, meshnflesh), не падали; LIME, EDU и
BJORN, верившие retryIn, падали каждую ночь — в GitHub Actions и после переезда.
"""
from __future__ import annotations

import json
from typing import Any

# 10 с лимита Яндекса + запас на расхождение часов.
MIN_POLL_SECONDS = 11
RATE_LIMIT_CODE = "56"


def poll_delay(retry_in: Any, default: int) -> int:
    """Пауза до следующего опроса: retryIn Директа, но не меньше лимита метода."""
    try:
        seconds = int(retry_in)
    except (TypeError, ValueError):
        seconds = default
    return max(MIN_POLL_SECONDS, seconds)


def is_rate_limited(status: int, body: str) -> bool:
    """400 с кодом 56 — «слишком часто», а не ошибка запроса: ждать и повторить."""
    if status != 400:
        return False
    try:
        return str(json.loads(body)["error"]["error_code"]) == RATE_LIMIT_CODE
    except (ValueError, KeyError, TypeError):
        return False
