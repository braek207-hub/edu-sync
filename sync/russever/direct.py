# -*- coding: utf-8 -*-
"""Тонкий клиент Директа для РосСеверЭкспо.

Комбинаторные объявления (ResponsiveAd) живут только в API v501 — в v5 их
структуры нет, поэтому здесь свой вызов, а не общий sync.placements.direct.
"""
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

API501 = "https://api.direct.yandex.com/json/v501/"
LOGIN = "orso-groupmedia"
TOKEN_ENV = "RUSSEVER_DIRECT"


def token() -> str:
    return os.environ.get(TOKEN_ENV, "").strip()


def call(service: str, method: str, params: Dict[str, Any],
         tok: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Вызов API. None вместо исключения: напоминание важнее, чем полнота
    сверки, — без Директа сообщение уйдёт с пометкой «кабинет не прочитан»."""
    tok = tok or token()
    if not tok:
        return None
    body = json.dumps({"method": method, "params": params},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API501 + service, data=body, headers={
        "Authorization": "Bearer " + tok,
        "Client-Login": LOGIN,
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
    except Exception:
        return None
    try:
        j = json.loads(raw)
    except ValueError:
        return None
    return None if "error" in j else j.get("result")


def group_titles(group_id: int, tok: Optional[str] = None) -> List[str]:
    """Заголовки комбинаторного объявления в группе — по ним видно, какая
    фаза сейчас залита в кабинет."""
    r = call("ads", "get", {
        "SelectionCriteria": {"AdGroupIds": [group_id], "States": ["ON", "OFF"]},
        "FieldNames": ["Id", "Type", "State"],
        "ResponsiveAdFieldNames": ["Titles"],
        "Page": {"Limit": 50}}, tok=tok)
    if not r:
        return []
    out: List[str] = []
    for ad in r.get("Ads", []):
        if ad.get("Type") != "RESPONSIVE_AD":
            continue
        for t in (ad.get("ResponsiveAd", {}).get("Titles") or []):
            title = t.get("Title") if isinstance(t, dict) else t
            if title:
                out.append(title)
    return out
