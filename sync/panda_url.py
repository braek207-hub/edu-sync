# -*- coding: utf-8 -*-
"""Адрес Panda-BI, в который синки шлют заказы (ingest) и сброс кэша (revalidate).

База — из PANDA_BI_URL: это имя уже передают юниты panda-sync@ на сервере (sync.env,
http://127.0.0.1:3100) и читал scripts/revalidate_panda.py. Умолчание — прод на Vercel:
прогоны GitHub Actions адрес не передают, и до дня переключения ходят туда же, что и раньше.
Пустое значение равно отсутствию: GitHub отдаёт незаведённый секрет пустой строкой.
"""
import os

DEFAULT_BASE_URL = "https://panda-bi.vercel.app"


def panda_base_url() -> str:
    return (os.environ.get("PANDA_BI_URL") or "").strip().rstrip("/") or DEFAULT_BASE_URL


def ingest_url(slug: str, env_name: str) -> str:
    """Свой адрес приёма (<X>_INGEST_URL) главнее базы: так он задан у юнитов сейчас."""
    return (os.environ.get(env_name) or "").strip() or f"{panda_base_url()}/api/ingest/{slug}"
