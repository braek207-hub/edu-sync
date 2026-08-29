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

import datetime as _dt
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


# --------------------------------------------- подключение: витрина источника


def test_load_direct_rows_drops_foreign_campaigns(monkeypatch):
    """Единственный вход агента в direct_stats фильтрует сам.

    Читателей у него двое — расчёт (agent_e0) и гейт пишущих прогонов
    (sync/agent/gate.py), и фильтр обязан быть общим: гейт сверяет сумму
    источника с суммой витрины фактов, а витрину пишет расчёт. Отфильтруй
    одну сторону — сверка разойдётся на расход чужих кампаний и запретит
    запись навсегда.
    """
    from sync.agent import db as agent_db

    rows = [{"date": "2026-08-01", "campaign_id": "1",
             "campaign_name": "vuz / Поиск / РФ", "cost": 100.0},
            {"date": "2026-08-01", "campaign_id": "710118280",
             "campaign_name": "vse / ВПО-МСК-rsv", "cost": 900.0}]
    monkeypatch.setattr(agent_db, "_fetch_dicts", lambda sql, params=(): rows)

    kept = agent_db.load_direct_rows("2026-08-01", "2026-08-07")

    assert [r["campaign_id"] for r in kept] == ["1"]


def test_mart_cost_total_excludes_the_same_campaigns(monkeypatch):
    """Вторая сторона сверки сумм — витрина фактов.

    В edu_agent_facts строки чужих кампаний уже лежат: их писали прогоны до
    введения исключения, и удалять их эта задача не имеет права. Значит
    вычитать их обязан запрос, иначе источник (уже без rsv) не сойдётся с
    витриной (ещё с rsv) и гейт покраснеет на ровном месте.
    """
    from sync.agent import db as agent_db

    seen = {}
    monkeypatch.setattr(agent_db, "_fetch_dicts",
                        lambda sql, params=(): seen.update(sql=sql, params=params)
                        or [{"total": 0.0}])

    agent_db.mart_cost_total("2026-08-01", "2026-08-07")

    assert "campaign_name" in seen["sql"]
    assert "%rsv%" in seen["params"][-1]


def test_load_excluded_campaign_ids_asks_the_source_by_name(monkeypatch):
    """Отчёты Директа по сегментам отдают CampaignId без имени.

    Множество исключённых Id снимается одним запросом к тому же источнику,
    где имя есть, — а не выводится из уже отфильтрованных строк, в которых
    чужих кампаний по построению не осталось.
    """
    from sync.agent import db as agent_db

    seen = {}
    monkeypatch.setattr(
        agent_db, "_fetch_dicts",
        lambda sql, params=(): seen.update(sql=sql, params=params)
        or [{"campaign_id": "710118280"}, {"campaign_id": 712704859}])

    ids = agent_db.load_excluded_campaign_ids("2026-08-01", "2026-08-07")

    assert ids == {"710118280", "712704859"}
    assert "campaign_name" in seen["sql"]


# --------------------------------------------- подключение: кабинеты прогонов


def test_calculation_and_writer_both_drop_the_excluded_account(monkeypatch):
    """Расчёт и движок записи читают один секрет и обязаны сойтись по составу."""
    import sync.agent_e0 as agent_e0
    import sync.agent_e1 as agent_e1

    monkeypatch.setenv("DIRECT_CLIENTS_JSON", json.dumps([
        {"login": "account10-506462-fqs4", "goal_ids": ["1"]},
        {"login": " account4-506456-gsrr ", "goal_ids": ["2"]},
        {"login": "account1-506453-ln8s", "goal_ids": []},
    ]))

    calc = [c["login"] for c in agent_e0._direct_clients()]
    writer = [c["login"] for c in agent_e1._clients()]

    assert calc == writer == ["account10-506462-fqs4", "account1-506453-ln8s"]


def test_single_client_env_of_the_excluded_account_gives_nothing(monkeypatch):
    """Запасной вход расчёта — DIRECT_CLIENT_LOGIN — исключение тоже уважает."""
    import sync.agent_e0 as agent_e0

    monkeypatch.delenv("DIRECT_CLIENTS_JSON", raising=False)
    monkeypatch.setenv("DIRECT_CLIENT_LOGIN", "account4-506456-gsrr")

    assert agent_e0._direct_clients() == []


