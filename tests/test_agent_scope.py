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


def test_account_report_asks_for_campaign_id_without_date(monkeypatch):
    """Кабинетному агрегату нужен CampaignId и не нужна Date.

    CampaignId — чтобы было чем отсечь чужие строки. Date — не нужна: числа
    всё равно складываются по всему окну, а лишний разрез умножает объём
    ответа на число дней окна (30 у расчётного такта).
    """
    from sync.agent import segments

    captured = _capture_segment_payload(monkeypatch)
    segments.fetch_segment_report("acc-1", "device", "2026-08-01", "2026-08-28",
                                  by_campaign=True, with_date=False)

    fields = captured["payload"]["params"]["FieldNames"]
    assert fields[0] == "CampaignId"
    assert "Date" not in fields
    # Условия в запросе нет ни в каком виде: Мастер кампаний обязан остаться.
    assert "Filter" not in captured["payload"]["params"]["SelectionCriteria"]


def test_sliced_report_still_asks_for_date(monkeypatch):
    """Покампанийный срез фактов без Date собрать нельзя — там недели."""
    from sync.agent import segments

    captured = _capture_segment_payload(monkeypatch)
    segments.fetch_segment_report("acc-1", "device", "2026-08-01", "2026-08-28",
                                  by_campaign=True)

    assert captured["payload"]["params"]["FieldNames"][:2] == ["CampaignId", "Date"]


def test_calculation_never_asks_direct_for_an_account_aggregate(monkeypatch, capsys):
    """Ни один запрос такта расчёта не идёт агрегатом кабинета.

    Пока запрос был агрегатом (by_campaign=False), в ответе не было
    CampaignId — и отсечь в нём чужие деньги было нечем вообще: они оседали в
    знаменателе конверсионности сегмента, то есть в кабинетных корректировках
    ставок.
    """
    import sync.agent_e0 as agent_e0
    import tests.test_agent_e0 as e0_tests

    e0_tests._patch_e0_run(monkeypatch)
    seen = []

    def _report(login, kind, date_from, date_to, by_campaign=False, goals=(),
                with_date=True):
        seen.append({"login": login, "kind": kind, "by_campaign": by_campaign,
                     "with_date": with_date})
        return [], {"goal_column": None, "conversions": 0, "columns_offered": 0}

    monkeypatch.setattr(agent_e0, "fetch_segment_report", _report)

    assert agent_e0.main() == 0
    capsys.readouterr()

    assert seen, "отчёты вообще не запрашивались — тест ничего не проверил"
    assert [j for j in seen if not j["by_campaign"]] == []


def test_account_modifiers_are_computed_without_foreign_campaigns(monkeypatch, capsys):
    """Кабинетные корректировки считаются по своим кампаниям и только по ним.

    Чужая кампания здесь не просто добавляет расход: она добавляет клики в
    знаменатель конверсионности сегмента. Проверяется по support_n — это и
    есть клики, на которых посчитана корректировка.
    """
    import sync.agent_e0 as agent_e0
    import tests.test_agent_e0 as e0_tests

    calls = e0_tests._patch_e0_run(monkeypatch)
    monkeypatch.setattr(agent_e0.agent_db, "load_excluded_campaign_ids",
                        lambda *a, **k: {"710118280"})

    def _row(segment_key, campaign_id, clicks, conversions):
        return {"segment_kind": "device", "segment_key": segment_key,
                "slice_key": segment_key, "slice_label": "", "clicks": clicks,
                "impressions": clicks * 10, "conversions": conversions,
                "cost": float(clicks), "campaign_id": campaign_id}

    def _report(login, kind, date_from, date_to, by_campaign=False, goals=(),
                with_date=True):
        rows = ([] if (with_date or kind != "device") else [
            _row("MOBILE", "111", 20000, 1200),
            _row("DESKTOP", "111", 20000, 200),
            # Чужая кампания: в её сегментах кликов больше, чем во всём
            # кабинете, — доехав до расчёта, она перевернула бы обе
            # корректировки.
            _row("MOBILE", "710118280", 90000, 100),
            _row("DESKTOP", "710118280", 90000, 9000),
        ])
        return rows, {"goal_column": "Conversions_1_LSCCD", "conversions": 1,
                      "columns_offered": 1}

    monkeypatch.setattr(agent_e0, "fetch_segment_report", _report)

    assert agent_e0.main() == 0
    capsys.readouterr()

    support = {r["setting_key"]: r["support_n"]
               for call in calls for r in call["rows"]
               if call["object_id"] == "acc-1"
               and r["setting_kind"] == "bid_modifier:device"}
    assert support == {"MOBILE": 20000, "DESKTOP": 20000}


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
