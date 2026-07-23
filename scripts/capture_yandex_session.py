# -*- coding: utf-8 -*-
"""
Разовый захват сессии Яндекса для headless-ботов медийки (Медиаметрика/Геомедийка),
у которых нет API. Запускается ЛОКАЛЬНО (не в CI) — открывает браузер, ты логинишься
(с 2FA), скрипт сохраняет cookie/сессию в файл. Файл → секрет edu-sync, бот в CI его
переиспользует, пока сессия жива.

Установка (один раз):
    pip install playwright
    playwright install chromium

Запуск:
    python scripts/capture_yandex_session.py

Что делать в открывшемся браузере:
    1) Войти в нужный аккаунт Яндекса (performance21lime / тот, где медийка).
    2) Открыть кабинет Медиаметрики (media.metrika / metrika для медийной рекламы).
    3) Открыть кабинет Геомедийки (Яндекс Бизнес, наружная/гео-реклама).
    4) Вернуться в терминал и нажать Enter — сессия сохранится.

Результат: файл yandex_storage_state.json рядом. Дальше его кладём в секрет.
"""
from playwright.sync_api import sync_playwright

OUT = "yandex_storage_state.json"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto("https://passport.yandex.ru/")

        print("\n" + "=" * 60)
        print("1) Залогинься в аккаунт Яндекса (с 2FA).")
        print("2) Открой кабинет МЕДИАМЕТРИКИ и дойди до отчёта со статистикой.")
        print("3) Открой кабинет ГЕОМЕДИЙКИ (Яндекс Бизнес) и дойди до статистики.")
        print("4) Скопируй URL обеих страниц статистики — пришли их в чат.")
        print("=" * 60)
        input("\nКогда залогинился в оба кабинета — нажми ENTER для сохранения сессии...")

        ctx.storage_state(path=OUT)
        print(f"\n✓ Сессия сохранена в {OUT}")
        print("  Дальше: скажи в чате «готово» — я заберу файл в секрет edu-sync.")
        browser.close()


if __name__ == "__main__":
    main()
