# -*- coding: utf-8 -*-
from sync.modules.accounts import DirectAccount, accounts_for


def _no_agency(slug):
    return []


def test_edu_agency_logins():
    acc, why = accounts_for("edunetwork", {"DIRECT_TOKEN": "t"}, lambda s: ["a", "b"])
    assert [(a.login, a.token) for a in acc] == [("a", "t"), ("b", "t")]


def test_edu_fallback_to_clients_json():
    env = {"DIRECT_TOKEN": "t", "DIRECT_CLIENTS_JSON": '["x", {"login": "y"}]'}
    acc, _ = accounts_for("edunetwork", env, _no_agency)
    assert [a.login for a in acc] == ["x", "y"]


def test_edu_agency_failure_reason_and_fallback():
    def boom(slug):
        raise RuntimeError("204")
    env = {"DIRECT_TOKEN": "t", "DIRECT_CLIENTS_JSON": '["x"]'}
    acc, why = accounts_for("edunetwork", env, boom)
    assert [a.login for a in acc] == ["x"] and why and "204" in why[0]


def test_edu_no_token():
    acc, why = accounts_for("edunetwork", {}, _no_agency)
    assert acc == [] and "DIRECT_TOKEN" in why[0]


def test_lime_two_cabinets():
    env = {"LIME_DIRECT_TOKEN": "a", "LIME_DIRECT_CLIENT_LOGIN": "l1",
           "LIME_DIRECT_KZ_TOKEN": "b", "LIME_DIRECT_KZ_CLIENT_LOGIN": "l2"}
    acc, _ = accounts_for("lime", env)
    assert [(a.login, a.currency) for a in acc] == [("l1", "RUB"), ("l2", "KZT")]


def test_lime_missing_kz_reports_name():
    acc, why = accounts_for("lime", {"LIME_DIRECT_TOKEN": "a",
                                     "LIME_DIRECT_CLIENT_LOGIN": "l1"})
    assert len(acc) == 1 and any("LIME_DIRECT_KZ_TOKEN" in w for w in why)


def test_bjorn_token_from_element_and_fallback():
    env = {"BJORN_DIRECT_CLIENTS_JSON": '[{"login": "a", "token": "t1"}, "b"]',
           "BJORN_DIRECT_TOKEN": "fb"}
    acc, _ = accounts_for("bjorn", env)
    assert [(a.login, a.token) for a in acc] == [("a", "t1"), ("b", "fb")]


def test_polina_token_fallback_and_login():
    acc, _ = accounts_for("polinarepik", {"POLINAREPIK_YANDEX_TOKEN": "y"})
    assert acc == [DirectAccount("polinarepik-wear", "y", "RUB", "Polinarepik")]
    acc, _ = accounts_for("polinarepik", {"POLINAREPIK_DIRECT_TOKEN": "d",
                                          "POLINAREPIK_YANDEX_TOKEN": "y",
                                          "POLINAREPIK_DIRECT_CLIENT_LOGIN": "z"})
    assert (acc[0].login, acc[0].token) == ("z", "d")


def test_mnf():
    acc, _ = accounts_for("meshnflesh", {"MESHNFLESH_DIRECT_TOKEN": "m"})
    assert (acc[0].login, acc[0].token) == ("meshnflesh", "m")


def test_missing_token_names_variable():
    for slug, name in [("bjorn", "BJORN_DIRECT_CLIENTS_JSON"),
                       ("meshnflesh", "MESHNFLESH_DIRECT_TOKEN"),
                       ("polinarepik", "POLINAREPIK_DIRECT_TOKEN")]:
        acc, why = accounts_for(slug, {})
        assert acc == [] and any(name in w for w in why), slug


def test_unknown_slug():
    acc, why = accounts_for("nope", {})
    assert acc == [] and "нет кабинетов Директа" in why[0]