# --------------------------------------------- подключение: кампании кабинета


class _FakeCampaignsClient:
    """campaigns.get одного кабинета: страницы и запомненные запросы."""

    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def get(self, service, params):
        assert service == "campaigns"
        self.requests.append(params)
        idx = params["Page"]["Offset"] // params["Page"]["Limit"]
        return {"Campaigns": self.pages[idx] if idx < len(self.pages) else []}


def test_writer_campaign_list_asks_for_names_and_drops_foreign(monkeypatch):
    """У движка записи это ЕДИНСТВЕННЫЙ вход списка кампаний.

    Всё, что ниже (own_campaign_ids, чтение корректировок, план, отправка),
    работает с его результатом. Без имени в FieldNames отличить чужую
    кампанию нечем — фильтр молча пропускал бы всё.
    """
    import sync.agent_e1 as agent_e1

    client = _FakeCampaignsClient(pages=[[
        {"Id": 1, "Name": "vuz / Поиск / РФ"},
        {"Id": 710118280, "Name": "vse / ВПО-МСК-rsv"},
        {"Id": 2, "Name": "vse / СПО / РСЯ"},
    ]])

    ids = agent_e1.fetch_campaign_ids(client)

    assert ids == [1, 2]
    assert "Name" in client.requests[0]["FieldNames"]


def test_calculation_campaign_list_asks_for_names_and_drops_foreign(monkeypatch):
    """Тот же вход у расчёта: по нему снимаются объекты и справочник кабинетов."""
    from sync.agent import segments

    captured = {}

    def _fake_post(url, login, payload, what, attempts=None):
        captured["payload"] = payload
        if payload["params"]["Page"]["Offset"]:
            return {"Campaigns": []}
        return {"Campaigns": [
            {"Id": 1, "Name": "vuz / Поиск / РФ"},
            {"Id": 710687014, "Name": "vse /ВПО/ rsv"},
        ]}

    monkeypatch.setattr(segments, "_api_post", _fake_post)

    ids = segments.fetch_campaign_ids("account10-506462-fqs4")

    assert ids == [1]
    assert "Name" in captured["payload"]["params"]["FieldNames"]


# --------------------------------------------- подключение: журнальные стадии


def test_watchdog_ignores_actions_of_the_excluded_account():
    import sync.agent_e1_watchdog as watchdog

    actions = [{"action_id": "a", "account": "account10-506462-fqs4"},
               {"action_id": "b", "account": " account4-506456-gsrr "}]

    assert [a["action_id"] for a in watchdog.own_actions(actions)] == ["a"]


def test_drift_ignores_actions_of_the_excluded_account():
    import sync.agent_drift as agent_drift

    actions = [{"action_id": "a", "account": "account1-506453-ln8s"},
               {"action_id": "b", "account": "ACCOUNT4-506456-GSRR"}]

    assert [a["action_id"] for a in agent_drift.own_actions(actions)] == ["a"]


def test_review_ignores_rejects_of_the_excluded_account():
    import sync.agent_review as agent_review

    rejects = [{"account": "account1-506453-ln8s", "kind": "bid"},
               {"account": "account4-506456-gsrr", "kind": "bid"}]

    assert [r["account"] for r in agent_review.own_rejects(rejects)] == [
        "account1-506453-ln8s"]


# ------------------------- подключение: отчёты Директа со строками кампаний


def _capture_segment_payload(monkeypatch, tsv="Device\n"):
    """Перехват тела запроса к Reports API вместо самого запроса."""
    from sync.agent import segments

    captured = {}
    monkeypatch.setattr(segments, "_run_report",
                        lambda login, payload: captured.update(payload=payload) or tsv)
    return captured


