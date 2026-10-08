# -*- coding: utf-8 -*-
"""Сброс кэша Panda-BI после синка: слаги своего дашборда + Bearer CRON_SECRET, сеть — мок."""
import http.client
import io
import json
import os
import socket
import urllib.error
from unittest.mock import patch

from scripts import revalidate_panda as m


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run(argv, env):
    sent = []

    def fake_urlopen(req, timeout):
        sent.append(req)
        return _Resp(b'{"revalidated":true}')

    with patch.dict(os.environ, env, clear=False), patch.object(
        m.urllib.request, "urlopen", fake_urlopen
    ):
        code = m.main(argv)
    return code, sent


def test_posts_own_slugs_with_bearer():
    code, sent = _run(["bjorn", "meshnflesh"], {"CRON_SECRET": "s3", "PANDA_BI_URL": ""})
    assert code == 0
    (req,) = sent
    assert req.full_url == "https://panda-bi.vercel.app/api/revalidate"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer s3"
    assert req.get_header("Content-type").startswith("application/json")
    assert json.loads(req.data.decode("utf-8")) == {"slugs": ["bjorn", "meshnflesh"]}


def test_base_url_override():
    _, sent = _run(["lime"], {"CRON_SECRET": "s3", "PANDA_BI_URL": "http://localhost:3000/"})
    assert sent[0].full_url == "http://localhost:3000/api/revalidate"


def test_without_secret_fails_and_stays_offline():
    """Без секрета — красный шаг, а не анонимный вызов: иначе после выключения переходного
    режима сброс тихо превратился бы в 401."""
    code, sent = _run(["lime"], {"CRON_SECRET": ""})
    assert code == 1
    assert sent == []


def test_unknown_or_missing_slug_rejected_before_network():
    assert _run(["limee"], {"CRON_SECRET": "s3"}) == (2, [])
    assert _run([], {"CRON_SECRET": "s3"}) == (2, [])


def test_http_error_is_red(capsys):
    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"Unauthorized"}'))

    with patch.dict(os.environ, {"CRON_SECRET": "bad"}), patch.object(m.urllib.request, "urlopen", boom):
        assert m.main(["lime"]) == 1
    out = capsys.readouterr().out
    assert "401" in out
    assert "bad" not in out  # секрет в лог не уходит


def test_secret_trailing_newline_is_stripped():
    """Секрет из буфера с переводом строки: без strip http.client бросает ValueError с repr
    заголовка — секрет оказывается в логе Actions."""
    code, sent = _run(["lime"], {"CRON_SECRET": "s3cr3t\n"})
    assert code == 0
    header = sent[0].get_header("Authorization")
    assert header == "Bearer s3cr3t"

    conn = http.client.HTTPConnection("panda-bi.invalid")
    conn.putrequest("POST", "/api/revalidate")
    conn.putheader("Authorization", header)  # очищенный заголовок http.client принимает
    try:
        conn.putheader("Authorization", "Bearer s3cr3t\n")
    except ValueError as e:
        assert "s3cr3t" in str(e)  # то, от чего защищает strip
    else:
        raise AssertionError("http.client должен отвергать перевод строки в заголовке")


def test_whitespace_only_secret_counts_as_missing():
    assert _run(["lime"], {"CRON_SECRET": " \n"}) == (1, [])


def _run_failing(fake_urlopen, capsys):
    with patch.dict(os.environ, {"CRON_SECRET": "s3cr3t"}), patch.object(
        m.urllib.request, "urlopen", fake_urlopen
    ):
        code = m.main(["lime"])
    return code, capsys.readouterr().out


def test_connect_timeout_is_red_without_secret(capsys):
    def connect_timeout(req, timeout):
        raise urllib.error.URLError(socket.timeout("timed out"))

    code, out = _run_failing(connect_timeout, capsys)
    assert code == 1
    assert out.startswith("::error::")
    assert "s3cr3t" not in out


def test_read_timeout_is_red_without_secret(capsys):
    """Таймаут чтения ответа — голый TimeoutError из resp.read(), мимо URLError."""

    class _SlowResp(_Resp):
        def read(self, *a):
            raise TimeoutError("timed out")

    code, out = _run_failing(lambda req, timeout: _SlowResp(b""), capsys)
    assert code == 1
    assert out.startswith("::error::")
    assert "s3cr3t" not in out
