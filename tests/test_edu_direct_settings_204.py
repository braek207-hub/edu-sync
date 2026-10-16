# -*- coding: utf-8 -*-
"""204 с пустым телом от JSON API Директа — балансер Яндекса при лежащем API (11.10.2026).

meshnflesh_direct_settings падал на нём JSONDecodeError из r.json() в
_list_campaigns_for_login: по логу не было видно, что недоступен Директ. Теперь —
повтор, а не поднялся — понятная ошибка «Директ API 204 (недоступен)».
"""
import json

import pytest

import sync.direct_settings_cabinets as C
from sync import edu_direct_settings as S


class _Resp:
    def __init__(self, status, body=b""):
        self.status_code = status
        self.content = body
        self.encoding = None

    @property
    def text(self):
        return self.content.decode("utf-8")

    def json(self):
        return json.loads(self.content.decode("utf-8"))


def _ok(campaigns):
    return _Resp(200, json.dumps({"result": {"Campaigns": campaigns}}).encode("utf-8"))


@pytest.fixture
def api(monkeypatch):
    """Очередь ответов вместо сети; паузы повторов — в список, не в sleep."""
    queue, sleeps = [], []
    monkeypatch.setattr(S.requests, "post", lambda *a, **kw: queue.pop(0))
    monkeypatch.setattr(S.time, "sleep", sleeps.append)
    monkeypatch.setenv("DIRECT_TOKEN", "t")
    monkeypatch.setattr(S, "_CURRENT_LOGIN", "meshnflesh")
    return queue, sleeps


def test_204_then_ok_is_retried(api):
    queue, sleeps = api
    queue += [_Resp(204), _Resp(204), _ok([{"Id": 7, "Name": "к"}])]

    assert S._list_campaigns_for_login() == {"7": "к"}
    assert sleeps == [5, 10]


def test_204_to_the_end_is_a_clear_error_not_json_decode(api):
    queue, sleeps = api
    queue += [_Resp(204)] * S._API_ATTEMPTS

    with pytest.raises(RuntimeError, match=r"Директ API 204 \(недоступен\)"):
        S._list_campaigns_for_login()
    assert len(sleeps) == S._API_ATTEMPTS - 1
    assert queue == []


def test_empty_200_is_treated_as_outage(api):
    queue, _ = api
    queue += [_Resp(200, b"  ")] * S._API_ATTEMPTS

    with pytest.raises(RuntimeError, match="недоступен"):
        S._direct_post(S.CAMPAIGNS_URL, {"method": "get"})


def test_api_error_body_is_not_read_as_zero_campaigns(api):
    queue, sleeps = api
    queue.append(_Resp(200, json.dumps({"error": {"error_code": 53}}).encode("utf-8")))

    with pytest.raises(RuntimeError, match="53"):
        S._list_campaigns_for_login()
    assert sleeps == []


def test_client_error_is_not_retried(api):
    queue, sleeps = api
    queue.append(_Resp(400, b"bad request"))

    with pytest.raises(RuntimeError, match="400"):
        S._direct_post(S.CAMPAIGNS_URL, {"method": "get"})
    assert sleeps == []


def test_meshnflesh_cabinet_fails_red_with_readable_reason(api, monkeypatch, capsys):
    queue, _ = api
    queue += [_Resp(204)] * S._API_ATTEMPTS
    monkeypatch.setenv("MESHNFLESH_DIRECT_TOKEN", "t")
    monkeypatch.delenv("MESHNFLESH_YANDEX_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="meshnflesh"):
        C.sync_cabinet("meshnflesh")
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "Директ API 204 (недоступен)" in out
    assert "JSONDecodeError" not in out