def test_account_aggregate_is_summed_from_campaign_rows():
    """Кабинетный агрегат складывается в Python, а не запрашивается у Директа.

    Условия «кроме этих кампаний» в Reports API нет: NOT_IN не существует
    (ошибка 4001, run 33274646184), а перечисление своих через IN выбросило бы
    из агрегата кампании Мастера — campaigns.get их не отдаёт вовсе. Поэтому
    отсечение чужих идёт по строкам, где CampaignId есть, а кабинетные числа
    получаются сложением.
    """
    from sync.agent.segments import aggregate_account_rows

    rows = [
        {"segment_kind": "device", "segment_key": "MOBILE", "slice_key": "MOBILE",
         "slice_label": "", "clicks": 10, "impressions": 100, "conversions": 1,
         "cost": 100.0, "campaign_id": "111"},
        {"segment_kind": "device", "segment_key": "MOBILE", "slice_key": "MOBILE",
         "slice_label": "", "clicks": 5, "impressions": 50, "conversions": 2,
         "cost": 50.5, "campaign_id": "222"},
        {"segment_kind": "device", "segment_key": "DESKTOP", "slice_key": "DESKTOP",
         "slice_label": "", "clicks": 7, "impressions": 70, "conversions": 3,
         "cost": 70.25, "campaign_id": "111"},
    ]

    assert aggregate_account_rows(rows) == [
        {"segment_kind": "device", "segment_key": "DESKTOP", "slice_key": "DESKTOP",
         "slice_label": "", "clicks": 7, "impressions": 70, "conversions": 3,
         "cost": 70.25},
        {"segment_kind": "device", "segment_key": "MOBILE", "slice_key": "MOBILE",
         "slice_label": "", "clicks": 15, "impressions": 150, "conversions": 3,
         "cost": 150.5},
    ]


def test_every_segment_report_asks_for_campaign_id(monkeypatch):
    """CampaignId просят ОБА такта — иначе отсечь чужие строки нечем.

    Разрез по кампаниям перестал быть признаком среза фактов: агрегат кабинета
    у Директа не запрашивается вовсе (в его ответе CampaignId нет, и чужой
    расход осел бы в знаменателе конверсионности сегмента). Date остаётся
    отличием: срезу фактов она нужна — там недели, — а расчётному такту нет,
    и лишний разрез умножил бы объём ответа на число дней окна.
    """
    from sync.agent import segments

    captured = _capture_segment_payload(monkeypatch)
    segments.fetch_segment_report("acc-1", "device", "2026-08-01", "2026-08-28",
                                  with_date=False)
    assert captured["payload"]["params"]["FieldNames"][0] == "CampaignId"
    assert "Date" not in captured["payload"]["params"]["FieldNames"]
    # Условия в запросе нет ни в каком виде: Мастер кампаний обязан остаться.
    assert "Filter" not in captured["payload"]["params"]["SelectionCriteria"]

    captured = _capture_segment_payload(monkeypatch)
    segments.fetch_segment_report("acc-1", "device", "2026-08-01", "2026-08-28")
    assert captured["payload"]["params"]["FieldNames"][:2] == ["CampaignId", "Date"]


def test_segment_report_drops_foreign_rows_before_choosing_the_goal(monkeypatch):
    """Чужие строки уходят ДО выбора цели, а не после.

    Колонка цели выбирается самой массовой по отчёту (primary_goal_column), и
    паспорт отчёта считается по ней же. Отсечение после выбора означало бы,
    что вся конверсионность кабинета — и, через неё, все корректировки
    ставок — посчитаны по цели, которую назначила чужая кампания.
    """
    from sync.agent import segments

    tsv = (
        "CampaignId\tDevice\tClicks\tCost\tImpressions"
        "\tConversions_11_LSCCD\tConversions_22_LSCCD\n"
        "111\tMOBILE\t100\t1000.0\t900\t30\t5\n"
        "710118280\tMOBILE\t900\t9000.0\t8000\t1\t400\n"
    )
    monkeypatch.setattr(segments, "_run_report", lambda login, payload: tsv)

    rows, goal = segments.fetch_segment_report(
        "acc-1", "device", "2026-08-01", "2026-08-28",
        goals=["11", "22"], excluded_campaign_ids={"710118280"})

    assert [r["campaign_id"] for r in rows] == ["111"]
    # Цель 22 массовее только за счёт чужой кампании: её 400 конверсий
    # перевесили бы 30 своих.
    assert goal["goal_column"] == "Conversions_11_LSCCD"
    assert goal["conversions"] == 30
    assert rows[0]["conversions"] == 30


