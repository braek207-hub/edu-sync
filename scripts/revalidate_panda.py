# -*- coding: utf-8 -*-
"""Сброс серверного кэша Panda-BI после синка — только для дашбордов, чьи данные он записал.

    python scripts/revalidate_panda.py lime [bjorn ...]

Panda-BI (/api/revalidate) сбрасывает тег `<slug>-data` названных дашбордов и сразу после
ответа их прогревает. Раньше каждый синк сбрасывал все пять дашбордов: синк EDU посреди дня
отдавал LIME холодным (сборка 13–28 с) первому зашедшему.

Env:
    CRON_SECRET   — тот же секрет, что в Vercel (Bearer); без него роут отвечает 401.
    PANDA_BI_URL  — база Panda-BI, по умолчанию прод.
"""
import json
import os
import socket
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://panda-bi.vercel.app"
# Слаги, у которых в Panda-BI есть тег данных (lib/cache-tags.ts, DATA_SLUGS). Опечатку ловим
# здесь, до сети: в логе синка она понятнее, чем 400 от сервера.
SLUGS = ("lime", "edunetwork", "bjorn", "polinarepik", "meshnflesh")


def build_request(slugs, secret, base_url=DEFAULT_URL):
    body = json.dumps({"slugs": list(slugs)}, ensure_ascii=False).encode("utf-8")
    return urllib.request.Request(
        base_url.rstrip("/") + "/api/revalidate",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )


def main(argv=None):
    slugs = list(sys.argv[1:] if argv is None else argv)
    if not slugs:
        print("usage: python scripts/revalidate_panda.py <slug> [<slug> ...]", file=sys.stderr)
        return 2
    unknown = [s for s in slugs if s not in SLUGS]
    if unknown:
        print(f"::error::неизвестные слаги: {', '.join(unknown)}; допустимы: {', '.join(SLUGS)}")
        return 2
    # strip: хвостовой перевод строки (секрет, вставленный из буфера) http.client отвергает
    # ValueError'ом с repr заголовка — то есть печатает секрет в лог.
    secret = os.environ.get("CRON_SECRET", "").strip()
    if not secret:
        print("::error::CRON_SECRET не задан — кэш Panda-BI не сброшен")
        return 1

    req = build_request(slugs, secret, os.environ.get("PANDA_BI_URL") or DEFAULT_URL)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            print(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        print(f"::error::Panda-BI /api/revalidate ответил {e.code}: {detail}")
        return 1
    except urllib.error.URLError as e:
        print(f"::error::Panda-BI /api/revalidate недоступен: {e.reason}")
        return 1
    except (TimeoutError, socket.timeout) as e:
        # Таймаут чтения ответа приходит голым TimeoutError, а не URLError.
        print(f"::error::Panda-BI /api/revalidate не ответил за 60 с: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
