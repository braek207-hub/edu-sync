# -*- coding: utf-8 -*-
"""
sync/lime_media_session.py — общий помощник для headless-ботов медийки без API
(Медиаметрика, Геомедийка). Поднимает Playwright-контекст по сохранённой сессии
Яндекса из секрета YANDEX_STORAGE_STATE (base64 JSON storageState).

Сессия захватывается разово локально (scripts/capture_yandex_session.py) — 2FA
Яндекса автоматизировать нельзя. Протухает раз в N недель → перезахват.
"""
import os
import base64
import tempfile
from contextlib import contextmanager


def _write_state_file() -> str:
    b64 = os.environ.get("YANDEX_STORAGE_STATE", "").strip()
    if not b64:
        raise RuntimeError("YANDEX_STORAGE_STATE не задан (base64 storageState Яндекса)")
    raw = base64.b64decode(b64)
    fd, path = tempfile.mkstemp(suffix=".json", prefix="yastate_")
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return path


@contextmanager
def yandex_page(origin_url: str):
    """Даёт Playwright page на нужном origin, залогиненный по сессии.
    In-page fetch к внутренним API идёт с корректными куками/заголовками."""
    from playwright.sync_api import sync_playwright

    state_path = _write_state_file()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(storage_state=state_path)
        page = ctx.new_page()
        try:
            page.goto(origin_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            yield page
        finally:
            browser.close()
            try:
                os.remove(state_path)
            except OSError:
                pass


# JS: in-page fetch (браузер сам добавляет куки/CSRF/референс) → {status, body}
FETCH_JS = """async (path) => {
  const r = await fetch(path, {credentials:'include', headers:{'Accept':'application/json'}});
  return {status: r.status, body: await r.text()};
}"""

def page_fetch_json(page, path: str) -> dict:
    """GET через in-page fetch; возвращает распарсенный JSON или бросает с диагностикой."""
    import json
    res = page.evaluate(FETCH_JS, path)
    if res["status"] != 200:
        raise RuntimeError(f"fetch {res['status']}: {path}\n{res['body'][:300]}")
    body = res["body"]
    if not body.startswith("{"):
        raise RuntimeError(f"не JSON от {path}: {body[:200]}")
    return json.loads(body)