def test_search_queries_drop_foreign_rows(monkeypatch):
    """Фраза агрегируется по кампаниям кабинета — чужие в сумму не идут.

    Кандидат в минус-слова отбирается по расходу и конверсиям ФРАЗЫ, сложенным
    по всем её кампаниям (objects.py). Строка чужой РК добавляла бы расход в
    порог, а минус-слово писалось бы в свои кампании: чужие деньги решали, что
    отминусовать у себя.
    """
    from sync.agent import segments

    tsv = ("CampaignId\tQuery\tCriteria\tCost\tClicks\tConversions_11_LSCCD\n"
           "111\tколледж москва\tколледж\t500.0\t10\t2\n"
           "710118280\tколледж москва\tколледж\t90000.0\t900\t0\n")
    monkeypatch.setattr(segments, "_run_report", lambda login, payload: tsv)

    rows, goal = segments.fetch_search_queries(
        "acc-1", "2026-08-01", "2026-08-28", goals=["11"],
        excluded_campaign_ids={"710118280"})

    assert [r["campaign_id"] for r in rows] == ["111"]
    assert sum(r["cost"] for r in rows) == 500.0
    assert goal["conversions"] == 2


def test_placements_drop_foreign_rows(monkeypatch):
    """Площадка запрещается в своих кампаниях — считаться должна по ним же."""
    from sync.agent import segments

    tsv = ("CampaignId\tPlacement\tAdNetworkType\tCost\tClicks\tImpressions"
           "\tConversions_11_LSCCD\n"
           "111\tsite.ru\tAD_NETWORK\t500.0\t10\t100\t2\n"
           "710118280\tsite.ru\tAD_NETWORK\t90000.0\t900\t9000\t0\n")
    monkeypatch.setattr(segments, "_run_report", lambda login, payload: tsv)

    rows, goal = segments.fetch_placements(
        "acc-1", "2026-08-01", "2026-08-28", goals=["11"],
        excluded_campaign_ids={"710118280"})

    assert [r["campaign_id"] for r in rows] == ["111"]
    assert goal["conversions"] == 2


def test_calculation_hands_the_excluded_set_to_every_report_reader(monkeypatch, capsys):
    """Все три отчёта кабинета получают множество исключённых.

    Отсечение живёт внутри читателей (там же выбирается цель), но множество
    приходит снаружи — из витрины источника, где имя кампании ещё видно.
    Забытый аргумент у любого из трёх молча возвращает чужие строки: у
    запросов — в кандидаты в минус-слова, у площадок — в запреты, у среза —
    в факты и корректировки.
    """
    import sync.agent_e0 as agent_e0
    import tests.test_agent_e0 as e0_tests

    e0_tests._patch_e0_run(monkeypatch)
    monkeypatch.setattr(agent_e0.agent_db, "load_excluded_campaign_ids",
                        lambda *a, **k: {"710118280"})
    seen = {}
    goal = {"goal_column": None, "conversions": 0, "columns_offered": 0}

    def _segments(login, kind, date_from, date_to, goals=(), with_date=True,
                  excluded_campaign_ids=()):
        seen["segments"] = set(excluded_campaign_ids)
        return [], goal

    def _queries(login, date_from, date_to, goals=(), excluded_campaign_ids=()):
        seen["queries"] = set(excluded_campaign_ids)
        # Колонка цели нужна настоящая: кабинет без неё считается слепым и
        # выпадает из шага площадок целиком — тогда третий читатель просто не
        # был бы спрошен, а тест этого не заметил бы.
        return [], {"goal_column": "Conversions_1_LSCCD", "conversions": 1,
                    "columns_offered": 1}

    def _placements(login, date_from, date_to, goals=(), excluded_campaign_ids=()):
        seen["placements"] = set(excluded_campaign_ids)
        return [], goal

    monkeypatch.setattr(agent_e0, "fetch_segment_report", _segments)
    monkeypatch.setattr(agent_e0, "fetch_search_queries", _queries)
    monkeypatch.setattr(agent_e0, "fetch_placements", _placements)
    # Площадки спрашиваются только при живом пороге CPA — без него шаг
    # пропускается целиком, и проверять было бы нечего.
    monkeypatch.setattr(agent_e0.agent_db, "load_baseline_cpa",
                        lambda *a, **k: {"vuz": 1000.0})

    assert agent_e0.main() == 0
    capsys.readouterr()

    assert seen == {"segments": {"710118280"},
                    "queries": {"710118280"},
                    "placements": {"710118280"}}


