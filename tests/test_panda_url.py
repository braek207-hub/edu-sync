# -*- coding: utf-8 -*-
"""База Panda-BI из PANDA_BI_URL: юниты сервера шлют на пилот, GitHub — по-прежнему в прод."""
import importlib

import pytest

from sync import panda_url as P


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("PANDA_BI_URL", "MESHNFLESH_INGEST_URL", "POLINAREPIK_INGEST_URL"):
        monkeypatch.delenv(k, raising=False)


def test_default_is_prod_until_cutover():
    assert P.panda_base_url() == "https://panda-bi.ru"


def test_empty_value_means_unset(monkeypatch):
    # GitHub отдаёт незаведённый секрет пустой строкой — это не адрес «».
    monkeypatch.setenv("PANDA_BI_URL", "  ")
    monkeypatch.setenv("MESHNFLESH_INGEST_URL", "")
    assert P.ingest_url("meshnflesh", "MESHNFLESH_INGEST_URL") == \
        "https://panda-bi.ru/api/ingest/meshnflesh"


def test_base_from_env_feeds_ingest(monkeypatch):
    monkeypatch.setenv("PANDA_BI_URL", "http://127.0.0.1:3100/")
    assert P.ingest_url("polinarepik", "POLINAREPIK_INGEST_URL") == \
        "http://127.0.0.1:3100/api/ingest/polinarepik"


def test_own_ingest_url_wins(monkeypatch):
    monkeypatch.setenv("PANDA_BI_URL", "http://127.0.0.1:3100")
    monkeypatch.setenv("MESHNFLESH_INGEST_URL", "http://127.0.0.1:3100/api/ingest/x")
    assert P.ingest_url("meshnflesh", "MESHNFLESH_INGEST_URL") == "http://127.0.0.1:3100/api/ingest/x"


@pytest.mark.parametrize("module,slug", [("sync.meshnflesh_orders", "meshnflesh"),
                                         ("sync.polinarepik_orders", "polinarepik")])
def test_orders_modules_take_base_from_env(monkeypatch, module, slug):
    monkeypatch.setenv("PANDA_BI_URL", "http://127.0.0.1:3100")
    mod = importlib.reload(importlib.import_module(module))
    try:
        assert mod.INGEST_URL == f"http://127.0.0.1:3100/api/ingest/{slug}"
    finally:
        monkeypatch.delenv("PANDA_BI_URL")
        importlib.reload(mod)
