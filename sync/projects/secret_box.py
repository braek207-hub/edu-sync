# -*- coding: utf-8 -*-
"""Токены Яндекса из Panda-BI: зеркало lib/crypto/secret-box.ts.

Формат — контракт между двумя репозиториями: `v1.<iv 12 байт>.<tag 16 байт>.<ct>`, всё base64url
без паддинга, AES-256-GCM, AAD нет. Ключ — base64 от ровно 32 байт, без хэширования.
"""

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = "v1"
IV_BYTES = 12
TAG_BYTES = 16
ENV_KEY = "PANDA_SECRET_KEY"


class SecretBoxError(ValueError):
    pass


def key_from_env() -> str:
    raw = os.environ.get(ENV_KEY, "").strip()
    if not raw:
        raise SecretBoxError(f"{ENV_KEY}: не задан")
    return raw


def _load_key(key_b64: str) -> bytes:
    try:
        key = base64.b64decode(key_b64.strip(), validate=True)
    except (binascii.Error, ValueError) as e:
        raise SecretBoxError(f"{ENV_KEY}: не base64") from e
    if len(key) != 32:
        raise SecretBoxError(f"{ENV_KEY}: нужно ровно 32 байта, получено {len(key)}")
    return key


def _b64url_decode(part: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (binascii.Error, ValueError) as e:
        raise SecretBoxError("Неверный base64url в секрете") from e


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decrypt(token: str, key_b64: str) -> str:
    key = _load_key(key_b64)
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != VERSION:
        raise SecretBoxError("Неизвестный формат зашифрованного секрета")
    iv, tag, ct = (_b64url_decode(p) for p in parts[1:])
    # Длину тега фиксируем сами: GCM иначе принимает усечённую подпись.
    if len(iv) != IV_BYTES or len(tag) != TAG_BYTES:
        raise SecretBoxError("Неверная длина iv или tag")
    try:
        return AESGCM(key).decrypt(iv, ct + tag, None).decode("utf-8")
    except (InvalidTag, UnicodeDecodeError) as e:
        raise SecretBoxError("Секрет не расшифровался: неверный ключ или повреждён") from e


def encrypt(plain: str, key_b64: str) -> str:
    key = _load_key(key_b64)
    iv = os.urandom(IV_BYTES)
    sealed = AESGCM(key).encrypt(iv, plain.encode("utf-8"), None)
    ct, tag = sealed[:-TAG_BYTES], sealed[-TAG_BYTES:]
    return ".".join([VERSION, _b64url_encode(iv), _b64url_encode(tag), _b64url_encode(ct)])