def test_run_report_names_the_accounts_it_actually_dropped(monkeypatch):
    """Строка «не смотрели» обязана называть факт, а не константу.

    Список исключений — свойство кода, а состав прогона — свойство секрета.
    Печатая константу, отчёт сообщал бы о работе, которой не было: кабинет,
    которого в DIRECT_CLIENTS_JSON нет вовсе, выглядел бы отброшенным.
    """
    import sync.agent_e0 as agent_e0
    import sync.agent_e1 as agent_e1

    monkeypatch.setenv("DIRECT_CLIENTS_JSON", json.dumps([
        {"login": "account1-506453-ln8s"},
        {"login": " account4-506456-gsrr "},
    ]))
    assert scope.excluded_client_logins(agent_e0._direct_clients_raw()) == [
        "account4-506456-gsrr"]
    assert scope.excluded_client_logins(agent_e1._clients_raw()) == [
        "account4-506456-gsrr"]

    monkeypatch.setenv("DIRECT_CLIENTS_JSON", json.dumps([
        {"login": "account1-506453-ln8s"}]))
    assert scope.excluded_client_logins(agent_e0._direct_clients_raw()) == []
    assert scope.excluded_client_logins(agent_e1._clients_raw()) == []


# ------------------------- подключение: остальные агрегаты витрины фактов


def _capture_facts_sql(monkeypatch, rows):
    from sync.agent import db as agent_db

    seen = {}
    monkeypatch.setattr(agent_db, "_fetch_dicts",
                        lambda sql, params=(): seen.update(sql=sql, params=params)
                        or rows)
    return seen


def test_daily_account_totals_exclude_foreign_campaigns(monkeypatch):
    """Сезонная поправка красных линий сторожа — контроль по ВСЕЙ витрине.

    Чужие кампании в этом контроле двигают порог отката боевых изменений.
    """
    from sync.agent import db as agent_db

    seen = _capture_facts_sql(monkeypatch, [])
    agent_db.load_daily_account_totals("2026-08-01", "2026-08-07")

    assert "campaign_name" in seen["sql"]
    assert "%rsv%" in seen["params"][-1]


def test_mart_day_breadth_excludes_foreign_campaigns(monkeypatch):
    """Ширина дня — знаменатель гейта данных: сколько кампаний считать нормой."""
    from sync.agent import db as agent_db

    seen = _capture_facts_sql(monkeypatch, [])
    agent_db.load_mart_day_breadth("2026-08-01", "2026-08-07")

    assert "campaign_name" in seen["sql"]
    assert "%rsv%" in seen["params"][-1]


def test_cost_by_campaign_excludes_foreign_campaigns(monkeypatch):
    """Знаменатель «слепой доли»: сколько денег вообще прошло мимо агента."""
    from sync.agent import db as agent_db

    seen = _capture_facts_sql(monkeypatch, [])
    agent_db.load_cost_by_campaign("2026-08-01", "2026-08-07")

    assert "campaign_name" in seen["sql"]
    assert "%rsv%" in seen["params"][-1]


# ----------------------------------------- подключение: лиды, тень, транспорт


