# -*- coding: utf-8 -*-
"""РАЗОВАЯ операция: освободить слоты токенов одного кабинета VK и сразу занять один.

Зачем нужен отдельный скрипт, а не код в синке. VK держит не более 5 токенов на пару
client_id+пользователь, причём слот НЕ освобождается по истечении токена: «no more than
5 tokens may exist simultaneously, regardless of token status». Мёртвый токен занимает
место до явного удаления либо до месяца неактивности. Удаление же выборочным не бывает —
эндпоинт снимает ВСЕ токены пользователя разом, включая токен подрядчика на том же
приложении. Именно поэтому из синка отзыв вырезан навсегда (см. тест-страховку в
tests/test_lime_vk_ads.py) и живёт только здесь: под ручным запуском и подтверждением.

Порядок: удалить все токены кабинета → СРАЗУ выпустить один свой и сохранить его вместе
с refresh_token. Дальше синк живёт на обновлениях, слот больше не тратится.

ЗАПУСКАТЬ ТОЛЬКО ПО ДОГОВОРЁННОСТИ с подрядчиком: его рабочий токен умрёт, ему нужно
сразу выпустить новый.

ENV: DATABASE_URL, пары VK_CLIENT_ID[_N]/VK_CLIENT_SECRET[_N],
     FREE_VK_CLIENT_ID (какой кабинет чистим), FREE_VK_CONFIRM (слово-подтверждение).
"""
import os
import sys
import urllib.request

from sync.lime_vk_ads import (
    BASE, _cabinets, _issue_token, _mask, _save_from_response, _token_form,
)

TOKEN_DELETE_URL = f"{BASE}/oauth2/token/" + "delete.json"
CONFIRM_WORD = "DELETE-ALL-TOKENS"


def _delete_all_tokens(client_id: str, secret: str) -> None:
    """Снять ВСЕ токены пользователя в рамках этого приложения. Необратимо для всех,
    кто ходит тем же client_id, — потому и требуется подтверждение выше по стеку."""
    req = urllib.request.Request(
        TOKEN_DELETE_URL, data=_token_form(client_id, secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    urllib.request.urlopen(req, timeout=40).close()


def find_cabinet(client_id: str, cabinets: list) -> tuple:
    """Пара (client_id, secret) по client_id. Неизвестный id — отказ, а не тихий пропуск:
    промах здесь означал бы чистку НЕ ТОГО кабинета."""
    for cid, secret in cabinets:
        if cid == client_id:
            return cid, secret
    known = ", ".join(_mask(c) for c, _ in cabinets) or "(ни одного)"
    raise RuntimeError(
        f"кабинет {_mask(client_id)} не найден среди заданных в окружении: {known}")


def main() -> int:
    client_id = os.environ.get("FREE_VK_CLIENT_ID", "").strip()
    confirm = os.environ.get("FREE_VK_CONFIRM", "").strip()
    if not client_id:
        raise RuntimeError("FREE_VK_CLIENT_ID не задан — неясно, какой кабинет чистить")
    if confirm != CONFIRM_WORD:
        raise RuntimeError(
            f"подтверждение не получено: ожидается FREE_VK_CONFIRM={CONFIRM_WORD}. "
            "Операция снимает ВСЕ токены кабинета, включая токен подрядчика")

    cid, secret = find_cabinet(client_id, _cabinets())
    mask = _mask(cid)

    print(f"[free-slots] {mask}: снимаю все токены кабинета", flush=True)
    _delete_all_tokens(cid, secret)
    print(f"[free-slots] {mask}: слоты освобождены, занимаю ОДИН — выпускаю токен", flush=True)

    data = _issue_token(cid, secret)
    _save_from_response(cid, data)
    has_refresh = bool(data.get("refresh_token"))
    print(f"[free-slots] {mask}: токен выпущен и сохранён, refresh_token: "
          f"{'получен' if has_refresh else 'НЕ ПРИШЁЛ'}", flush=True)
    if not has_refresh:
        print("[free-slots] ВНИМАНИЕ: без refresh_token следующий прогон снова займёт слот — "
              "проверь ответ VK", flush=True)
    print("[free-slots] готово. Подрядчику можно выпускать свой токен", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
