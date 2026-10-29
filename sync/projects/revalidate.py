# -*- coding: utf-8 -*-
"""Сброс кэша Panda-BI после синка проектов: POST {PANDA_BI_URL}/api/revalidate.

Умолчания на прод нет (тот же урок, что в infra/panda/sync/revalidate.sh в Panda-BI):
без PANDA_BI_URL или CRON_SECRET сброс пропускается строкой в лог. Ошибки наружу не идут —
данные уже записаны, а кэш в худшем случае обновится по TTL.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Iterable

TIMEOUT_SEC = 60


def _log(message: str) -> None:
    print(f"[projects:revalidate] {message}", flush=True)


def revalidate(slugs: Iterable[str]) -> None:
    names = sorted(set(slugs))
    if not names:
        return
    base = os.environ.get("PANDA_BI_URL", "").strip().rstrip("/")
    secret = os.environ.get("CRON_SECRET", "").strip()
    missing = [n for n, v in (("PANDA_BI_URL", base), ("CRON_SECRET", secret)) if not v]
    if missing:
        _log(f"сброс кэша пропущен: не задано {', '.join(missing)}")
        return
    # По одному POST на slug: неизвестный Panda-BI slug (проект отключён) даёт 400 на всё тело,
    # и остальные проекты прогона остались бы со старым кэшем.
    for name in names:
        body = json.dumps({"slugs": [name]}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{base}/api/revalidate",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as resp:
                resp.read()
            _log(f"кэш сброшен: {name}")
        except Exception as exc:  # noqa: BLE001 — сброс не должен ронять синк
            _log(f"сброс кэша {name} не удался ({type(exc).__name__}): {str(exc).replace(secret, '***')[:200]}")