def test_leads_of_foreign_campaigns_do_not_reach_facts(monkeypatch, capsys):
    """Лид чужой кампании обязан отсекаться ДО сборки фактов.

    В crm_lead_details имени кампании нет — только campaign_id, — поэтому
    условие по имени сюда не достаёт, а последствия у пропущенного лида
    тяжелее, чем у пропущенного расхода. Слот факта создаётся по паре «день ×
    кампания» с campaign_name=None и cost=0 (facts.py), а запись фактов идёт
    через ON CONFLICT: такой слот ПЕРЕЗАПИСЫВАЕТ историческую строку
    edu_agent_facts пустым именем и нулевым расходом. Дальше эту строку не
    ловит ни одно условие по имени (NULL не похож ни на один шаблон), и
    отбор в заповедник по leads_30d может увести чужую кампанию под
    эксперимент агента.
    """
    import sync.agent_e0 as agent_e0
    import tests.test_agent_e0 as e0_tests

    e0_tests._patch_e0_run(monkeypatch)
    today = _dt.date.today().isoformat()
    monkeypatch.setattr(agent_e0.agent_db, "load_excluded_campaign_ids",
                        lambda *a, **k: {"710118280"})
    monkeypatch.setattr(agent_e0.agent_db, "load_lead_rows", lambda *a, **k: [
        {"lead_id": "L1", "campaign_id": "111", "created_date": today},
        {"lead_id": "L2", "campaign_id": 710118280, "created_date": today},
    ])
    seen = {}

    def _capture(direct_rows, lead_rows, score_rows):
        seen["leads"] = list(lead_rows)
        return [e0_tests._fact(today)]

    monkeypatch.setattr(agent_e0, "assemble_facts", _capture)

    assert agent_e0.main() == 0
    capsys.readouterr()

    assert [r["lead_id"] for r in seen["leads"]] == ["L1"]


def test_watchdog_does_not_judge_shadow_of_the_excluded_account():
    """Вердикт тени пишется в журнал — по строке ЛЮБОГО кабинета.

    Открытые действия сторож уже отбирает своими (own_actions), а ждущие
    сверки намерения читались мимо границы: mark_shadow_outcome проставлял бы
    «сбылось/не сбылось» намерению чужой команды, и в её журнале появлялись
    бы вердикты, которых она не просила.
    """
    import sync.agent_e1_watchdog as watchdog

    rows = [{"action_id": "a", "account": "account10-506462-fqs4",
             "object_id": "111", "action_kind": "bid_modifier"},
            {"action_id": "b", "account": "account4-506456-gsrr",
             "object_id": "222", "action_kind": "bid_modifier"}]
    marked = []

    class _Db:
        @staticmethod
        def shadow_actions():
            return list(rows)

        @staticmethod
        def mark_shadow_outcome(action_id, verdict, payload):
            marked.append(action_id)
            return True

    report = watchdog.shadow_report(_Db(), {}, _dt.date.today(), None)

    assert report["waiting"] == 1
    assert "b" not in marked


def test_watchdog_shadow_rows_are_own_only(monkeypatch):
    """Тот же отбор на втором чтении тени — в отчёте прогона.

    Два чтения одного журнала обязаны видеть одно и то же: иначе «ждут
    сверки» в отчёте и «судим» в теле сторожа разойдутся, и объяснить разницу
    будет нечем.
    """
    import sync.agent_e1_watchdog as watchdog

    monkeypatch.setattr(watchdog.writer_db, "shadow_actions", lambda: [
        {"action_id": "a", "account": "account1-506453-ln8s"},
        {"action_id": "b", "account": "account4-506456-gsrr"},
    ])

    assert [r["action_id"] for r in watchdog._shadow_rows()] == ["a"]


def test_write_client_refuses_the_excluded_account():
    """Последний рубеж — у самого транспорта записи.

    Выше по течению исключение держат отборы (own_actions, filter_clients), но
    все они — списки, которые кто-то собирает. Транспорт же знает логин точно
    и в момент, когда запись ещё не ушла: конструктор, отказывающий с
    названной причиной, превращает будущую ошибку отбора из тихой правки
    чужого кабинета в падение с текстом.
    """
    import pytest

    from sync.agent.writer.client import WriteClient

    with pytest.raises(ValueError) as exc:
        WriteClient("account4-506456-gsrr", sandbox=False, dry_run=False)

    assert "account4-506456-gsrr" in str(exc.value)
    # Свой кабинет конструируется как раньше.
    assert WriteClient("account1-506453-ln8s").login == "account1-506453-ln8s"
