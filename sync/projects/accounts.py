# -*- coding: utf-8 -*-
"""Токен подключения Яндекса: зеркало lib/yandex/accounts.ts getAccessToken (Panda-BI).

Сообщения исключений фиксированные: ни тело ответа Яндекса, ни токены, ни client_secret
в них не попадают (и цепочка __cause__ обрывается — в ней мог быть текст requests).
Транзакцию коммитим сами после записи, как остальной edu-sync (sync/db.py): иначе
блокировка строки повисла бы на время всего прогона, а откат вызывающего стёр бы пометку.
"""

import os
from datetime import datetime, timedelta, timezone

import requests

from sync.projects.secret_box import decrypt, encrypt

TOKEN_URL = "https://oauth.yandex.ru/token"
TIMEOUT_S = 15
# Как REFRESH_AHEAD_MS в lib/yandex/accounts.ts: обновляем, когда до конца токена меньше недели.
REFRESH_AHEAD = timedelta(days=7)
# Оба кода Яндекс отдаёт и на отозванный refresh_token — нужно переподключение.
BAD_GRANT_ERRORS = {"invalid_grant", "bad_verification_code"}

_SELECT = (
    "SELECT access_token_enc, refresh_token_enc, expires_at, status "
    "FROM public.yandex_accounts WHERE id = %s"
)


class AccessTokenError(RuntimeError):
    pass


class NeedsReconnect(AccessTokenError):
    pass


def mark_reconnect(conn, account_id: int, reason: str) -> None:
    """Помечает подключение и его источники; КОММИТИТ. Вызывать на границе транзакции:
    незакоммиченных записей вызывающего на conn быть не должно (они уйдут вместе с коммитом)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE public.yandex_accounts SET status = 'reconnect', updated_at = now() "
            "WHERE id = %s",
            (account_id,),
        )
        cur.execute(
            "UPDATE public.project_sources SET status = 'reconnect', last_error = %s, "
            "updated_at = now() WHERE yandex_account_id = %s",
            (reason, account_id),
        )
    conn.commit()


def _load(conn, account_id: int, *, lock: bool):
    with conn.cursor() as cur:
        cur.execute(_SELECT + (" FOR UPDATE" if lock else ""), (account_id,))
        row = cur.fetchone()
    if row is None:
        raise AccessTokenError(f"Аккаунт Яндекса {account_id} не найден")
    return row


def _needs_refresh(expires_at, refresh_enc, now) -> bool:
    return not (expires_at is None or expires_at >= now + REFRESH_AHEAD or not refresh_enc)


def _request_refresh(refresh_token: str) -> dict:
    """dict ответа Яндекса; AccessTokenError без деталей при сбое сети/конфига/ответа."""
    client_id = os.environ.get("YANDEX_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("YANDEX_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise AccessTokenError("OAuth-приложение Яндекса не настроено")
    failed = False
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=TIMEOUT_S,
        )
        status = resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = {}
    except requests.RequestException:
        failed = True
    # Бросаем вне except: иначе в __context__ остался бы текст requests (мог нести секрет).
    if failed:
        raise AccessTokenError("Яндекс не ответил на обновление токена")
    if not isinstance(body, dict):
        body = {}
    if body.get("error") in BAD_GRANT_ERRORS:
        raise NeedsReconnect("Доступ к Яндексу отозван — переподключите аккаунт")
    # Как requestTokens в lib/yandex/oauth.ts: не 2xx, «error» в теле или нет access_token — сбой.
    token = body.get("access_token")
    if not 200 <= status < 300 or "error" in body or not isinstance(token, str) or not token:
        raise AccessTokenError(f"Яндекс отклонил обновление токена (HTTP {status})")
    return body


def access_token(conn, account_id: int, *, key_b64: str, now: datetime | None = None) -> str:
    """Рабочий access-токен подключения; при необходимости обновляет его. КОММИТИТ (и при
    ошибке под замком делает rollback). Вызывать на границе транзакции: незакоммиченных
    записей вызывающего на conn быть не должно."""
    now = now or datetime.now(timezone.utc)
    access_enc, refresh_enc, expires_at, status = _load(conn, account_id, lock=False)
    if status == "reconnect":
        raise NeedsReconnect("Доступ к Яндексу отозван — переподключите аккаунт")
    if not _needs_refresh(expires_at, refresh_enc, now):
        return decrypt(access_enc, key_b64)

    # Под замком перечитываем: параллельный прогон мог уже обновить токен; замок исключает
    # дублирующие обновления между процессами синка.
    try:
        return _refresh_locked(conn, account_id, key_b64, now)
    except Exception:
        conn.rollback()  # не оставлять строку под FOR UPDATE
        raise


def _refresh_locked(conn, account_id: int, key_b64: str, now: datetime) -> str:
    access_enc, refresh_enc, expires_at, status = _load(conn, account_id, lock=True)
    if status == "reconnect":
        conn.commit()
        raise NeedsReconnect("Доступ к Яндексу отозван — переподключите аккаунт")
    current = decrypt(access_enc, key_b64)
    if not _needs_refresh(expires_at, refresh_enc, now):
        conn.commit()
        return current

    try:
        body = _request_refresh(decrypt(refresh_enc, key_b64))
    except NeedsReconnect:
        mark_reconnect(conn, account_id, "Доступ к Яндексу отозван — переподключите аккаунт")
        raise
    except AccessTokenError:
        conn.commit()
        # Сбой Яндекса не значит, что доступ отозван: пока токен жив — работаем им.
        if expires_at > now:
            return current
        raise

    new_access = body["access_token"]
    new_refresh = body.get("refresh_token")
    expires_in = body.get("expires_in")
    new_expires = (
        now + timedelta(seconds=expires_in)
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool)
        else None
    )
    sets = ["access_token_enc = %s"]
    params: list = [encrypt(new_access, key_b64)]
    # Яндекс может не прислать новый refresh_token — тогда действует прежний.
    if isinstance(new_refresh, str) and new_refresh:
        sets.append("refresh_token_enc = %s")
        params.append(encrypt(new_refresh, key_b64))
    sets += ["expires_at = %s", "status = 'ok'", "updated_at = now()"]
    params += [new_expires, account_id]
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE public.yandex_accounts SET " + ", ".join(sets) + " WHERE id = %s",
            tuple(params),
        )
    conn.commit()
    return new_access
