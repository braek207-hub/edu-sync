# -*- coding: utf-8 -*-
"""
tests/test_agent_scope.py — что агент не видит вообще (sync/agent/scope.py).

Кабинет «Бренды» ведёт не владелец, кампании с «rsv» в имени — чужие РК.
Исключение здесь не «фильтр отчёта», а граница зоны ответственности: строка,
прошедшая мимо него, уезжает в факты, в корректировки и в кабинет.

Проверяется и сам предикат, и его подключение к каждому входу кабинетов и
кампаний: модуль, который никто не вызывает, зелёный ровно так же, как
подключённый.
"""

import json

import pytest

from sync.agent import scope


# ------------------------------------------------------- предикат по кабинету


def test_excluded_account_recognised_as_is():
    assert scope.is_excluded_account("account4-506456-gsrr") is True


def test_excluded_account_recognised_through_normalization():
    """Логин нормализуется той же функцией, что и ключ object_id таблиц агента.

    Пробел по краям или другой регистр в DIRECT_CLIENTS_JSON — не повод
    пропустить кабинет: сравнение по сырой строке промахнулось бы молча.
    """
    assert scope.is_excluded_account("  account4-506456-gsrr\t") is True
    assert scope.is_excluded_account("ACCOUNT4-506456-GSRR") is True


def test_own_account_is_not_excluded():
    assert scope.is_excluded_account("account10-506462-fqs4") is False
    assert scope.is_excluded_account("") is False
    assert scope.is_excluded_account(None) is False


# ------------------------------------------------------- предикат по кампании


@pytest.mark.parametrize("name", [
    "vse / СПО / ВПО-МСК-rsv",
    "provuz /ВПО/ rsv",
    "vse / СПО / ВПО-МСК-rsv-2",
    "RSV Поиск РФ",
])
def test_foreign_campaign_recognised_by_substring(name):
    assert scope.is_excluded_campaign(name) is True


@pytest.mark.parametrize("name", [
    "vuz / Ресурсы / Поиск / РФ",
    "vse / Резерв / РСЯ",
    "vuz / Онлайн-школа / Школа / Поиск / РФ",
    "",
    None,
])
def test_own_campaign_is_not_excluded(name):
    assert scope.is_excluded_campaign(name) is False


# ------------------------------------------------------------------- обёртки


def test_filter_clients_drops_the_excluded_account():
    clients = [{"login": "account10-506462-fqs4", "goal_ids": ["1"]},
               {"login": " account4-506456-gsrr ", "goal_ids": ["2"]},
               {"login": "account1-506453-ln8s", "goal_ids": []}]

    kept = scope.filter_clients(clients)

    assert [c["login"] for c in kept] == ["account10-506462-fqs4",
                                          "account1-506453-ln8s"]


def test_filter_campaign_rows_drops_foreign_campaigns_by_name():
    rows = [{"campaign_id": "1", "campaign_name": "vuz / Поиск / РФ"},
            {"campaign_id": "710118280", "campaign_name": "vse / ВПО-МСК-rsv"},
            {"campaign_id": "3", "campaign_name": None}]

    kept = scope.filter_campaign_rows(rows)

    assert [r["campaign_id"] for r in kept] == ["1", "3"]


def test_filter_campaign_rows_reads_the_key_it_was_given():
    rows = [{"Id": 1, "Name": "vuz / Поиск"}, {"Id": 2, "Name": "rsv"}]

    kept = scope.filter_campaign_rows(rows, name_key="Name")

    assert [r["Id"] for r in kept] == [1]


def test_excluded_campaign_ids_collects_ids_of_dropped_rows():
    """Ниже по течению у отчётов Директа есть только Id — имени в них нет.

    Поэтому множество исключённых идентификаторов снимается там, где имена
    ещё видны, и дальше едет вместо предиката.
    """
    rows = [{"campaign_id": "1", "campaign_name": "vuz / Поиск"},
            {"campaign_id": 710118280, "campaign_name": "vse / ВПО-МСК-rsv"},
            {"campaign_id": "712704859", "campaign_name": "RSV"}]

    assert scope.excluded_campaign_ids(rows) == {"710118280", "712704859"}


def test_like_patterns_match_the_substring_rule():
    """SQL-сторона исключения выведена из тех же констант, а не набрана руками."""
    assert scope.like_patterns() == ["%rsv%"]
