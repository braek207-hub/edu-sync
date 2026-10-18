# -*- coding: utf-8 -*-
"""Шифрование токенов Panda-BI (lib/crypto/secret-box.ts) в Python: формат v1.<iv>.<tag>.<ct>."""

import base64

import pytest

from sync.projects.secret_box import SecretBoxError, decrypt, encrypt, key_from_env

KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="  # байты 0..31
PLAIN = "y0_тест-токен"
# Выход encryptSecret из lib/crypto/secret-box.ts с KEY (Node 24, 2026-10-18).
NODE_VECTOR = "v1.Ww6mj9QtnRYKqO8A.G3anCLmtSGQwHtAlQ0Q3uQ.rEevGSNdyMj_1wLlccn2YV1jg6RpfA"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_decrypts_node_vector():
    assert decrypt(NODE_VECTOR, KEY) == PLAIN


def test_roundtrip():
    assert decrypt(encrypt("abc", KEY), KEY) == "abc"


def test_two_encryptions_differ():
    assert encrypt("abc", KEY) != encrypt("abc", KEY)


def test_wrong_key_raises():
    other = base64.b64encode(bytes(range(1, 33))).decode()
    with pytest.raises(SecretBoxError):
        decrypt(NODE_VECTOR, other)


def test_bad_format_raises():
    parts = NODE_VECTOR.split(".")
    short_tag = ".".join([parts[0], parts[1], _b64(b"x" * 8), parts[3]])
    for bad in ("v2.a.b.c", "v1.a.b", short_tag):
        with pytest.raises(SecretBoxError):
            decrypt(bad, KEY)


def test_key_must_be_32_bytes():
    short = base64.b64encode(bytes(16)).decode()
    with pytest.raises(SecretBoxError):
        decrypt(NODE_VECTOR, short)
    with pytest.raises(SecretBoxError):
        encrypt("abc", short)


def test_key_from_env(monkeypatch):
    monkeypatch.setenv("PANDA_SECRET_KEY", f"  {KEY}\n")
    assert key_from_env() == KEY
    monkeypatch.delenv("PANDA_SECRET_KEY")
    with pytest.raises(SecretBoxError):
        key_from_env()
