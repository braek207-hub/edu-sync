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


# ------------------------- подключение: кабинетный агрегат сегментных отчётов


def _capture_segment_payload(monkeypatch, tsv="Device\n"):
    """Перехват тела запроса к Reports API вместо самого запроса."""
    from sync.agent import segments

    captured = {}
    monkeypatch.setattr(segments, "_run_report",
                        lambda login, payload: captured.update(payload=payload) or tsv)
    return captured


def test_campaign_list_splits_own_from_foreign(monkeypatch):
    """Один проход campaigns.get отдаёт обе стороны: свои Id и чужие.

    Чужие нужны не для отчёта: по ним кабинет узнаётся как «есть что
    исключать», и только такому кабинету достаётся сужающее условие запроса.
    """
    from sync.agent import segments

    def _fake_post(url, login, payload, what, attempts=None):
        if payload["params"]["Page"]["Offset"]:
            return {"Campaigns": []}
        return {"Campaigns": [
            {"Id": 1, "Name": "vuz / Поиск / РФ"},
            {"Id": 710687014, "Name": "vse /ВПО/ rsv"},
            {"Id": 2, "Name": "vse / СПО / РСЯ"},
        ]}

    monkeypatch.setattr(segments, "_api_post", _fake_post)

    own, foreign = segments.fetch_campaign_ids_by_scope("account10-506462-fqs4")

    assert own == [1, 2]
    assert foreign == [710687014]
    # Прежний вход остался вторым именем той же выборки: у него один читатель
    # снаружи и десяток двойников в тестах, и ломать его незачем.
    assert segments.fetch_campaign_ids("account10-506462-fqs4") == [1, 2]


def test_account_aggregate_report_asks_direct_for_own_campaigns_only(monkeypatch):
    """Кабинетный агрегат нельзя отфильтровать по строкам — их нет.

    by_campaign=False возвращает по строке на СЕГМЕНТ, без CampaignId: расход
    чужих РК сидит внутри и уезжает в знаменатель конверсионности сегмента,
    то есть в кабинетные корректировки ставок. Единственное место, где эти
    кампании ещё различимы, — сам запрос.

    Условие перечисляет СВОИ кампании, а не исключённые: замер 30.08.2026
    (probe-report-filter, run 33274646184) получил от Директа ошибку 4001 —
    «для поля CampaignId допустимы только операторы EQUALS, IN» для
    CUSTOM_REPORT. Оператора NOT_IN не существует, и «всё кроме этих семи»
    в этом API выражается только перечислением всего остального.
    """
    from sync.agent import segments

    captured = _capture_segment_payload(monkeypatch)

    segments.fetch_segment_report(
        "account10-506462-fqs4", "device", "2026-08-01", "2026-08-07",
        own_campaign_ids=[22, 11])

    criteria = captured["payload"]["params"]["SelectionCriteria"]
    assert criteria["Filter"] == [{"Field": "CampaignId", "Operator": "IN",
                                  "Values": ["11", "22"]}]


def test_report_without_own_list_is_the_same_request_as_before(monkeypatch):
    """Кабинет без чужих кампаний обязан спрашивать ровно то же, что и раньше.

    Пустой список — не «условие ни на что»: это другое тело запроса, другой
    хеш имени отчёта (_stamp_report_name) и лишний повод Директу его
    отвергнуть. Нечего сужать — нет и ключа.
    """
    from sync.agent import segments

    captured = _capture_segment_payload(monkeypatch)

    segments.fetch_segment_report("acc", "device", "2026-08-01", "2026-08-07")

    assert "Filter" not in captured["payload"]["params"]["SelectionCriteria"]


def test_by_campaign_report_is_filtered_by_rows_not_by_request(monkeypatch):
    """Покампанийный срез режется по строкам — там CampaignId есть.

    Два фильтра на одном отчёте были бы двумя правдами: расходись они, понять
    по числам, какая сработала, стало бы нечем. Здесь это ещё и вопрос
    полноты: перечисление своих кампаний в запросе выбросило бы из среза
    кампании Мастера, которых campaigns.get не отдаёт вовсе.
    """
    from sync.agent import segments

    captured = _capture_segment_payload(monkeypatch)

    segments.fetch_segment_report(
        "acc", "device", "2026-08-01", "2026-08-07", by_campaign=True,
        own_campaign_ids=["11", "22"])

    assert "Filter" not in captured["payload"]["params"]["SelectionCriteria"]


def test_calculation_narrows_only_accounts_that_own_foreign_campaigns(monkeypatch):
    """Сужается запрос ТОЛЬКО у кабинета, которому есть что исключать.

    Условие «IN свои кампании» — не бесплатное: оно заодно выбрасывает из
    агрегата всё, чего не отдаёт campaigns.get, то есть кампании Мастера.
    Платить эту цену там, где чужих РК нет, значило бы сузить агрегат ни за
    чем — тело запроса такого кабинета обязано остаться прежним.
    """
    import sync.agent_e0 as agent_e0
    import tests.test_agent_e0 as e0_tests

    e0_tests._patch_e0_run(monkeypatch)
    by_login = {"acc-1": ([11, 12], [710118280]), "acc-2": ([21], [])}
    monkeypatch.setattr(agent_e0, "fetch_campaign_ids_by_scope",
                        lambda login: by_login[login])
    seen = []
    monkeypatch.setattr(
        agent_e0, "fetch_segment_report",
        lambda login, kind, date_from, date_to, by_campaign=False, goals=(),
        own_campaign_ids=(): seen.append(
            {"login": login, "by_campaign": by_campaign,
             # Приведение к строкам и сортировка — работа
             # fetch_segment_report (проверена тестом формы выше); здесь
             # важно только, ЧЕЙ список доехал.
             "own": sorted(str(c) for c in own_campaign_ids)})
        or ([], {"goal_column": None, "conversions": 0, "columns_offered": 0}),
    )

    assert agent_e0.main() == 0

    account_jobs = [j for j in seen if not j["by_campaign"]]
    assert account_jobs, "кабинетные срезы не запрашивались — тест ничего не проверил"
    narrowed = {j["login"]: j["own"] for j in account_jobs}
    assert narrowed["acc-1"] == ["11", "12"]
    assert narrowed["acc-2"] == []
    # Покампанийный срез не сужается ни у кого: он режется по строкам.
    assert all(j["own"] == [] for j in seen if j["by_campaign"])


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
