# -*- coding: utf-8 -*-
"""
tests/test_agent_e1.py — тесты отклонений оркестратора Э1a от исходного плана
задачи (см. task-9-report.md): кампании только своего кабинета, нормализация
демографических корректировок с двумя измерениями сразу, абсолютный порог
красной линии из медианы базового CPA (правка по код-ревью — старый вызов
red_line_for(action, baseline) без absolute_max_cpa молча получал дефолт
3000 ₽, никак не связанный с экономикой кабинета).

Все тесты — чистые функции/фейки по протоколу (.get()/.mutate(),
find_action_by_key/insert_action/mark_action) — без сети и без БД.
"""

import json
import sys
from contextlib import contextmanager
from datetime import date, timedelta

import pytest

import sync.agent_e1 as agent_e1
from sync.agent import rejects as rejects_mod
from sync.agent.writer.apply import SandboxApplyRefusal, apply_actions
from sync.agent.writer import risk


class _FakeLease:
    """Аренда прогона без БД: считает перепроверки и умеет «потеряться».

    lost=True — аренду перехватил другой прогон: guard обязан уронить
    прогон, а не дать ему дописать изменения в кабинет, куда уже пишет
    второй процесс.
    """

    def __init__(self, lost=False):
        self.holder = "test-holder"
        self.lost = lost
        self.guards = 0

    def renew(self):
        return not self.lost

    def guard(self):
        self.guards += 1
        if self.lost:
            raise agent_e1.writer_db.RunLeaseLost("аренда потеряна")


def _no_lock(lease=None):
    """Аренда на прогон в тестах не берётся — она требует БД."""
    @contextmanager
    def _cm(*a, **k):
        yield lease if lease is not None else _FakeLease()
    return _cm


def _patch_infra(monkeypatch, cooled=None, final_keys=(), lease=None, exhausted=None,
                 campaign_computed=None, learning_resets=None,
                 campaign_settings=None):
    """Общая подмена того, что прогон спрашивает у журнала помимо действий:
    аренда на прогон, история вредных сегментов, исчерпавшие попытки
    сегменты, уже закрытые ключи. campaign_computed — покампанийные строки
    Э2.2 ({campaign_id: rows}); по умолчанию их нет, кабинетный уровень."""
    monkeypatch.setattr(
        agent_e1.agent_db, "load_latest_campaign_computed",
        lambda ids: {str(k): list(v) for k, v in (campaign_computed or {}).items()
                     if str(k) in {str(i) for i in ids}})
    monkeypatch.setattr(agent_e1.writer_db, "run_lock", _no_lock(lease))
    monkeypatch.setattr(agent_e1.writer_db, "harmful_segments",
                        lambda *a, **k: dict(cooled or {}))
    monkeypatch.setattr(agent_e1.writer_db, "exhausted_segments",
                        lambda *a, **k: dict(exhausted or {}))
    monkeypatch.setattr(agent_e1.writer_db, "final_status_keys",
                        lambda keys: set(final_keys))
    monkeypatch.setattr(agent_e1.writer_db, "purge_dry_run_actions", lambda *a, **k: 0)
    # Витрина настроек — знаменатель слепой доли в отчёте прогона. Пустая по
    # умолчанию: тесты про действия к ней безразличны, а тест про саму долю
    # подменяет её сам. Числитель — сумма расхода за окно, тем же правилом:
    # без подмены прогон ушёл бы в реальную базу и напечатал ретраи коннекта
    # в тот же stdout, который тесты разбирают как JSON.
    monkeypatch.setattr(agent_e1.agent_db, "load_campaign_settings_raw",
                        lambda: dict(campaign_settings or {}))
    monkeypatch.setattr(agent_e1.agent_db, "load_cost_by_campaign", lambda *_: {})
    # Панель настроек: прогон читает её первым делом, до гейта данных. Без
    # подмены каждый тест Э1 идёт в живую базу — прогон набора вырастал с
    # десяти секунд до шести минут и падал там, где базы нет. Пустая панель =
    # кодовые дефолты, то есть поведение, на которое написаны все проверки.
    monkeypatch.setattr(agent_e1.agent_db, "load_agent_config",
                        lambda: {"preset": None, "overrides": {}})
    monkeypatch.setattr(agent_e1, "data_gate",
                        lambda today: {"status": "GREEN", "reason": "",
                                       "checks": []})
    monkeypatch.setattr(agent_e1.writer_db, "recent_action_objects",
                        lambda *a, **k: set())
    # История перезапусков обучения по кампаниям (кулдаун обучения). По
    # умолчанию пустая: кабинет, где стратегии никто не сбивал.
    monkeypatch.setattr(agent_e1.writer_db, "last_learning_reset",
                        lambda *a, **k: dict(learning_resets or {}))
    monkeypatch.setattr(agent_e1.writer_db, "mark_sent", lambda action_id: None)
    # База цены оплаты — инфраструктура второго чекпоинта: в красную линию
    # она едет пассажиром и на решения прогона не влияет (по оплатам не
    # откатывают). Тест, которому она важна, подменяет её сам.
    monkeypatch.setattr(agent_e1.agent_db, "load_baseline_cpo", lambda *_: {})
    # Чёрный ящик в тестах не пишет: он ходит в живую базу, а прогон обязан
    # считаться и без неё. Его собственное поведение проверяют свои тесты.
    monkeypatch.setattr(agent_e1.blackbox, "save_run",
                        lambda *a, **k: {"run_id": "test", "saved": False,
                                         "rejects": 0, "error": "тест"})
    # Тем же пассажиром едет ОБЪЁМ базы: он входит в красную линию ради
    # сверки прогноза с исходом, а решения прогона не трогает.
    monkeypatch.setattr(agent_e1.agent_db, "load_baseline_volume", lambda *_: {})


class _FakeCampaignsClient:
    """Отдаёт кампании страницами, как реальный campaigns.get."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def get(self, service, params):
        assert service == "campaigns"
        self.calls += 1
        limit = params["Page"]["Limit"]
        offset = params["Page"]["Offset"]
        idx = offset // limit
        items = self.pages[idx] if idx < len(self.pages) else []
        return {"Campaigns": [{"Id": i} for i in items]}


def test_fetch_campaign_ids_paginates_until_short_page(monkeypatch):
    monkeypatch.setattr(agent_e1, "CAMPAIGN_PAGE_LIMIT", 2)
    client = _FakeCampaignsClient(pages=[[1, 2], [3]])

    ids = agent_e1.fetch_campaign_ids(client)

    assert ids == [1, 2, 3]
    assert client.calls == 2  # вторая страница короче лимита — цикл остановился


def test_fetch_campaign_ids_stops_immediately_when_first_page_short():
    client = _FakeCampaignsClient(pages=[[111, 222]])  # 2 < CAMPAIGN_PAGE_LIMIT (1000)

    ids = agent_e1.fetch_campaign_ids(client)

    assert ids == [111, 222]
    assert client.calls == 1


def test_own_campaign_ids_excludes_foreign_campaigns():
    # Справочник расходов копит кампании ВСЕХ кабинетов; "999" принадлежит
    # чужому клиенту и не должна попасть в список этого кабинета.
    client = _FakeCampaignsClient(pages=[[111, 222]])
    daily_cost = {"111": 500.0, "999": 10.0}

    result = agent_e1.own_campaign_ids(client, daily_cost)

    assert result == ["111"]


def test_own_campaign_ids_drops_campaigns_without_cost_data():
    # У кабинета есть кампания 333, но по ней нет расхода в справочнике —
    # без пересечения она не должна попасть в опрос bidmodifiers.get.
    client = _FakeCampaignsClient(pages=[[111, 333]])
    daily_cost = {"111": 500.0}

    result = agent_e1.own_campaign_ids(client, daily_cost)

    assert result == ["111"]


def test_normalize_actual_collapses_combined_gender_and_age_to_one_record():
    # Дефект 2. Запись DemographicsAdjustment может нести Gender И Age
    # одновременно — это ОДИН объект Директа с одним Id. Раскладка её на две
    # normalized-записи с ОДНИМ И ТЕМ ЖЕ Id выпускала из diff два изменения на
    # один физический объект: второе затирало первое, оба списывали риск и оба
    # сохраняли прошлое состояние, снятое до первого.
    # BidModifier здесь — то, что реально отдаёт API: 100-базный коэффициент
    # (120 = «+20 %»). Нормализация переводит его в дельту плана.
    item = {"Id": 55, "DemographicsAdjustment": {
        "BidModifier": 120, "Gender": "GENDER_MALE", "Age": "AGE_25_34"}}

    out = agent_e1._normalize_actual(item)

    assert len(out) == 1
    assert out[0]["Id"] == 55
    assert out[0]["percent"] == 20
    assert out[0]["key"] == "GENDER_MALE+AGE_25_34"
    assert out[0]["composite"] is True


def test_normalize_actual_never_returns_two_records_with_same_id():
    # Инвариант нормализации: сколько бы измерений ни несла запись API, на один
    # Id приходится максимум одна actual-запись — иначе diff может выпустить
    # два изменения на один объект.
    items = [
        {"Id": 1, "DemographicsAdjustment": {
            "BidModifier": 120, "Gender": "GENDER_MALE", "Age": "AGE_25_34"}},
        {"Id": 2, "MobileAdjustment": {"BidModifier": 110},
         "DesktopAdjustment": {"BidModifier": 110}},
        {"Id": 3, "RegionalAdjustment": {"BidModifier": 90, "RegionId": 213}},
    ]

    out = [r for item in items for r in agent_e1._normalize_actual(item)]

    ids = [r["Id"] for r in out]
    assert len(ids) == len(set(ids)), out


def test_combined_demographic_does_not_match_one_dimensional_plan():
    # Семантика: коэффициент, посчитанный для всего мужского сегмента, к
    # пересечению «мужчины 25–34» не относится. Составной ключ не сходится с
    # одномерным планом, поэтому diff не выпустит set на чужой объект.
    from sync.agent.writer.diff import diff_modifiers

    desired = [{"kind": "bid_modifier:gender", "direct_type": "DEMOGRAPHICS_ADJUSTMENT",
                "key": "GENDER_MALE", "percent": 30}]
    actual = agent_e1._normalize_actual({"Id": 55, "DemographicsAdjustment": {
        "BidModifier": 120, "Gender": "GENDER_MALE", "Age": "AGE_25_34"}})

    actions = diff_modifiers(desired, actual, campaign_id="111")

    assert len(actions) == 1
    # Добавление отдельного одномерного объекта, а не правка многомерного.
    assert actions[0]["action_kind"] == "bidmodifier.add"
    assert "Id" not in actions[0]["payload"]


def test_normalize_actual_keeps_single_demographic_field_as_one_record():
    item = {"Id": 77, "DemographicsAdjustment": {"BidModifier": 110, "Gender": "GENDER_FEMALE"}}

    out = agent_e1._normalize_actual(item)

    assert out == [{"Id": 77, "Type": "DEMOGRAPHICS_ADJUSTMENT",
                    "key": "GENDER_FEMALE", "percent": 10}]


def test_normalize_actual_mobile_and_regional_unaffected():
    mobile = agent_e1._normalize_actual({"Id": 9, "MobileAdjustment": {"BidModifier": 115}})
    regional = agent_e1._normalize_actual(
        {"Id": 3, "RegionalAdjustment": {"BidModifier": 90, "RegionId": 213}})

    assert mobile == [{"Id": 9, "Type": "MOBILE_ADJUSTMENT", "key": "MOBILE", "percent": 15}]
    assert regional == [{"Id": 3, "Type": "REGIONAL_ADJUSTMENT", "key": "213", "percent": -10}]


def test_normalize_actual_no_dimensions_returns_empty():
    assert agent_e1._normalize_actual({"Id": 1}) == []


# ------------------------------------------ красная линия: медиана базового CPA


def test_absolute_max_cpa_from_baseline_uses_median_times_multiplier():
    baseline_cpa = {"111": 1000.0, "222": 3000.0, "333": 2000.0}  # медиана 2000.0

    result = agent_e1.absolute_max_cpa_from_baseline(baseline_cpa)

    assert result == 2000.0 * agent_e1.ABSOLUTE_MAX_CPA_MULTIPLIER


def test_absolute_max_cpa_from_baseline_none_when_dict_empty():
    # Справочник базовых CPA пуст целиком — медианы не существует.
    assert agent_e1.absolute_max_cpa_from_baseline({}) is None


def test_build_red_line_uses_own_baseline_when_present():
    # Кампания с собственным base_cpa > 0 — относительный порог, absolute_max_cpa
    # не участвует (можно передать None).
    action = {"object_id": "111"}
    baseline_cpa = {"111": 1000.0}

    red_line = agent_e1.build_red_line(action, baseline_cpa, absolute_max_cpa=None)

    assert red_line["has_baseline"] is True
    assert red_line["baseline_cpa"] == 1000.0


def test_build_red_line_passes_absolute_threshold_when_no_own_baseline():
    # Порог считается от медианы и передаётся в красную линию: max_value
    # красной линии обязан совпасть с absolute_max_cpa_from_baseline, а не с
    # захардкоженным DEFAULT_ABSOLUTE_MAX_CPA=3000 из rollback.py.
    action = {"object_id": "999"}  # нет в справочнике
    baseline_cpa = {"111": 1000.0, "222": 3000.0}  # медиана 2000.0

    absolute_max_cpa = agent_e1.absolute_max_cpa_from_baseline(baseline_cpa)
    red_line = agent_e1.build_red_line(action, baseline_cpa, absolute_max_cpa)

    assert absolute_max_cpa == 4000.0  # 2000.0 * ABSOLUTE_MAX_CPA_MULTIPLIER (2.0)
    assert red_line["has_baseline"] is False
    assert red_line["max_value"] == absolute_max_cpa


def test_build_red_line_returns_none_when_no_baseline_and_no_absolute():
    # Ни собственного base_cpa, ни медианы (справочник пуст целиком) —
    # красную линию посчитать не из чего, действие не применяется.
    action = {"object_id": "111"}

    red_line = agent_e1.build_red_line(action, baseline_cpa={}, absolute_max_cpa=None)

    assert red_line is None


class _FakeWriteClient:
    """Кампания 111 без корректировок — desired_bid_modifiers всегда предложит
    добавление, которое дальше упирается в отсутствие красной линии."""

    def is_write_allowed(self):
        return True

    def __init__(self, login, sandbox=True, dry_run=True):
        self.login = login
        self.sandbox = sandbox
        self.dry_run = dry_run
        self.units_left = None

    def get(self, service, params):
        if service == "campaigns":
            return {"Campaigns": [{"Id": 111}]}
        if service == "bidmodifiers":
            return {"BidModifiers": []}
        raise AssertionError(f"неожиданный сервис: {service}")

    def mutate(self, service, method, params):
        raise AssertionError(
            "mutate не должен вызываться: действие без красной линии обязано "
            "быть исключено ДО apply_actions"
        )

    def mutate_batch(self, service, method, collection, items):
        # Батч — ОДИН запрос (client.WriteClient.mutate_batch). Двойник
        # обязан повторять форму настоящего транспорта: иначе прогон в тесте
        # ходит одним путём, а в бою другим.
        return self.mutate(service, method, {collection: list(items)})


def test_main_excludes_action_and_reports_reason_when_baseline_cpa_empty(monkeypatch, capsys):
    # Сквозной тест правки по код-ревью: если справочник базовых CPA пуст
    # целиком, ни у одного действия нет работающей красной линии — main()
    # обязан не применять их (mutate не вызывается) и показать причину в
    # отчёте прогона (no_red_line), а не тихо использовать дефолт-плейсхолдер.
    monkeypatch.setattr(agent_e1, "_clients", lambda: [{"login": "acc-1"}])
    monkeypatch.setattr(agent_e1.writer_db, "ensure_writer_tables", lambda: None)
    monkeypatch.setattr(
        agent_e1.agent_db, "load_latest_computed_settings",
        lambda *_: [_setting("bid_modifier:device", "mobile", 30.0)],
    )
    monkeypatch.setattr(agent_e1.agent_db, "load_holdout_ids", lambda: [])
    monkeypatch.setattr(agent_e1.agent_db, "load_daily_cost_by_campaign",
                         lambda *_: {"111": 500.0})
    monkeypatch.setattr(agent_e1.agent_db, "load_baseline_cpa", lambda *_: {})
    monkeypatch.setattr(agent_e1.agent_db, "crm_maturity_date",
                        lambda: date.today() - timedelta(days=3))
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit", lambda *_: 50_000.0)
    monkeypatch.setattr(agent_e1.writer_db, "spent_risk", lambda *_: 0.0)
    monkeypatch.setattr(agent_e1.writer_db, "charged_risk_by_object", lambda *_: {})
    monkeypatch.setattr(agent_e1.writer_db, "mark_stale_planned", lambda *a, **k: [])
    monkeypatch.setattr(agent_e1, "WriteClient", _FakeWriteClient)
    _patch_infra(monkeypatch)

    exit_code = agent_e1.main()

    assert exit_code == 0
    report = _reports(capsys)[0]
    assert report["no_red_line"] == {"count": 1, "reason": agent_e1.NO_RED_LINE_REASON}
    assert report["absolute_max_cpa"] is None
    assert report["result"]["applied"] == 0
    assert report["result"]["rejected"] == 0
    assert report["result"]["failed"] == 0


# --------------------------------- неприменимые настройки видны в отчёте


class _RecordingWriteClient:
    """Кампания 111 без корректировок; mutate только записывает вызовы."""

    def is_write_allowed(self):
        return True

    instances = []

    def __init__(self, login, sandbox=True, dry_run=True):
        self.login = login
        self.units_left = None
        self.sent = []
        _RecordingWriteClient.instances.append(self)

    def get(self, service, params):
        if service == "campaigns":
            return {"Campaigns": [{"Id": 111}]}
        if service == "bidmodifiers":
            return {"BidModifiers": []}
        raise AssertionError(f"неожиданный сервис: {service}")

    def mutate(self, service, method, params):
        self.sent.append((service, method, params))
        return {"dry_run": True}

    def mutate_batch(self, service, method, collection, items):
        # Батч — ОДИН запрос (client.WriteClient.mutate_batch). Двойник
        # обязан повторять форму настоящего транспорта: иначе прогон в тесте
        # ходит одним путём, а в бою другим.
        return self.mutate(service, method, {collection: list(items)})


def test_main_reports_unsupported_settings_instead_of_failing_on_them(monkeypatch, capsys):
    # Регион приходит НАЗВАНИЕМ (срез отдаёт TargetingLocationName). Раньше
    # такое действие доходило до to_api_call, падало на int("Москва") в
    # статус 'failed' и переприменялось каждый прогон, съедая слоты лимита.
    # Теперь оно исключается из плана с явной причиной, видимой в отчёте, а
    # устройство доезжает до API в 100-базной шкале и своим типом.
    _RecordingWriteClient.instances = []
    monkeypatch.setattr(agent_e1, "_clients", lambda: [{"login": "acc-1"}])
    monkeypatch.setattr(agent_e1.writer_db, "ensure_writer_tables", lambda: None)
    monkeypatch.setattr(
        agent_e1.agent_db, "load_latest_computed_settings",
        lambda *_: [
            _setting("bid_modifier:region", "Москва", 30.0),
            _setting("bid_modifier:device", "DESKTOP", 30.0),
        ],
    )
    monkeypatch.setattr(agent_e1.agent_db, "load_holdout_ids", lambda: [])
    monkeypatch.setattr(agent_e1.agent_db, "load_daily_cost_by_campaign",
                         lambda *_: {"111": 500.0})
    monkeypatch.setattr(agent_e1.agent_db, "load_baseline_cpa", lambda *_: {"111": 1000.0})
    monkeypatch.setattr(agent_e1.agent_db, "crm_maturity_date",
                        lambda: date.today() - timedelta(days=3))
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit", lambda *_: 50_000.0)
    monkeypatch.setattr(agent_e1.writer_db, "spent_risk", lambda *_: 0.0)
    monkeypatch.setattr(agent_e1.writer_db, "charged_risk_by_object", lambda *_: {})
    monkeypatch.setattr(agent_e1.writer_db, "find_action_by_key", lambda *_: None)
    monkeypatch.setattr(agent_e1.writer_db, "insert_action", lambda action: 1)
    monkeypatch.setattr(agent_e1.writer_db, "mark_action", lambda *a, **k: True)
    monkeypatch.setattr(agent_e1.writer_db, "mark_unknown_outcome", lambda *a, **k: True)
    monkeypatch.setattr(agent_e1.writer_db, "mark_stale_planned", lambda *a, **k: [])
    monkeypatch.setattr(agent_e1, "WriteClient", _RecordingWriteClient)
    _patch_infra(monkeypatch)

    assert agent_e1.main() == 0

    report = _reports(capsys)[0]
    assert report["desired"] == 1                      # только устройство
    assert report["unsupported"]["count"] == 1         # регион — с причиной
    assert sum(report["unsupported"]["by_reason"].values()) == 1
    assert report["result"]["failed"] == 0

    sent = _RecordingWriteClient.instances[0].sent
    assert len(sent) == 1
    item = sent[0][2]["BidModifiers"][0]
    assert item["DesktopAdjustment"]["BidModifier"] == 130   # дельта +30 → 100-база
    assert "MobileAdjustment" not in item


# --------------------------------- Э2.2: личный план кампании поверх кабинетного


def _patch_single_account(monkeypatch, computed, campaign_computed=None):
    """Один кабинет acc-1 с кампанией 111 без текущих корректировок."""
    _RecordingWriteClient.instances = []
    monkeypatch.setattr(agent_e1, "_clients", lambda: [{"login": "acc-1"}])
    monkeypatch.setattr(agent_e1.writer_db, "ensure_writer_tables", lambda: None)
    monkeypatch.setattr(agent_e1.agent_db, "load_latest_computed_settings",
                        lambda *_: list(computed))
    monkeypatch.setattr(agent_e1.agent_db, "load_holdout_ids", lambda: [])
    monkeypatch.setattr(agent_e1.agent_db, "load_daily_cost_by_campaign",
                        lambda *_: {"111": 500.0})
    monkeypatch.setattr(agent_e1.agent_db, "load_baseline_cpa", lambda *_: {"111": 1000.0})
    monkeypatch.setattr(agent_e1.agent_db, "crm_maturity_date",
                        lambda: date.today() - timedelta(days=3))
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit", lambda *_: 50_000.0)
    monkeypatch.setattr(agent_e1.writer_db, "spent_risk", lambda *_: 0.0)
    monkeypatch.setattr(agent_e1.writer_db, "charged_risk_by_object", lambda *_: {})
    monkeypatch.setattr(agent_e1.writer_db, "find_action_by_key", lambda *_: None)
    monkeypatch.setattr(agent_e1.writer_db, "insert_action", lambda action: 1)
    monkeypatch.setattr(agent_e1.writer_db, "mark_action", lambda *a, **k: True)
    monkeypatch.setattr(agent_e1.writer_db, "mark_unknown_outcome", lambda *a, **k: True)
    monkeypatch.setattr(agent_e1.writer_db, "mark_stale_planned", lambda *a, **k: [])
    monkeypatch.setattr(agent_e1, "WriteClient", _RecordingWriteClient)
    _patch_infra(monkeypatch, campaign_computed=campaign_computed)


def test_campaign_with_own_values_gets_its_plan_not_the_accounts(monkeypatch, capsys):
    # Кабинет держит DESKTOP +30, личный расчёт кампании 111 говорит −20
    # (Э2.2: дельты до 76 п.п. с переворотом знака). Применяться обязан
    # личный план, и отчёт обязан показать, что применялся именно он.
    _patch_single_account(
        monkeypatch,
        computed=[_setting("bid_modifier:device", "DESKTOP", 30.0)],
        campaign_computed={"111": [_setting("bid_modifier:device", "DESKTOP", -20.0)]},
    )

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    sent = _RecordingWriteClient.instances[0].sent
    assert len(sent) == 1
    item = sent[0][2]["BidModifiers"][0]
    assert item["DesktopAdjustment"]["BidModifier"] == 80    # −20 → 100-база
    assert report["campaign_level"] == {
        "campaigns_with_own_values": 1, "fallback_account": 0, "stale_dropped": 0}


def test_campaign_without_own_values_falls_back_to_account(monkeypatch, capsys):
    _patch_single_account(
        monkeypatch,
        computed=[_setting("bid_modifier:device", "DESKTOP", 30.0)],
        campaign_computed=None,
    )

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    item = _RecordingWriteClient.instances[0].sent[0][2]["BidModifiers"][0]
    assert item["DesktopAdjustment"]["BidModifier"] == 130
    assert report["campaign_level"] == {
        "campaigns_with_own_values": 0, "fallback_account": 1, "stale_dropped": 0}


def test_stale_campaign_rows_do_not_apply_and_are_counted(monkeypatch, capsys):
    # Личные строки старше MAX_COMPUTED_AGE_DAYS не применяются — та же
    # рельса свежести, что у кабинетных, — и отказ виден счётчиком, а не
    # тихим откатом на кабинетный план.
    old = (date.today() - timedelta(days=agent_e1.MAX_COMPUTED_AGE_DAYS + 1)).isoformat()
    _patch_single_account(
        monkeypatch,
        computed=[_setting("bid_modifier:device", "DESKTOP", 30.0)],
        campaign_computed={"111": [_setting("bid_modifier:device", "DESKTOP", -20.0,
                                            calc_date=old)]},
    )

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    item = _RecordingWriteClient.instances[0].sent[0][2]["BidModifiers"][0]
    assert item["DesktopAdjustment"]["BidModifier"] == 130   # кабинетный план
    assert report["campaign_level"]["stale_dropped"] == 1
    assert report["campaign_level"]["campaigns_with_own_values"] == 0


def test_campaign_rows_alone_are_enough_without_account_plan(monkeypatch, capsys):
    # У кабинета корректировок нет вовсе (например, срез выродился), а личный
    # расчёт кампании есть. Прежний ранний выход «нет кабинетных настроек»
    # молча прятал бы личные значения — теперь они применяются.
    _patch_single_account(
        monkeypatch,
        computed=[],
        campaign_computed={"111": [_setting("bid_modifier:device", "MOBILE", 25.0)]},
    )

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    item = _RecordingWriteClient.instances[0].sent[0][2]["BidModifiers"][0]
    assert item["MobileAdjustment"]["BidModifier"] == 125
    # Полный отчёт кампании, а не ранний выход NO_COMPUTED_SETTINGS.
    assert report.get("verdict") is None
    assert report["campaign_level"]["campaigns_with_own_values"] == 1


# =========================================================================
# Оркестратор прогона: настройки по кабинетам, лимит и риск на прогон,
# состав репетиции и зависшие записи журнала.
# =========================================================================


class _MultiCabinetClient:
    """Кабинет с заданным набором кампаний и пустыми корректировками.

    campaigns_by_login задаётся тестом; mutate только записывает вызовы.
    """

    def is_write_allowed(self):
        return True

    instances = []
    campaigns_by_login = {}

    def __init__(self, login, sandbox=True, dry_run=True):
        self.login = login
        self.sandbox = sandbox
        self.dry_run = dry_run
        self.units_left = None
        self.sent = []
        self.read = []
        _MultiCabinetClient.instances.append(self)

    def get(self, service, params):
        if service == "campaigns":
            ids = self.campaigns_by_login.get(self.login, [])
            return {"Campaigns": [{"Id": i} for i in ids]}
        if service == "bidmodifiers":
            # По каким кампаниям кабинет вообще ЧИТАЛСЯ. Ограничитель прогона
            # обязан урезать именно этот список: отсечение действий в конце
            # оставило бы кабинет прочитанным целиком.
            self.read += [str(c) for c in params["SelectionCriteria"]["CampaignIds"]]
            return {"BidModifiers": []}
        raise AssertionError(f"неожиданный сервис: {service}")

    def mutate(self, service, method, params):
        self.sent.append((service, method, params))
        return {"dry_run": True}

    def mutate_batch(self, service, method, collection, items):
        # Батч — ОДИН запрос (client.WriteClient.mutate_batch). Двойник
        # обязан повторять форму настоящего транспорта: иначе прогон в тесте
        # ходит одним путём, а в бою другим.
        return self.mutate(service, method, {collection: list(items)})


def _setting(kind, key, value, support=1000, calc_date=None):
    # calc_date по умолчанию сегодняшняя: возраст расчёта — рельса, и без даты
    # прогон обязан отказаться применять настройки (см. тесты свежести ниже).
    return {"setting_kind": kind, "setting_key": key, "value": float(value),
            "support_n": support, "raw_value": 1.0 + value / 100.0,
            "calc_date": calc_date or date.today().isoformat()}


def _reports(capsys):
    """Отчёты прогона: main() печатает по одному JSON на кабинет подряд."""
    out = capsys.readouterr().out
    decoder = json.JSONDecoder()
    reports = []
    idx = 0
    while idx < len(out):
        while idx < len(out) and out[idx].isspace():
            idx += 1
        if idx >= len(out):
            break
        obj, idx = decoder.raw_decode(out, idx)
        reports.append(obj)
    return reports


def _patch_run(monkeypatch, computed_by_login, campaigns_by_login, daily_cost,
               baseline_cpa=None, stale=(), journal=None, cooled=None, final_keys=(),
               lease=None, prod_apply=False, exhausted=None, argv=(),
               window_cost=None):
    # prod_apply — боевой режим (--prod --apply). Нужен там, где проверяется
    # изменение состояния журнала: по общему правилу движка
    # (writer/client.py::journal_writes_allowed) репетиция журнал не трогает.
    args = ["agent_e1"]
    if prod_apply:
        args += ["--prod", "--apply"]
    args += list(argv)
    if len(args) > 1:
        monkeypatch.setattr(sys, "argv", args)
    _MultiCabinetClient.instances = []
    _MultiCabinetClient.campaigns_by_login = campaigns_by_login
    logins = list(computed_by_login.keys())
    monkeypatch.setattr(agent_e1, "_clients", lambda: [{"login": x} for x in logins])
    monkeypatch.setattr(agent_e1.writer_db, "ensure_writer_tables", lambda: None)
    # Загрузчик вызывается С кабинетом. Значение по умолчанию оставлено
    # намеренно: на коде ДО правки вызов идёт без аргументов, и тест падает на
    # утверждениях о поведении, а не на TypeError.
    monkeypatch.setattr(
        agent_e1.agent_db, "load_latest_computed_settings",
        lambda login=None, *a, **k: list(computed_by_login.get(login, [])),
    )
    monkeypatch.setattr(agent_e1.agent_db, "load_holdout_ids", lambda: [])
    monkeypatch.setattr(agent_e1.agent_db, "load_daily_cost_by_campaign", lambda *_: daily_cost)
    monkeypatch.setattr(agent_e1.agent_db, "load_baseline_cpa",
                        lambda *_: dict(baseline_cpa or {c: 1000.0 for c in daily_cost}))
    monkeypatch.setattr(agent_e1.agent_db, "crm_maturity_date",
                        lambda: date.today() - timedelta(days=3))
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit", lambda *_: 50_000.0)
    monkeypatch.setattr(agent_e1.writer_db, "spent_risk", lambda *_: 0.0)
    monkeypatch.setattr(agent_e1.writer_db, "charged_risk_by_object", lambda *_: {})
    # Прогон не читает зависшие строки, а ПОМЕЧАЕТ их: mark_stale_planned
    # возвращает только впервые обнаруженные (writer/db.py::MARK_STALE_SQL).
    monkeypatch.setattr(agent_e1.writer_db, "mark_stale_planned", lambda *a, **k: list(stale))
    monkeypatch.setattr(agent_e1.writer_db, "find_action_by_key", lambda *_: None)
    rows = journal if journal is not None else []
    monkeypatch.setattr(agent_e1.writer_db, "insert_action",
                        lambda row: (rows.append(row), row["idempotency_key"])[1])
    monkeypatch.setattr(agent_e1.writer_db, "mark_action", lambda *a, **k: True)
    monkeypatch.setattr(agent_e1.writer_db, "mark_unknown_outcome", lambda *a, **k: True)
    monkeypatch.setattr(agent_e1, "WriteClient", _MultiCabinetClient)
    _patch_infra(monkeypatch, cooled=cooled, final_keys=final_keys, lease=lease,
                 exhausted=exhausted)
    # После _patch_infra: он ставит свою пустую заглушку суммы расхода, а
    # здесь она должна отвечать числами этого теста.
    monkeypatch.setattr(agent_e1.agent_db, "load_cost_by_campaign",
                        lambda *_: dict(window_cost if window_cost is not None
                                        else {cid: v * 28.0
                                              for cid, v in daily_cost.items()}))
    return rows


def _many_actions_per_campaign(monkeypatch):
    """Разрешить полосе несколько правок на одной кампании за прогон.

    Пока рычаг учится (ступени 1–2), полоса берёт с объекта ОДНО действие:
    иначе неизвестно, что именно сработало. Тесты ниже про другое — про цену
    объекта и состав отчёта, — и им нужен доказанный класс, где изоляция снята
    (writer/lanes.py: MULTI_LEVER_LANES + TOP_STEP). Ступень приезжает из
    панели (задача 26 плана беты); до неё все полосы стоят на DEFAULT_STEP,
    поэтому здесь двигается он.
    """
    monkeypatch.setattr(agent_e1.lanes, "DEFAULT_STEP", agent_e1.lanes.TOP_STEP)


# --------------------------------- дефект 1: настройки каждого кабинета — свои


def test_each_cabinet_gets_its_own_computed_settings(monkeypatch, capsys):
    # Расчёт Э0 идёт по кабинетам, и числа одного кабинета неприменимы к
    # другому. Загрузчик обязан взять настройки именно того кабинета, в
    # который сейчас пишет, а не общий схлопнутый набор.
    _patch_run(
        monkeypatch,
        computed_by_login={
            "acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)],
            "acc-2": [_setting("bid_modifier:device", "MOBILE", -20)],
        },
        campaigns_by_login={"acc-1": [111], "acc-2": [222]},
        daily_cost={"111": 100.0, "222": 100.0},
    )

    assert agent_e1.main() == 0

    sent_by_login = {c.login: c.sent for c in _MultiCabinetClient.instances}
    first = sent_by_login["acc-1"][0][2]["BidModifiers"][0]
    second = sent_by_login["acc-2"][0][2]["BidModifiers"][0]
    assert first["DesktopAdjustment"]["BidModifier"] == 130   # дельта +30
    assert "MobileAdjustment" not in first
    assert second["MobileAdjustment"]["BidModifier"] == 80    # дельта -20
    assert "DesktopAdjustment" not in second


def test_cabinet_without_computed_settings_is_visible_in_report(monkeypatch, capsys):
    # В таблице уже лежат данные, записанные по-старому: при чтении по-новому
    # они не находятся, и прогон честно ничего не применяет. Это правильное
    # поведение, но оно обязано быть видно в отчёте с причиной, а не выглядеть
    # как «нечего делать».
    _patch_run(
        monkeypatch,
        computed_by_login={
            "acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)],
            "acc-2": [],
        },
        campaigns_by_login={"acc-1": [111], "acc-2": [222]},
        daily_cost={"111": 100.0, "222": 100.0},
    )

    assert agent_e1.main() == 0

    reports = {r["account"]: r for r in _reports(capsys) if "account" in r}
    silent = reports["acc-2"]
    assert silent["verdict"] == "NO_COMPUTED_SETTINGS"
    assert silent["computed_settings"] == 0
    assert "object_id" in silent["reason"]
    assert "acc-2" in silent["reason"]
    # Кабинет с настройками при этом отработал как обычно.
    assert reports["acc-1"]["desired"] == 1


# ------------------------------------- дефект 3: лимит полосы — на прогон


def test_lane_budget_is_shared_across_cabinets(monkeypatch, capsys):
    # Тот же дефект, что был у лимита действий (cap_actions вызывался внутри
    # цикла по кабинетам, и при четырёх кабинетах потолок был вчетверо выше
    # заявленного), — теперь на полосах. Остаток полосы едет между кабинетами
    # одним словарём, поэтому второй кабинет видит то, что потратил первый.
    settings = [_setting("bid_modifier:device", "DESKTOP", 30)]
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": settings, "acc-2": settings},
        campaigns_by_login={"acc-1": [111, 112], "acc-2": [221, 222]},
        daily_cost={"111": 10.0, "112": 10.0, "221": 10.0, "222": 10.0},
    )

    assert agent_e1.main() == 0

    reports = [r for r in _reports(capsys) if "account" in r]
    spent = [r["lanes"]["spent"] for r in reports]
    assert spent[0] != {} or spent[1] != {}
    # Второй кабинет продолжает счёт первого, а не начинает свой.
    first = float((spent[0].get("tuning") or {}).get("risk_rub") or 0.0)
    second = float((spent[1].get("tuning") or {}).get("risk_rub") or 0.0)
    assert second >= first, "полоса начала счёт заново на втором кабинете"


# --------------------------- дефект 4: риск кампании списывается один раз


def test_campaign_risk_is_capped_by_the_object_price(monkeypatch, capsys):
    # Дельта-модель: каждая правка платит цену СВОЕГО изменения (доля сегмента
    # × насколько его двигают), а сумма списаний по кампании ограничена её
    # ценой целиком — расходом за горизонт замера. Прежде первое же действие
    # платило за всю кампанию, и бюджет исчерпывался на второй-третьей.
    journal = []
    _many_actions_per_campaign(monkeypatch)
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [
            _setting("bid_modifier:device", "DESKTOP", 30),
            _setting("bid_modifier:device", "MOBILE", 20),
            _setting("bid_modifier:gender", "GENDER_MALE", 15),
        ]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 1000.0},   # риск кампании = 1000 × 7 = 7000
        journal=journal,
    )

    assert agent_e1.main() == 0

    report = _reports(capsys)[0]
    assert report["prepared"]["count"] == 3
    assert report["deferred_by_risk"] == 0
    # Три корректировки сегментов кампании стоят долей её расхода, а не
    # трёх её расходов: сумма меньше цены кампании целиком (7000 ₽).
    assert 0 < report["risk_charged_rub"] <= 7000.0
    assert sum(row["risk_rub"] for row in journal) == report["risk_charged_rub"]


def test_budget_is_not_exhausted_by_repeated_actions_on_same_campaign(monkeypatch, capsys):
    # Тот же расчёт «в лоб»: три действия по кампании с расходом 1000 ₽/день
    # стоили бы 21 000 ₽ при доступных 10 000 ₽ — два из трёх ушли бы в
    # отложенные, хотя реальная цена ошибки по этой кампании 7000 ₽.
    #
    # Недельный лимит здесь — семь дневных долей, чтобы прогону досталось
    # ровно те 10 000 ₽ в любой день недели: проверяется модель ПОТОЛКА
    # ОБЪЕКТА, и распределение недели по дням (paced_allowance) не должно
    # подмешиваться в неё днём запуска.
    journal = []
    _many_actions_per_campaign(monkeypatch)
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [
            _setting("bid_modifier:device", "DESKTOP", 30),
            _setting("bid_modifier:device", "MOBILE", 20),
            _setting("bid_modifier:gender", "GENDER_MALE", 15),
        ]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 1000.0},
        journal=journal,
    )
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit",
                        lambda *_: 10_000.0 * risk.DAYS_IN_WEEK)

    assert agent_e1.main() == 0

    report = _reports(capsys)[0]
    assert report["deferred_by_risk"] == 0
    assert report["result"]["dry_run"] == 3



def test_object_paid_for_in_an_earlier_run_this_week_is_not_charged_again(monkeypatch, capsys):
    # Реальный случай недели 2026-08-17: прогон 32559366898 списал 38 876 ₽ за
    # кампанию 114057545 (5 554 ₽/день × 7). Следующий прогон посчитал ей
    # расписание — и потребовал те же 38 876 повторно, при остатке 11 124.
    # Одна кампания съедала 78 % недельного бюджета за КАЖДОЕ касание, и
    # конвейер гипотез был этим заперт: множество оплаченных объектов
    # создавалось заново на каждый запуск, поэтому довод «расход у кампании
    # один» работал внутри прогона и переставал работать между прогонами.
    journal = []
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [
            _setting("bid_modifier:device", "DESKTOP", 30),
        ]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 5_554.0},          # риск кампании = 38 878 ₽
        journal=journal,
    )
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit", lambda *_: 50_000.0)
    monkeypatch.setattr(agent_e1.writer_db, "spent_risk", lambda *_: 38_878.0)
    # По кампании уже списана её полная цена — потолок объекта исчерпан,
    # и следующая правка по ней проходит, ничего не добавляя к риску.
    monkeypatch.setattr(agent_e1.writer_db, "charged_risk_by_object",
                        lambda *_: {"campaign:111": 38_878.0})

    assert agent_e1.main() == 0

    report = _reports(capsys)[0]
    assert report["deferred_by_risk"] == 0
    assert report["prepared"]["count"] == 1
    assert report["risk_charged_rub"] == 0.0
    # Второе касание оплаченного объекта стоит НОЛЬ: цена ошибки по кампании
    # уже под наблюдением, второй раз тот же расход не тратится.
    assert report["risk_charged_rub"] == 0.0
    assert [row["risk_rub"] for row in journal] == [0.0]


def test_untouched_campaign_still_pays_full_price(monkeypatch, capsys):
    # Обратная сторона: «уже оплачено» относится к КОНКРЕТНОМУ объекту.
    # Если освободить от платы всех подряд, риск-бюджет перестаёт быть
    # бюджетом — а это единственный тормоз перед боевым кабинетом.
    journal = []
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [
            _setting("bid_modifier:device", "DESKTOP", 30),
        ]},
        campaigns_by_login={"acc-1": [222]},
        daily_cost={"222": 1_000.0},
        journal=journal,
    )
    monkeypatch.setattr(agent_e1.writer_db, "charged_risk_by_object",
                        lambda *_: {"campaign:111": 7_000.0})

    assert agent_e1.main() == 0

    report = _reports(capsys)[0]
    # Списания по ЧУЖОЙ кампании нашу не освобождают: она платит свою дельту.
    assert report["risk_charged_rub"] > 0


def test_charged_risk_is_read_for_the_same_week_as_the_budget(monkeypatch, capsys):
    # Оплаченные объекты и потраченный бюджет обязаны читаться за ОДНУ неделю.
    # Разъехались бы — прогон освобождал бы от платы по одной границе, а
    # остаток считал по другой, и перерасход был бы невидим.
    seen = {}
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
    )
    def _spent(wk):
        seen["spent"] = wk
        return 0.0

    def _limit(wk, *_):
        seen["limit"] = wk
        return 50_000.0

    def _charged(wk):
        seen["charged"] = wk
        return {}

    monkeypatch.setattr(agent_e1.writer_db, "spent_risk", _spent)
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit", _limit)
    monkeypatch.setattr(agent_e1.writer_db, "charged_risk_by_object", _charged)

    assert agent_e1.main() == 0

    assert seen["charged"] == seen["spent"] == seen["limit"]


def test_baseline_cpa_stops_at_crm_maturity_not_at_today(monkeypatch, capsys):
    # Расход Директа приезжает вовремя, лиды CRM — с отставанием 2-4 дня, и
    # день приходит целиком либо не приходит вовсе. Окно до сегодня делит
    # расход тридцати полных дней на лиды двадцати шести — база завышена, и
    # всегда в одну сторону. Из базы растёт порог отката (×1.4): завышение
    # делает сторож мягче ровно там, где он единственная защита кабинета.
    seen = {}
    crm_through = date.today() - timedelta(days=4)

    def _baseline(d_from, d_to):
        seen["window"] = (d_from, d_to)
        return {"111": 1000.0}

    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
    )
    monkeypatch.setattr(agent_e1.agent_db, "crm_maturity_date", lambda: crm_through)
    monkeypatch.setattr(agent_e1.agent_db, "load_baseline_cpa", _baseline)

    assert agent_e1.main() == 0

    assert seen["window"][1] == crm_through.isoformat()


def test_cost_window_is_not_trimmed_by_crm_lag(monkeypatch, capsys):
    # Обратная сторона: обрезать по зрелости CRM надо ТОЛЬКО базу CPA.
    # Дневной расход — источник цены риска, он приезжает вовремя, и урезание
    # его окна занизило бы риск, то есть ослабило бы тормоз перед кабинетом.
    seen = {}

    def _cost(d_from, d_to):
        seen["window"] = (d_from, d_to)
        return {"111": 10.0}

    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
    )
    monkeypatch.setattr(agent_e1.agent_db, "crm_maturity_date",
                        lambda: date.today() - timedelta(days=4))
    monkeypatch.setattr(agent_e1.agent_db, "load_daily_cost_by_campaign", _cost)

    assert agent_e1.main() == 0

    assert seen["window"][1] == date.today().isoformat()


def test_no_mature_crm_day_means_no_baseline_not_a_stale_one(monkeypatch, capsys):
    # Лидов нет вовсе — базы не существует. Подставлять сюда окно до сегодня
    # значило бы считать базу по дням с расходом и нулём лидов: CPA
    # бесконечен, и красная линия оказалась бы пробита на ровном месте.
    called = []

    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
    )
    monkeypatch.setattr(agent_e1.agent_db, "crm_maturity_date", lambda: None)
    monkeypatch.setattr(agent_e1.agent_db, "load_baseline_cpa",
                        lambda *a: called.append(a) or {"111": 1000.0})

    assert agent_e1.main() == 0

    assert called == []

# ------------------------- дефект 6: репетиция показывает, что было бы записано


def test_dry_run_report_shows_what_would_be_written(monkeypatch, capsys):
    # Главный артефакт для решения «включать боевую запись» показывал нули и
    # не содержал ни числа готовых действий, ни их состава.
    _many_actions_per_campaign(monkeypatch)
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [
            _setting("bid_modifier:device", "DESKTOP", 30),
            _setting("bid_modifier:gender", "GENDER_MALE", -15),
        ]},
        campaigns_by_login={"acc-1": [111, 112]},
        daily_cost={"111": 10.0, "112": 10.0},
    )

    assert agent_e1.main() == 0

    report = _reports(capsys)[0]
    assert report["dry_run"] is True
    assert report["result"]["dry_run"] == 4          # 2 кампании × 2 корректировки
    assert report["prepared"]["count"] == 4
    assert report["prepared"]["by_setting"] == {
        "DEMOGRAPHICS_ADJUSTMENT:GENDER_MALE -15% (add)": 2,
        "DESKTOP_ADJUSTMENT:DESKTOP +30% (add)": 2,
    }
    assert report["prepared"]["sample"]
    assert report["prepared"]["sample_truncated"] is False


def test_dry_run_preview_stays_compact_on_many_actions():
    # Состав показывается агрегатом плюс несколько примеров: полный список из
    # полусотни строк превратил бы отчёт в стену текста.
    actions = [{"object_id": str(i), "action_kind": "bidmodifier.add",
                "direct_type": "MOBILE_ADJUSTMENT", "key": "MOBILE",
                "payload": {"BidModifier": 20}} for i in range(50)]

    preview = agent_e1.actions_preview(actions)

    assert preview["count"] == 50
    assert preview["by_setting"] == {"MOBILE_ADJUSTMENT:MOBILE +20% (add)": 50}
    assert len(preview["sample"]) == agent_e1.PREVIEW_SAMPLE_LIMIT
    assert preview["sample_truncated"] is True


# --------------------- дефект 7: зависшие после обрыва записи видны в отчёте


def test_stuck_planned_rows_are_reported(monkeypatch, capsys):
    # Обрыв ПОСЛЕ отправки: изменение в кабинете состоялось, строка осталась
    # planned. Её не видит ни сторож применённых действий, ни откат; риск не
    # списан; diff следующего прогона новых действий не предложит, потому что
    # факт уже совпал. Расхождение обязано всплыть в отчёте прогона.
    stuck = [{"action_id": "abc123", "idempotency_key": "k-stuck",
              "account": "acc-1", "object_level": "campaign", "object_id": "111",
              "action_kind": "bidmodifier.add", "created_at": "2026-08-18T10:00:00+00:00"}]
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
        stale=stuck,
        prod_apply=True,
    )

    assert agent_e1.main() == 0

    report = _reports(capsys)[0]
    assert report["stale_planned"]["count"] == 1
    assert report["stale_planned"]["older_than_minutes"] == agent_e1.STALE_PLANNED_MINUTES
    assert report["stale_planned"]["sample"][0]["action_id"] == "abc123"
    assert report["stale_planned"]["sample"][0]["object_id"] == "111"


def test_stuck_rows_are_asked_per_cabinet(monkeypatch, capsys):
    # Запрос идёт с кабинетом и порогом: чужие зависшие строки в отчёте
    # кабинета не нужны.
    calls = []
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)],
                           "acc-2": []},
        campaigns_by_login={"acc-1": [111], "acc-2": [222]},
        daily_cost={"111": 10.0, "222": 10.0},
        prod_apply=True,
    )
    monkeypatch.setattr(agent_e1.writer_db, "mark_stale_planned",
                        lambda minutes, account=None: calls.append((minutes, account)) or [])

    assert agent_e1.main() == 0

    assert calls == [(agent_e1.STALE_PLANNED_MINUTES, "acc-1"),
                     (agent_e1.STALE_PLANNED_MINUTES, "acc-2")]


def test_stuck_rows_are_marked_not_only_printed(monkeypatch, capsys):
    # Дефект: зависшая строка печаталась в отчёте КАЖДОГО прогона вечно, и
    # больше с ней не происходило ничего — её не видел ни сторож применённых
    # действий, ни откат, риск по ней не был списан. Прогон обязан ПОМЕТИТЬ
    # находку (перевести в статус 'stale'), а не ограничиться чтением.
    stuck = [{"action_id": "abc123", "idempotency_key": "k-stuck",
              "account": "acc-1", "object_level": "campaign", "object_id": "111",
              "action_kind": "bidmodifier.add", "created_at": "2026-08-18T10:00:00+00:00"}]
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
        prod_apply=True,
    )
    marked = []
    monkeypatch.setattr(agent_e1.writer_db, "mark_stale_planned",
                        lambda minutes, account=None: marked.append(account) or list(stuck))

    assert agent_e1.main() == 0

    assert marked == ["acc-1"]
    # Чтения без пометки было недостаточно: находка повторялась бы вечно.
    # Читающий-без-пометки вариант удалён — вернуть его молча уже нельзя.
    assert not hasattr(agent_e1.writer_db, "stale_planned")
    report = _reports(capsys)[0]
    assert report["stale_planned"]["count"] == 1
    assert report["stale_planned"]["marked_status"] == "stale"


def test_second_run_does_not_repeat_the_same_stuck_row(monkeypatch, capsys):
    # Второй прогон: строка уже помечена, mark_stale_planned её не возвращает —
    # отчёт про неё молчит. Сама находка не потеряна: она видна через
    # open_actions (живой тест — tests/test_agent_writer_db.py).
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
        stale=(),
    )

    assert agent_e1.main() == 0

    report = _reports(capsys)[0]
    assert report["stale_planned"]["count"] == 0
    assert report["stale_planned"]["sample"] == []


# =========================================================================
# Дефект A: откат ничего не сообщал планированию, и обходился сдвигом на процент
# =========================================================================


def _bidmod_key(campaign_id, direct_type, key, percent):
    from sync.agent.writer.diff import _idempotency_key
    return _idempotency_key(campaign_id, direct_type, key, percent)


def test_cooldown_key_ignores_percent_drift():
    # Ключ истории — объект и сегмент, БЕЗ процента. Именно на проценте цикл и
    # держался: применили 30, откатили, назавтра расчёт дал 29 — другой ключ
    # идемпотентности, и то же вредное изменение уезжало снова.
    cooled = {("111", "MOBILE_ADJUSTMENT", "MOBILE"): "2026-08-01"}
    actions = [
        {"object_id": "111", "direct_type": "MOBILE_ADJUSTMENT", "key": "MOBILE",
         "payload": {"BidModifier": 30}, "idempotency_key": "a"},
        {"object_id": "111", "direct_type": "MOBILE_ADJUSTMENT", "key": "MOBILE",
         "payload": {"BidModifier": 29}, "idempotency_key": "b"},
    ]

    allowed, blocked = agent_e1.split_by_cooldown(actions, cooled)

    assert allowed == []
    assert len(blocked) == 2, "сдвиг процента не должен открывать кулдаун"
    assert all("кулдаун" in b["blocked_reason"] for b in blocked)


def test_cooldown_does_not_block_other_segments_of_same_campaign():
    # Кулдаун адресный: вредным признан сегмент, а не вся кампания.
    cooled = {("111", "MOBILE_ADJUSTMENT", "MOBILE"): "2026-08-01"}
    actions = [
        {"object_id": "111", "direct_type": "DESKTOP_ADJUSTMENT", "key": "DESKTOP",
         "payload": {"BidModifier": 30}, "idempotency_key": "a"},
        {"object_id": "222", "direct_type": "MOBILE_ADJUSTMENT", "key": "MOBILE",
         "payload": {"BidModifier": 30}, "idempotency_key": "b"},
    ]

    allowed, blocked = agent_e1.split_by_cooldown(actions, cooled)

    assert [a["idempotency_key"] for a in allowed] == ["a", "b"]
    assert blocked == []


def test_cooldown_is_longer_than_full_observation_cycle():
    # Кулдаун короче полного цикла наблюдения цикл не разрывает, а удлиняет:
    # сегмент так же уезжает снова, просто реже. Порог сверяется с окном
    # сторожа, а не назначен на глаз.
    import sync.agent_e1_watchdog as watchdog

    full_cycle = watchdog.OBSERVATION_LAG_DAYS + watchdog.OBSERVATION_HORIZON_DAYS
    assert agent_e1.COOLDOWN_AFTER_ROLLBACK_DAYS >= 2 * full_cycle
    # И длиннее окна, из которого берутся расход и базовый CPA: иначе повтор
    # судился бы по базе, испорченной откатанным изменением.
    assert agent_e1.COOLDOWN_AFTER_ROLLBACK_DAYS > 30


def test_rolled_back_segment_is_cut_before_the_lane_selection(monkeypatch, capsys):
    # Сквозной: оба сегмента живут на ОДНОЙ кампании, а полоса корректировок
    # на своей ступени берёт с объекта одно действие — место ровно одно. Если
    # отсев стоит ПОСЛЕ отбора (или его нет вовсе), запертое действие занимает
    # это место и в кабинет не уходит ничего. Кулдаун обязан отсекать ДО.
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [
            _setting("bid_modifier:device", "DESKTOP", 30),
            _setting("bid_modifier:device", "MOBILE", 20),
        ]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
        cooled={("111", "DESKTOP_ADJUSTMENT", "DESKTOP"): "2026-08-01"},
    )

    assert agent_e1.main() == 0

    sent = [c for inst in _MultiCabinetClient.instances for c in inst.sent]
    assert len(sent) == 1, "место в полосе обязано достаться незапертому сегменту"
    assert "MobileAdjustment" in sent[0][2]["BidModifiers"][0]
    report = _reports(capsys)[0]
    assert report["blocked_by_cooldown"]["count"] == 1
    assert report["blocked_by_cooldown"]["cooldown_days"] == \
        agent_e1.COOLDOWN_AFTER_ROLLBACK_DAYS
    assert report["blocked_by_cooldown"]["segments"] == ["111:DESKTOP_ADJUSTMENT:DESKTOP"]


def test_action_closed_by_idempotency_does_not_eat_the_lane(monkeypatch, capsys):
    # Второе следствие той же природы: действие с уже закрытым ключом
    # доходило до применения и отсеивалось только там — а место в полосе
    # занимало. Накопившиеся закрытые ключи стабильно съедали объём прогона
    # целиком: «подготовлено пятьдесят, применено ноль».
    closed = _bidmod_key("111", "DESKTOP_ADJUSTMENT", "DESKTOP", 30)
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [
            _setting("bid_modifier:device", "DESKTOP", 30),
            _setting("bid_modifier:device", "MOBILE", 20),
        ]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
        final_keys=(closed,),
    )

    assert agent_e1.main() == 0

    sent = [c for inst in _MultiCabinetClient.instances for c in inst.sent]
    assert len(sent) == 1
    assert "MobileAdjustment" in sent[0][2]["BidModifiers"][0]
    report = _reports(capsys)[0]
    assert report["skipped_already_final"]["count"] == 1


# =========================================================================
# Дефект B: параллельный запуск
# =========================================================================


def test_second_simultaneous_run_refuses_to_start(monkeypatch, capsys):
    # Два одновременных прогона на одном ключе создают в кабинете ДВА объекта,
    # и Id первого теряется навсегда — без строки журнала и без красной линии.
    monkeypatch.setattr(agent_e1, "_clients", lambda: [{"login": "acc-1"}])
    monkeypatch.setattr(agent_e1.writer_db, "ensure_writer_tables", lambda: None)

    @contextmanager
    def _busy(*a, **k):
        raise agent_e1.writer_db.RunLockBusy("прогон agent_e1 уже идёт")
        yield  # pragma: no cover

    monkeypatch.setattr(agent_e1.writer_db, "run_lock", _busy)
    monkeypatch.setattr(agent_e1, "_run_all",
                        lambda *a, **k: pytest.fail("прогон стартовал вторым"))

    assert agent_e1.main() == 1

    report = _reports(capsys)[0]
    assert report["verdict"] == "RUN_LOCKED"


# =========================================================================
# Сопутствующее: ноль вместо нейтрали, свежесть расчёта, падение кабинета
# =========================================================================


def test_missing_bid_modifier_is_unusable_not_full_suppression():
    # Подстановка нуля превращала отсутствующий коэффициент в
    # api_to_delta(0) = -100, то есть «подавить сегмент на сто процентов».
    # Это уезжало в previous_state, и откат вместо возврата ставки выставлял
    # бы коэффициент 0 — бил бы сильнее исходного изменения.
    out = agent_e1._normalize_actual({"Id": 9, "MobileAdjustment": {"Foo": 1}})

    assert len(out) == 1
    assert out[0]["percent"] != -100
    assert out[0]["percent"] is None
    assert out[0]["unusable"] is True


def test_unusable_actual_produces_no_action_at_all():
    # Ни set (прошлое состояние неизвестно — откат станет невозможен), ни add
    # (объект в кабинете есть, второй такой же создавать нельзя).
    from sync.agent.writer.diff import diff_modifiers

    desired = [{"kind": "bid_modifier:device", "direct_type": "MOBILE_ADJUSTMENT",
                "key": "MOBILE", "percent": 30}]
    actual = agent_e1._normalize_actual({"Id": 9, "MobileAdjustment": {"Foo": 1}})

    assert diff_modifiers(desired, actual, campaign_id="111") == []


def test_stale_computed_settings_are_not_applied(monkeypatch, capsys):
    # «Последний расчёт» не значит «свежий»: без верхней границы возраста
    # движок раскатает месячные коэффициенты.
    old = (date.today() - timedelta(days=agent_e1.MAX_COMPUTED_AGE_DAYS + 1)).isoformat()
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [
            _setting("bid_modifier:device", "DESKTOP", 30, calc_date=old)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
    )

    assert agent_e1.main() == 0

    sent = [c for inst in _MultiCabinetClient.instances for c in inst.sent]
    assert sent == []
    report = _reports(capsys)[0]
    assert report["verdict"] == "STALE_COMPUTED_SETTINGS"
    assert str(agent_e1.MAX_COMPUTED_AGE_DAYS) in report["reason"]
    assert report["computed_age_days"] == agent_e1.MAX_COMPUTED_AGE_DAYS + 1


def test_computed_settings_within_max_age_are_applied(monkeypatch, capsys):
    # Граница включающая: расчёт ровно предельного возраста ещё применяется —
    # иначе рельса резала бы строже заявленного.
    edge = (date.today() - timedelta(days=agent_e1.MAX_COMPUTED_AGE_DAYS)).isoformat()
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [
            _setting("bid_modifier:device", "DESKTOP", 30, calc_date=edge)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
    )

    assert agent_e1.main() == 0

    sent = [c for inst in _MultiCabinetClient.instances for c in inst.sent]
    assert len(sent) == 1


def test_computed_settings_without_calc_date_are_refused(monkeypatch, capsys):
    # «Возраст неизвестен» — не «свежо».
    setting = _setting("bid_modifier:device", "DESKTOP", 30)
    setting.pop("calc_date")
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [setting]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
    )

    assert agent_e1.main() == 0

    sent = [c for inst in _MultiCabinetClient.instances for c in inst.sent]
    assert sent == []
    report = _reports(capsys)[0]
    assert report["verdict"] == "STALE_COMPUTED_SETTINGS"
    assert report["reason"] == agent_e1.UNKNOWN_COMPUTED_DATE_REASON


def test_failure_on_one_cabinet_does_not_block_the_others(monkeypatch, capsys):
    # Четыре кабинета, падение на первом — остальные обязаны быть обработаны,
    # а отказ обязан быть виден в отчёте и в коде возврата.
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)],
                           "acc-2": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111], "acc-2": [221]},
        daily_cost={"111": 10.0, "221": 10.0},
    )
    real_run_account = agent_e1.run_account

    def _boom(login, *a, **k):
        if login == "acc-1":
            raise RuntimeError("кабинет отвалился")
        return real_run_account(login, *a, **k)

    monkeypatch.setattr(agent_e1, "run_account", _boom)

    assert agent_e1.main() == 1, "частичный отказ обязан быть виден кодом возврата"

    reports = _reports(capsys)
    by_account = {r.get("account"): r for r in reports if r.get("account")}
    assert by_account["acc-1"]["verdict"] == "ACCOUNT_FAILED"
    assert by_account["acc-2"]["result"]["dry_run"] == 1, "второй кабинет обработан"
    assert reports[-1]["verdict"] == "PARTIAL_FAILURE"
    assert reports[-1]["failed_accounts"][0]["account"] == "acc-1"


# =========================================================================
# Дефект 5: аренда прогона живёт дольше часа только если её продлевать
# =========================================================================


def test_run_renews_the_lease_while_reading_campaign_state(monkeypatch, capsys):
    # Аренду берут на час, а прогон читает состояние по каждой кампании — с
    # ретраями и таймаутом в две минуты на запрос. Сотни кампаний легко
    # переживают срок аренды, и тогда второй прогон стартует штатно: оба шлют
    # bidmodifiers.add по одной кампании, второй объект в кабинете, Id первого
    # не знает никто.
    lease = _FakeLease()
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111, 222, 333]},
        daily_cost={"111": 10.0, "222": 10.0, "333": 10.0},
        lease=lease,
    )
    # Считаем перепроверки, увиденные ИМЕННО на чтении состояния кампаний:
    # проверок на отправке недостаточно — до отправки прогон успевает
    # прожить всё самое долгое время.
    seen_on_read = []
    real_actual = agent_e1._actual_modifiers
    monkeypatch.setattr(
        agent_e1, "_actual_modifiers",
        lambda client, campaign_id: (seen_on_read.append(lease.guards),
                                     real_actual(client, campaign_id))[1],
    )

    assert agent_e1.main() == 0
    capsys.readouterr()

    assert seen_on_read == [1, 2, 3], "аренда продлевается на каждой кампании"


def test_lost_lease_aborts_the_whole_run_not_just_one_cabinet(monkeypatch, capsys):
    # Потеря аренды означает, что в кабинет уже может писать второй прогон.
    # Это не отказ одного кабинета: следующий кабинет писать тем более не
    # вправе, и прогон обязан оборваться с явным вердиктом.
    lease = _FakeLease(lost=True)
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)],
                           "acc-2": [_setting("bid_modifier:device", "MOBILE", -20)]},
        campaigns_by_login={"acc-1": [111], "acc-2": [222]},
        daily_cost={"111": 10.0, "222": 10.0},
        lease=lease,
    )

    assert agent_e1.main() == 1

    out = capsys.readouterr().out
    assert "RUN_LEASE_LOST" in out
    assert "PARTIAL_FAILURE" not in out, "потеря аренды — не отказ отдельного кабинета"
    assert all(c.sent == [] for c in _MultiCabinetClient.instances)


# =========================================================================
# Мелкое: репетиция не меняет журнал, а её собственные строки не вечны
# =========================================================================


def test_rehearsal_does_not_mark_stuck_rows_in_the_journal(monkeypatch, capsys):
    # Правило журнала одно на оба рабочих процесса: сторож в репетиции журнал
    # не трогает, и прямое применение тоже. Пометка 'stale' закрывает строку
    # от повторной отправки И списывает за неё риск-бюджет — репетиция делала
    # это, ничего никуда не отправив.
    called = []
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
    )
    monkeypatch.setattr(agent_e1.writer_db, "mark_stale_planned",
                        lambda *a, **k: called.append(1) or [])

    assert agent_e1.main() == 0

    assert called == []
    report = _reports(capsys)[0]
    # Молчать об этом нельзя: «ноль зависших строк» и «мы их не искали» —
    # разные состояния.
    assert report["stale_planned"]["journal_written"] is False
    assert report["stale_planned"]["skipped_reason"] == agent_e1.REHEARSAL_STALE_REASON


def test_prod_apply_still_marks_stuck_rows(monkeypatch, capsys):
    # Обратная половина правила: боевая запись помечать обязана, иначе
    # зависшая строка снова стала бы вечным шумом в отчёте.
    called = []
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
        prod_apply=True,
    )
    monkeypatch.setattr(agent_e1.writer_db, "mark_stale_planned",
                        lambda *a, **k: called.append(1) or [])

    assert agent_e1.main() == 0

    assert called == [1]
    assert _reports(capsys)[0]["stale_planned"]["journal_written"] is True


def test_run_purges_old_rehearsal_rows(monkeypatch, capsys):
    # У строк репетиции не было судьбы вообще: статус не финальный и не живой,
    # ни один механизм журнала их не закрывает — они копились вечно.
    purged = []
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
    )
    monkeypatch.setattr(agent_e1.writer_db, "purge_dry_run_actions",
                        lambda days: purged.append(days) or 3)

    assert agent_e1.main() == 0

    assert purged == [agent_e1.writer_db.DRY_RUN_RETENTION_DAYS]
    maintenance = [r for r in _reports(capsys) if r.get("verdict") == "JOURNAL_MAINTENANCE"]
    assert maintenance and maintenance[0]["dry_run_rows_purged"] == 3


def test_run_reports_share_of_spend_it_cannot_see(monkeypatch, capsys):
    # «Изменили десять кампаний» без слепой доли читается как «кабинет взят
    # под управление», хотя часть денег живёт вне витрины настроек и на неё
    # эти изменения не влияют никак.
    #
    # Знаменатель — СУММА расхода за окно, а не средний дневной × 28: второе
    # растягивает темп кампании, отработавшей часть окна, на всё окно, и доля
    # выходит про расход, которого не было. Кампания «999» здесь как раз
    # такая: темп 300 в день, но за окно потратила 1000.
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 100.0, "999": 300.0},
        window_cost={"111": 1000.0, "999": 1000.0},
    )
    monkeypatch.setattr(agent_e1.agent_db, "load_campaign_settings_raw",
                        lambda: {"111": {}})

    assert agent_e1.main() == 0

    blind = [r for r in _reports(capsys) if r.get("verdict") == "BLIND_SPEND"]
    assert blind, "слепая доля печатается всегда, в том числе нулём"
    section = blind[0]["blind_spend"]
    assert section["cost_total"] == 2000.0
    assert section["blind_share"] == 0.5
    assert [s["campaign_id"] for s in section["sample"]] == ["999"]


def test_unavailable_settings_do_not_break_the_writing_tact(monkeypatch, capsys):
    # Отчётный слой не вправе уронить запись: витрина настроек недоступна —
    # видна причина, кабинеты обработаны.
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 100.0},
    )

    def _boom():
        raise RuntimeError("витрина недоступна")

    monkeypatch.setattr(agent_e1.agent_db, "load_campaign_settings_raw", _boom)

    assert agent_e1.main() == 0

    reports = _reports(capsys)
    blind = [r for r in reports if r.get("verdict") == "BLIND_SPEND"]
    assert "витрина недоступна" in blind[0]["blind_spend"]["unavailable"]
    assert [r for r in reports if "account" in r], "кабинет обработан"


# =========================================================================
# Дефект 3: кулдаун обязан включать неоткатанный пробой
# =========================================================================


def test_cooldown_is_asked_about_harmful_segments_not_only_rolled_back(monkeypatch,
                                                                       capsys):
    # Планировщик спрашивает журнал о ВРЕДНЫХ сегментах: и откатанных, и тех,
    # чей откат не удался. Отдельного вопроса «что откатано» больше нет.
    asked = []
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
    )
    monkeypatch.setattr(
        agent_e1.writer_db, "harmful_segments",
        lambda days, account=None: asked.append((days, account)) or {
            ("111", "DESKTOP_ADJUSTMENT", "DESKTOP"): "2026-08-19T10:00:00+00:00"},
    )

    assert agent_e1.main() == 0

    assert asked == [(agent_e1.COOLDOWN_AFTER_ROLLBACK_DAYS, "acc-1")]
    report = _reports(capsys)[0]
    assert report["blocked_by_cooldown"]["count"] == 1
    assert _MultiCabinetClient.instances[0].sent == []


# =========================================================================
# Дефект (Critical): «песочница + запись» у прямого применения — как у сторожа
# =========================================================================


def test_refusal_blocks_only_sandbox_with_apply():
    # То же правило, что у сторожа (agent_e1_watchdog.refusal): запрещена
    # ровно комбинация sandbox=True И dry_run=False. Остальные три — рабочие
    # режимы прогона и обязаны оставаться разрешёнными.
    assert agent_e1.refusal(sandbox=True, dry_run=False) == agent_e1.SANDBOX_APPLY_REFUSAL
    assert agent_e1.refusal(sandbox=True, dry_run=True) is None
    assert agent_e1.refusal(sandbox=False, dry_run=True) is None
    assert agent_e1.refusal(sandbox=False, dry_run=False) is None


def test_main_refuses_sandbox_apply_before_touching_db(monkeypatch, capsys):
    # --apply без --prod: sandbox=True, dry_run=False — запрещённая
    # комбинация. Раньше main() её не отсекал вовсе и уходил писать в
    # БОЕВОЙ журнал строки о песочнице. Отказ обязан случиться ДО первого
    # обращения к БД — оба подменённых вызова роняют тест, если их всё же
    # позвали.
    monkeypatch.setattr(sys, "argv", ["agent_e1", "--apply"])
    monkeypatch.setattr(
        agent_e1.writer_db, "ensure_writer_tables",
        lambda: pytest.fail("ensure_writer_tables не должен вызываться при отказе"),
    )
    monkeypatch.setattr(
        agent_e1, "_clients",
        lambda: pytest.fail("_clients не должен вызываться при отказе"),
    )

    exit_code = agent_e1.main()

    assert exit_code == 2
    report = _reports(capsys)[0]
    assert report["verdict"] == "REFUSED"
    assert report["sandbox"] is True
    assert report["dry_run"] is False
    assert report["reason"] == agent_e1.SANDBOX_APPLY_REFUSAL


def test_main_prod_apply_is_not_refused(monkeypatch, capsys):
    # Соседний режим (--prod --apply) не должен зацепиться отказом —
    # это ровно тот режим, ради которого прогон вообще существует.
    monkeypatch.setattr(agent_e1, "_clients", lambda: [])
    monkeypatch.setattr(agent_e1.writer_db, "ensure_writer_tables", lambda: None)
    monkeypatch.setattr(sys, "argv", ["agent_e1", "--prod", "--apply"])

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]
    assert report["verdict"] == "NOTHING_TO_DO"


class _NoDBTouch:
    """Двойник журнала: падает на любом обращении, чтобы доказать, что
    отказ apply_actions случается ДО первой записи в БД."""

    def __getattr__(self, name):
        def _fail(*a, **k):
            pytest.fail(f"db_module.{name} не должен вызываться при отказе")
        return _fail


class _StubMutateClient:

    def is_write_allowed(self):
        return not self.dry_run

    def __init__(self, sandbox, dry_run):
        self.sandbox = sandbox
        self.dry_run = dry_run

    def mutate(self, service, method, params):
        return {"dry_run": True} if self.dry_run else {}

    def mutate_batch(self, service, method, collection, items):
        # Батч — ОДИН запрос (client.WriteClient.mutate_batch). Двойник
        # обязан повторять форму настоящего транспорта: иначе прогон в тесте
        # ходит одним путём, а в бою другим.
        return self.mutate(service, method, {collection: list(items)})


def test_apply_actions_refuses_sandbox_write_client_without_touching_db():
    # Второй рубеж инварианта «песочный клиент журнала не касается»: даже
    # если вызывающий код (живой тест, разовый скрипт) соберёт запрещённый
    # клиент в обход main(), apply_actions обязан отказаться сам, а не
    # начать писать в боевой журнал строки о песочнице.
    client = _StubMutateClient(sandbox=True, dry_run=False)

    with pytest.raises(SandboxApplyRefusal):
        apply_actions(client, [{"idempotency_key": "x"}], _NoDBTouch())


def test_apply_actions_allows_sandbox_rehearsal_and_prod_apply():
    # Оба легитимных режима не задеты новым рубежом: песочница-репетиция
    # (умолчание) и боевая запись (sandbox=False) обязаны работать как
    # раньше — это и есть «боевой путь не меняется ни на шаг».
    class _FakeDB:
        def find_action_by_key(self, key):
            return None

        def insert_action(self, action):
            return action["idempotency_key"]

        def mark_action(self, action_id, status, response):
            return True

        def mark_sent(self, action_id):
            pass

    action = {"idempotency_key": "x", "action_kind": "bidmodifier.set",
              "payload": {"Id": 1, "BidModifier": 10}}

    sandbox_rehearsal = apply_actions(
        _StubMutateClient(sandbox=True, dry_run=True), [action], _FakeDB())
    assert sandbox_rehearsal["dry_run"] == 1

    prod_apply = apply_actions(
        _StubMutateClient(sandbox=False, dry_run=False), [action], _FakeDB())
    assert prod_apply["applied"] == 1


# =========================================================================
# Дефект И1: нечем ограничить первый боевой прогон
#
# Песочница боевым логинам недоступна (выяснено пробой), поэтому безопасность
# первого применения держат три вещи вместе: репетиция без записи, проба по
# несуществующим идентификаторам и ПЕРВОЕ ПРИМЕНЕНИЕ НА ОДНОЙ КАМПАНИИ.
# Третьего в коде не было вовсе: вычисленные настройки лежат на уровне
# кабинета, то есть один набор ключей раскатывается на все его кампании, и
# сколько кампаний тронет прогон, решали лимит действий и порядок сортировки.
# =========================================================================


def test_first_prod_run_can_be_limited_to_one_campaign(monkeypatch, capsys):
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111, 222, 333]},
        daily_cost={"111": 10.0, "222": 10.0, "333": 10.0},
        argv=["--max-campaigns=1"],
    )

    assert agent_e1.main() == 0

    client = _MultiCabinetClient.instances[0]
    assert len(client.sent) == 1, "тронута ровно одна кампания, а не сколько влезло в лимит"
    report = _reports(capsys)[0]
    assert report["campaigns_touched"] == 1
    assert report["campaign_scope"]["max_campaigns"] == 1
    assert report["campaign_scope"]["campaigns_left_in_run"] == 0


def test_campaign_limit_cuts_the_campaign_list_not_the_actions(monkeypatch, capsys):
    # Ограничение стоит ТАМ, ГДЕ СТРОИТСЯ СПИСОК КАМПАНИЙ. Отсечение действий
    # в конце означало бы, что кабинет прочитан целиком, план построен по всем
    # кампаниям и от порядка сортировки по-прежнему зависит, кто доживёт до
    # отправки. Проверяем по факту ЧТЕНИЯ кабинета, а не по числу отправок.
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111, 222, 333]},
        daily_cost={"111": 10.0, "222": 10.0, "333": 10.0},
        argv=["--max-campaigns=1"],
    )

    assert agent_e1.main() == 0

    client = _MultiCabinetClient.instances[0]
    assert client.read == ["111"], "остальные кампании не должны даже читаться"


def test_named_campaigns_are_the_only_ones_touched(monkeypatch, capsys):
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111, 222, 333]},
        daily_cost={"111": 10.0, "222": 10.0, "333": 10.0},
        argv=["--campaigns=222"],
    )

    assert agent_e1.main() == 0

    client = _MultiCabinetClient.instances[0]
    assert client.read == ["222"]
    sent_campaign = client.sent[0][2]["BidModifiers"][0]["CampaignId"]
    assert sent_campaign == 222
    report = _reports(capsys)[0]
    assert report["campaign_scope"]["only"] == ["222"]
    assert report["campaigns_touched"] == 1


def test_campaign_limit_is_shared_across_cabinets(monkeypatch, capsys):
    # Тот же довод, что у лимита действий: «первое применение на одной
    # кампании» при потолке НА КАБИНЕТ означало бы четыре кампании на четырёх
    # кабинетах.
    settings = [_setting("bid_modifier:device", "DESKTOP", 30)]
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": settings, "acc-2": settings},
        campaigns_by_login={"acc-1": [111, 112], "acc-2": [221, 222]},
        daily_cost={"111": 10.0, "112": 10.0, "221": 10.0, "222": 10.0},
        argv=["--max-campaigns=1"],
    )

    assert agent_e1.main() == 0

    touched = sum(len(c.read) for c in _MultiCabinetClient.instances)
    assert touched == 1, "потолок кампаний обязан считаться на все кабинеты сразу"
    reports = {r["account"]: r for r in _reports(capsys) if "account" in r}
    assert reports["acc-2"]["campaigns_touched"] == 0


def test_campaigns_touched_counts_campaigns_not_actions(monkeypatch, capsys):
    # Отдельное поле нужно именно потому, что ни одно соседнее число на этот
    # вопрос не отвечает: три действия по одной кампании — это одна тронутая
    # кампания, а prepared.count покажет три.
    _many_actions_per_campaign(monkeypatch)
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [
            _setting("bid_modifier:device", "DESKTOP", 30),
            _setting("bid_modifier:device", "MOBILE", 20),
            _setting("bid_modifier:gender", "GENDER_MALE", 15),
        ]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
    )

    assert agent_e1.main() == 0

    report = _reports(capsys)[0]
    assert report["prepared"]["count"] == 3
    assert report["campaigns_touched"] == 1


def test_scope_is_visible_in_the_report_even_when_off(monkeypatch, capsys):
    # «Ограничение не сработало» и «ограничения не было» обязаны различаться
    # при чтении отчёта первого боевого прогона.
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111, 222]},
        daily_cost={"111": 10.0, "222": 10.0},
    )

    assert agent_e1.main() == 0

    scope = _reports(capsys)[0]["campaign_scope"]
    assert scope["enabled"] is False
    assert scope["only"] is None and scope["max_campaigns"] is None


def test_unparsable_campaign_limit_refuses_the_run(monkeypatch, capsys):
    # Молчаливое игнорирование неразобранного аргумента здесь недопустимо:
    # оператор, набравший --max-campaigns=one перед ПЕРВЫМ боевым прогоном,
    # получил бы прогон без ограничения вообще, будучи уверенным в обратном.
    monkeypatch.setattr(sys, "argv", ["agent_e1", "--prod", "--apply",
                                      "--max-campaigns=one"])
    monkeypatch.setattr(agent_e1.writer_db, "ensure_writer_tables",
                        lambda: (_ for _ in ()).throw(AssertionError("БД тронута")))

    assert agent_e1.main() == 2

    report = _reports(capsys)[0]
    assert report["verdict"] == "REFUSED"
    assert "--max-campaigns" in report["reason"]


def test_empty_campaign_list_is_not_read_as_all_campaigns(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["agent_e1", "--prod", "--apply", "--campaigns="])
    monkeypatch.setattr(agent_e1.writer_db, "ensure_writer_tables",
                        lambda: (_ for _ in ()).throw(AssertionError("БД тронута")))

    assert agent_e1.main() == 2
    assert _reports(capsys)[0]["verdict"] == "REFUSED"


def test_campaign_scope_selection_is_deterministic():
    # Повторный прогон с тем же ограничением обязан взять ТЕ ЖЕ кампании:
    # иначе «применили на одной кампании и посмотрели» превращается в
    # «применили на разных кампаниях по одному разу».
    first = agent_e1.CampaignScope(max_campaigns=2).select(["111", "222", "333"])
    second = agent_e1.CampaignScope(max_campaigns=2).select(["111", "222", "333"])
    assert first == second == ["111", "222"]


def test_campaign_scope_without_limits_changes_nothing():
    scope = agent_e1.CampaignScope()
    assert scope.enabled is False
    assert scope.select(["111", "222"]) == ["111", "222"]


# =========================================================================
# Дефект И3: отклонённое и неудавшееся переотправляются вечно
#
# Статус 'rejected' сознательно не финальный — действие обязано быть
# переприменимо. Но счётчика попыток у этого пути не было, в отличие от
# отката. Детерминированный отказ (неподдерживаемый ключ, неподходящий тип
# кампании, «объект уже существует») переотправлялся бы каждый прогон вечно,
# каждый раз занимая слот лимита и часть риск-бюджета.
# =========================================================================


def test_segment_out_of_attempts_is_not_sent_again(monkeypatch, capsys):
    exhausted = {("111", "DESKTOP_ADJUSTMENT", "DESKTOP"):
                 {"attempts": 3, "last_attempt_at": "2026-08-19"}}
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
        exhausted=exhausted,
    )

    assert agent_e1.main() == 0

    assert [c for inst in _MultiCabinetClient.instances for c in inst.sent] == []
    report = _reports(capsys)[0]
    assert report["blocked_by_attempts"]["count"] == 1
    assert report["blocked_by_attempts"]["max_attempts"] == \
        agent_e1.writer_db.MAX_APPLY_ATTEMPTS
    assert report["blocked_by_attempts"]["segments"] == \
        ["111:DESKTOP_ADJUSTMENT:DESKTOP"]
    assert report["blocked_by_attempts"]["reason"]


def test_exhausted_segment_is_cut_before_the_lane_selection(monkeypatch, capsys):
    # Главная цена вечной переотправки — не сам запрос, а МЕСТО В ПОЛОСЕ:
    # порядок обхода детерминирован, и накопившиеся отказные сегменты стабильно
    # занимали начало списка. Отсев обязан стоять до отбора, как кулдаун и
    # закрытые ключи.
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [
            _setting("bid_modifier:device", "DESKTOP", 30),
            _setting("bid_modifier:device", "MOBILE", 20),
        ]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
        exhausted={("111", "DESKTOP_ADJUSTMENT", "DESKTOP"): {"attempts": 3}},
    )

    assert agent_e1.main() == 0

    sent = [c for inst in _MultiCabinetClient.instances for c in inst.sent]
    assert len(sent) == 1, "место в полосе обязано достаться живому сегменту"
    assert "MobileAdjustment" in sent[0][2]["BidModifiers"][0]


def test_attempts_are_counted_by_segment_not_by_idempotency_key(monkeypatch, capsys):
    # Тот же довод, что у кулдауна: в ключ идемпотентности зашит ПРОЦЕНТ, а он
    # пересчитывается на каждом прогоне по скользящему окну. Считай мы попытки
    # по ключу — у каждой попытки был бы свой ключ, счётчик показывал бы
    # единицу вечно, и потолок не наступал бы никогда.
    exhausted = {("111", "DESKTOP_ADJUSTMENT", "DESKTOP"): {"attempts": 3}}
    _patch_run(
        monkeypatch,
        # Процент ДРУГОЙ: ключ идемпотентности у этого действия новый.
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 29)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
        exhausted=exhausted,
    )

    assert agent_e1.main() == 0

    assert [c for inst in _MultiCabinetClient.instances for c in inst.sent] == []
    assert _reports(capsys)[0]["blocked_by_attempts"]["count"] == 1


def test_exhausted_segment_does_not_block_other_segments(monkeypatch, capsys):
    # Обратная половина: потолок попыток адресован сегменту, а не кампании.
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [
            _setting("bid_modifier:device", "DESKTOP", 30),
            _setting("bid_modifier:device", "MOBILE", 20),
        ]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
        exhausted={("111", "DESKTOP_ADJUSTMENT", "DESKTOP"): {"attempts": 3}},
    )

    assert agent_e1.main() == 0

    sent = [c for inst in _MultiCabinetClient.instances for c in inst.sent]
    assert len(sent) == 1
    assert "MobileAdjustment" in sent[0][2]["BidModifiers"][0]


def test_split_by_attempts_leaves_untouched_segments_alone():
    action = {"object_id": "111", "direct_type": "MOBILE_ADJUSTMENT", "key": "MOBILE"}
    allowed, blocked = agent_e1.split_by_attempts([action], {})
    assert allowed == [action] and blocked == []


# =========================================================================
# Мелкое: порядок пометки зависших строк и чтения закрытых ключей
# =========================================================================


def test_stuck_row_does_not_eat_the_lane_on_the_run_that_finds_it(monkeypatch, capsys):
    # Список закрытых ключей читался ДО пометки зависших строк. На прогоне
    # ОБНАРУЖЕНИЯ зависшая строка ещё стояла в 'planned' — final_status_keys
    # её не видел, действие доходило до отбора полосой, занимало её место и
    # помечало свой объект оплаченным риск-бюджетом.
    #
    # Журнал здесь фейковый: пометка переводит ключ в закрытые, и правильный
    # порядок обязан этот перевод УВИДЕТЬ.
    stuck_key = _bidmod_key("111", "DESKTOP_ADJUSTMENT", "DESKTOP", 30)
    closed = set()

    def _mark_stale(*a, **k):
        closed.add(stuck_key)
        return [{"action_id": "act-1", "object_id": "111",
                 "action_kind": "bidmodifier.add", "created_at": "2026-08-19",
                 "idempotency_key": stuck_key}]

    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [
            _setting("bid_modifier:device", "DESKTOP", 30),
            _setting("bid_modifier:device", "MOBILE", 20),
        ]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 10.0},
        prod_apply=True,
    )
    monkeypatch.setattr(agent_e1.writer_db, "mark_stale_planned", _mark_stale)
    monkeypatch.setattr(agent_e1.writer_db, "final_status_keys",
                        lambda keys: {k for k in keys if k in closed})

    assert agent_e1.main() == 0

    sent = [c for inst in _MultiCabinetClient.instances for c in inst.sent]
    assert len(sent) == 1, "слот лимита не должен достаться зависшей строке"
    assert "MobileAdjustment" in sent[0][2]["BidModifiers"][0]
    report = _reports(capsys)[0]
    assert report["skipped_already_final"]["count"] == 1
    assert report["stale_planned"]["count"] == 1


# --------------------------------- почасовое расписание доезжает до кабинета


class _ScheduleClient:
    """Кампания 111 с ровным расписанием: профиль обязан дать одно действие."""

    def is_write_allowed(self):
        return True

    instances = []

    def __init__(self, login, sandbox=True, dry_run=True):
        self.login = login
        self.units_left = None
        self.sent = []
        self.reads = []
        _ScheduleClient.instances.append(self)

    def get(self, service, params):
        self.reads.append((service, params))
        if service == "campaigns":
            # Читается двумя разными запросами: список кампаний и расписание.
            if "TimeTargeting" in (params.get("FieldNames") or []):
                return {"Campaigns": [{"Id": 111, "TimeTargeting": {
                    "Schedule": {"Items": ["1," + ",".join(["100"] * 24)]},
                    "ConsiderWorkingWeekends": "YES"}}]}
            return {"Campaigns": [{"Id": 111}]}
        if service == "bidmodifiers":
            return {"BidModifiers": []}
        raise AssertionError(f"неожиданный сервис: {service}")

    def mutate(self, service, method, params):
        self.sent.append((service, method, params))
        return {"dry_run": True}

    def mutate_batch(self, service, method, collection, items):
        # Батч — ОДИН запрос (client.WriteClient.mutate_batch). Двойник
        # обязан повторять форму настоящего транспорта: иначе прогон в тесте
        # ходит одним путём, а в бою другим.
        return self.mutate(service, method, {collection: list(items)})


def _patch_schedule_run(monkeypatch, settings, learning_resets=None):
    _ScheduleClient.instances = []
    monkeypatch.setattr(agent_e1, "_clients", lambda: [{"login": "acc-1"}])
    monkeypatch.setattr(agent_e1.writer_db, "ensure_writer_tables", lambda: None)
    monkeypatch.setattr(agent_e1.agent_db, "load_latest_computed_settings",
                        lambda *_: settings)
    monkeypatch.setattr(agent_e1.agent_db, "load_holdout_ids", lambda: [])
    monkeypatch.setattr(agent_e1.agent_db, "load_daily_cost_by_campaign",
                        lambda *_: {"111": 500.0})
    monkeypatch.setattr(agent_e1.agent_db, "load_baseline_cpa", lambda *_: {"111": 1000.0})
    monkeypatch.setattr(agent_e1.agent_db, "crm_maturity_date",
                        lambda: date.today() - timedelta(days=3))
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit", lambda *_: 50_000.0)
    monkeypatch.setattr(agent_e1.writer_db, "spent_risk", lambda *_: 0.0)
    monkeypatch.setattr(agent_e1.writer_db, "charged_risk_by_object", lambda *_: {})
    monkeypatch.setattr(agent_e1.writer_db, "mark_stale_planned", lambda *a, **k: [])
    monkeypatch.setattr(agent_e1.writer_db, "find_action_by_key", lambda *_: None)
    monkeypatch.setattr(agent_e1.writer_db, "insert_action", lambda action: 1)
    monkeypatch.setattr(agent_e1.writer_db, "mark_action", lambda *a, **k: True)
    monkeypatch.setattr(agent_e1.writer_db, "mark_unknown_outcome", lambda *a, **k: True)
    monkeypatch.setattr(agent_e1, "WriteClient", _ScheduleClient)
    _patch_infra(monkeypatch, learning_resets=learning_resets)


def test_schedule_reaches_the_cabinet(monkeypatch, capsys):
    """Сквозная половина: посчитанный профиль обязан дойти до запроса.

    Проверять один plan_schedule недостаточно — ровно так дефект и выживал
    бы: ветка написана, протестирована и никем не вызывается. Здесь
    фиксируется сам запрос к API.
    """
    _patch_schedule_run(monkeypatch, [_setting("schedule:hour", "3", -40.0)])

    assert agent_e1.main() == 0
    capsys.readouterr()

    sent = [s for c in _ScheduleClient.instances for s in c.sent]
    campaigns_calls = [s for s in sent if s[0] == "campaigns"]
    assert campaigns_calls, "расписание не ушло в кабинет"

    service, method, params = campaigns_calls[0]
    assert (service, method) == ("campaigns", "update")
    targeting = params["Campaigns"][0]["TimeTargeting"]
    # Ночной час опущен, шкала 100-базная, кратность десяти соблюдена.
    assert targeting["Schedule"]["Items"][0].split(",")[1 + 3] == "60"
    # Соседнее поле кабинета перенесено, а не сброшено.
    assert targeting["ConsiderWorkingWeekends"] == "YES"


def test_schedule_is_reported_even_when_it_changes_nothing(monkeypatch, capsys):
    # «Профиля нет» и «профиль есть, но в кабинете уже стоит» обязаны
    # различаться: иначе оба выглядят как молчание прогона.
    _patch_schedule_run(monkeypatch, [_setting("schedule:hour", "3", 1.0)])

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    assert report["schedule"]["significant_hours"] == 0
    assert "пороги значимости" in report["schedule"]["reason"]


def test_schedule_alone_is_enough_to_work(monkeypatch, capsys):
    """Кабинет без значимых корректировок, но со значимым профилем.

    Ранний выход «нечего делать» стоял ДО расчёта расписания и молча уносил
    посчитанный профиль с собой: у расписания свой механизм, и отсутствие
    корректировок ничего о нём не говорит.
    """
    _patch_schedule_run(monkeypatch, [_setting("schedule:hour", "3", -40.0)])

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    # Полный отчёт (а не короткий с verdict) — прогон нашёл, что делать.
    assert "verdict" not in report
    assert report["schedule"]["campaigns_differing"] == 1
    assert report["desired"] == 0, "корректировок нет, работа только по расписанию"


def test_stale_settings_block_the_schedule_too(monkeypatch, capsys):
    # Профиль посчитан по тем же данным, что и корректировки: «данные
    # протухли» не перестаёт быть правдой оттого, что механизм другой.
    old = (date.today() - timedelta(days=30)).isoformat()
    _patch_schedule_run(monkeypatch, [_setting("schedule:hour", "3", -40.0, calc_date=old)])

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    assert report["verdict"] == "STALE_COMPUTED_SETTINGS"
    sent = [s for c in _ScheduleClient.instances for s in c.sent]
    assert sent == [], "устаревший профиль ушёл в кабинет"


def test_schedule_is_not_read_when_there_is_nothing_to_apply(monkeypatch, capsys):
    # Чтение расписания — лишний запрос к API на каждую кампанию кабинета.
    # Профиля нет — запроса быть не должно.
    _patch_schedule_run(monkeypatch, [_setting("bid_modifier:device", "MOBILE", 30.0)])

    agent_e1.main()
    capsys.readouterr()

    reads = [r for c in _ScheduleClient.instances for r in c.reads]
    targeting_reads = [r for r in reads
                       if r[0] == "campaigns" and "TimeTargeting" in (r[1].get("FieldNames") or [])]
    assert targeting_reads == []


# --------------------------- гейт данных перед пишущим прогоном


def test_data_gate_red_blocks_apply_run_before_any_journal_io(monkeypatch, capsys):
    # e1 — пишущий прогон: красный гейт данных запрещает применение ДО
    # первого обращения к журналу и загрузок планирования. Репетиция гейтом
    # не блокируется (смотреть на плохие данные можно, писать по ним — нет),
    # это отдельный путь.
    monkeypatch.setattr(agent_e1, "data_gate",
                        lambda today: {"status": "RED", "reason": "тест",
                                       "checks": []})

    def _boom(*a, **k):
        raise AssertionError("при красном гейте журнал и БД трогать нельзя")

    monkeypatch.setattr(agent_e1.agent_db, "load_holdout_ids", _boom)
    monkeypatch.setattr(agent_e1.writer_db, "purge_dry_run_actions", _boom)

    rc = agent_e1._run_all([{"login": "acc"}], sandbox=False, dry_run=False,
                           today="2026-08-24")

    assert rc == 1
    assert "DATA_GATE_RED" in capsys.readouterr().out


# ------------------- белый список аргументов прогона


def test_unknown_flag_is_refused_not_ignored():
    # Опечатка в ограничителе тише всего: «--max-campaign=5» (без s) прежде
    # просто не распознавался, и оператор получал прогон БЕЗ ограничения,
    # будучи уверенным в обратном. Любой неизвестный флаг — ошибка.
    with pytest.raises(ValueError):
        agent_e1.parse_campaign_scope(["--max-campaign=5"])
    with pytest.raises(ValueError):
        agent_e1.parse_campaign_scope(["--campaign=111"])
    with pytest.raises(ValueError):
        agent_e1.parse_campaign_scope(["--dry-run"])


def test_known_flags_are_accepted():
    scope = agent_e1.parse_campaign_scope(
        ["--prod", "--apply", "--campaigns=111,222", "--max-campaigns=3"])
    assert scope.only == {"111", "222"}
    assert scope.max_campaigns == 3


# ------------------- Э3.5: рычаг целевого CPA в прогоне


def test_tcpa_action_reaches_the_planner(monkeypatch):
    # Сквозная проверка вайринга: строки Э3.5 из computed → действие tcpa.set
    # с previous_state из СВЕЖЕГО чтения кабинета.
    from sync.agent.writer import tcpa as tcpa_writer
    computed = {"111": [
        {"setting_kind": "tcpa_target", "setting_key": "target",
         "value": 1300.0, "raw_value": 1000.0, "support_n": 200,
         "rel_error": 0.05},
        {"setting_kind": "tcpa_target", "setting_key": "roi_vs_target",
         "value": 1.4, "raw_value": 1500.0, "support_n": 200, "rel_error": 0.05},
    ]}
    plan = tcpa_writer.plan_tcpa_moves(computed)
    assert plan["desired"]["111"]["target"] == 1300.0

    state = {"111": {"strategy": {"Search": {
        "BiddingStrategyType": "AVERAGE_CPA",
        "AverageCpa": {"AverageCpa": 1000 * 1_000_000,
                       "WeeklySpendLimit": 350_000 * 1_000_000}}},
        "package_id": None, "campaign_type": "TEXT_CAMPAIGN"}}
    actions, refused = tcpa_writer.diff_tcpa(plan["desired"], state)
    assert not refused
    assert actions[0]["action_kind"] == tcpa_writer.TCPA_KIND
    from sync.agent.writer.guardrails import check_action
    ok, reason = check_action(actions[0])
    assert ok, reason


def test_budget_and_tcpa_do_not_touch_the_same_campaign_in_one_tick():
    # Две денежные ручки разом делают исход неразличимым: красная линия не
    # скажет, какая из них навредила. Кампания, которой этим тактом двигают
    # бюджет, целью не трогается.
    import inspect
    source = inspect.getsource(agent_e1)
    assert "cid not in budget_desired" in source


# ------------------- кулдаун обучения стратегий (writer/learning.py)


def test_learning_cooldown_blocks_action_that_would_restart_learning(monkeypatch, capsys):
    # Сквозной: обучение стратегии кампании перезапускали три дня назад
    # (журнал), и расписание — действие класса unknown, то есть заведомо
    # меняющее объём показов, — в этот такт до кабинета не доходит.
    _patch_schedule_run(
        monkeypatch, [_setting("schedule:hour", "3", -40.0)],
        learning_resets={"111": date.today() - timedelta(days=3)})

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    sent = [s for c in _ScheduleClient.instances for s in c.sent]
    assert [s for s in sent if s[0] == "campaigns"] == [], \
        "сбрасывающее обучение действие ушло в кабинет внутри кулдауна"
    assert report["learning"]["blocked_by_cooldown"] == 1
    assert report["learning"]["cooldown_days"] == \
        agent_e1.LEARNING_COOLDOWN_DAYS
    # Дата последнего сброса — в отчёте: без неё запрет нечем проверить.
    assert report["learning"]["blocked_objects"] == [
        "111:" + (date.today() - timedelta(days=3)).isoformat()]


def test_learning_cooldown_lets_the_action_through_when_history_is_old(monkeypatch, capsys):
    # Обратная половина того же: сброс был давно — действие уходит в кабинет,
    # а отчёт называет его классом (unknown у расписания — не «безопасно»).
    _patch_schedule_run(
        monkeypatch, [_setting("schedule:hour", "3", -40.0)],
        learning_resets={"111": date.today() - timedelta(
            days=agent_e1.LEARNING_COOLDOWN_DAYS + 1)})

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    sent = [s for c in _ScheduleClient.instances for s in c.sent]
    assert [s for s in sent if s[0] == "campaigns"], "действие не дошло до кабинета"
    assert report["learning"]["blocked_by_cooldown"] == 0
    assert report["learning"]["unknown_applied"] == 1


def test_learning_class_travels_to_the_journal_with_the_action(monkeypatch, capsys):
    # Класс влияния на обучение обязан лечь в СТРОКУ ЖУРНАЛА: по нему потом
    # считается кулдаун (writer_db.last_learning_reset). Без этого поля
    # история сбросов осталась бы пустой навсегда, и гейт молчал бы всегда.
    rows = _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 100.0},
        prod_apply=True,
    )

    assert agent_e1.main() == 0
    capsys.readouterr()

    assert rows, "действие не дошло до журнала"
    # Корректировка ставок обучение не сбивает — и это записано словом,
    # а не пропуском: NULL означал бы «класс не приписан».
    assert rows[0]["learning_impact"] == "safe"


def test_learning_cooldown_reuses_the_money_knob_window(monkeypatch):
    # Гейт обучения не заводит собственный срок: и он, и кулдаун денежных
    # ручек (budget.apply_cooldown, применяется к бюджету и цели CPA) считают
    # по одному числу дней. Две копии разъехались бы при первой правке.
    from sync.agent.writer import budget as budget_writer

    assert agent_e1.LEARNING_COOLDOWN_DAYS == budget_writer.BUDGET_COOLDOWN_DAYS


def test_red_line_carries_baseline_volume():
    """Красная линия несёт не только цену базы, но и её темп.

    Цена без объёма не даёт сравнить ожидание с фактом: сторож знает лиды за
    окно наблюдения, а сколько их было до изменения — неизвестно. Темп, а не
    сумма: окна базы и наблюдения разной длины, суммы несопоставимы.
    """
    action = {"action_kind": "budget.set", "object_id": "111",
              "object_level": "campaign"}

    red_line = agent_e1.build_red_line(
        action, {"111": 1000.0}, None, ("2026-07-01", "2026-07-28"), None,
        {"111": {"leads": 56.0, "days": 28, "leads_per_day": 2.0}})

    assert red_line["baseline_leads_per_day"] == 2.0


def test_red_line_without_volume_has_no_rate_key():
    # Кампании нет в справочнике объёма — темпа в линии нет вовсе: ноль
    # сторож прочитал бы как «база не давала лидов», и наблюдаемая дельта
    # оказалась бы равна всему объёму окна.
    red_line = agent_e1.build_red_line(
        {"object_id": "111"}, {"111": 1000.0}, None, None, None, {})

    assert "baseline_leads_per_day" not in red_line


# ------------------- баланс такта: сокращение без адресата роста (agent/balance.py)


class _SwitchClient(_MultiCabinetClient):
    """Кабинет, который умеет отвечать на чтение State (writer/switch.py)."""

    def get(self, service, params):
        if service == "campaigns" and "State" in (params.get("FieldNames") or []):
            ids = (params.get("SelectionCriteria") or {}).get("Ids") or []
            return {"Campaigns": [{"Id": i, "State": "ON"} for i in ids]}
        return super().get(service, params)


def _switch_row(roi_share=0.3):
    return {"setting_kind": "campaign_switch", "setting_key": "off",
            "value": roi_share, "raw_value": roi_share, "rel_error": 0.05,
            "support_n": 300, "calc_date": date.today().isoformat()}


def _patch_switch_run(monkeypatch, daily_cost):
    _patch_run(monkeypatch,
               computed_by_login={"acc-1": []},
               campaigns_by_login={"acc-1": [111]},
               daily_cost=daily_cost)
    monkeypatch.setattr(agent_e1, "WriteClient", _SwitchClient)
    monkeypatch.setattr(agent_e1.agent_db, "load_latest_campaign_computed",
                        lambda ids: {"111": [_switch_row()]})


def test_suspend_without_growth_address_does_not_reach_the_cabinet(monkeypatch, capsys):
    # Сквозной: единственное действие такта — выключение кампании. Оно
    # освобождает её расход, назначать его некому, и такт целиком сводится к
    # «стало эффективнее и меньше». Гейт снимает его до отправки.
    _patch_switch_run(monkeypatch, daily_cost={"111": 1000.0})

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    sent = [s for c in _MultiCabinetClient.instances for s in c.sent]
    assert sent == [], "сокращение без адресата роста ушло в кабинет"
    assert report["balance"]["blocked_without_address"] == 1
    assert report["balance"]["gate"]["freed_rub"] == 28_000.0      # 1000 ₽/день × 28
    assert report["balance"]["gate"]["unassigned_rub"] == 28_000.0
    assert report["balance"]["gate"]["shrinking"] is True
    # Применённый баланс — по тому, что реально уехало: пусто, а не «план».
    assert report["balance"]["applied"]["freed_rub"] == 0.0


def test_balance_gate_leaves_a_tact_that_does_not_cut_alone(monkeypatch, capsys):
    # Обратная половина: корректировка ставок расход не освобождает, такт не
    # сжимающий — гейт не имеет права его трогать.
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": [_setting("bid_modifier:device", "DESKTOP", 30)]},
        campaigns_by_login={"acc-1": [111]},
        daily_cost={"111": 100.0},
    )

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    sent = [s for c in _MultiCabinetClient.instances for s in c.sent]
    assert len(sent) == 1, "действие, которое ничего не режет, не дошло до кабинета"
    assert report["balance"]["gate"]["shrinking"] is False
    assert report["balance"]["blocked_without_address"] == 0


def test_balance_gate_stands_after_the_other_gates_and_before_the_lanes():
    # Место гейта — не деталь. Все гейты стоят ПОСЛЕ солвера, и доливка,
    # запертая кулдауном или потолком попыток, обязана быть уже вычтенной к
    # моменту расчёта баланса: иначе её рубли считались бы назначенными.
    # А до отбора полосами — чтобы снятое здесь не занимало место в полосе.
    import inspect

    source = inspect.getsource(agent_e1.run_account)
    assert (source.index("split_by_final_keys")
            < source.index("require_growth_address")
            < source.index("lanes.select("))


def test_rails_order_uses_lanes():
    """Уберите этот тест — и полосы вернутся на место лимита прогона, а вместе
    с ним вернётся списание риска за действия, которые в кабинет не поедут.

    Порядок рельс: конфликты → красная линия → полосы → бюджет недели и дня.

    Красная линия стоит ПЕРЕД полосами (в плане беты её место было после), и
    это осознанное отклонение: отбор полосой не просто раздаёт места, он
    СПИСЫВАЕТ риск-бюджет полосы. Действие без красной линии не применяется
    никогда, и списать за него полосу — тот же дефект, от которого рельсы
    защищает собственный комментарий run_account: объект помечен оплаченным,
    а изменение по нему в кабинет не ушло.

    Бюджет недели и дня — последним: он общий на все полосы и на все кабинеты
    прогона, и считать его до отбора значило бы платить за отказанное.
    """
    import inspect

    source = inspect.getsource(agent_e1.run_account)
    assert (source.index("conflicts.resolve(")
            < source.index("build_red_line(")
            < source.index("lanes.select(")
            < source.index("fit_week_budget("))


def _transfer(key, campaign, rub_delta, days=14):
    """Бюджетное действие с обещанием знака: донор (−) или получатель (+).

    Знак берёт risk._moved_rub из обещания рычага — по нему и определяется
    пара, имена кампаний в этом не участвуют.
    """
    return {"idempotency_key": key, "action_kind": "budget.set",
            "object_level": "campaign", "object_id": campaign,
            "marginal_roi": 2.0 if rub_delta > 0 else 1.0,
            "payload": {"CampaignId": int(campaign),
                        "expected_rub_delta": rub_delta,
                        "expected_leads_delta": 0.0,
                        "expectation_basis": "перенос",
                        "expectation_days": days}}


def test_weekly_rail_does_not_split_a_transfer_pair():
    """Уберите этот тест — и скидку за перенос снова выдадут без компенсации.

    lanes.select держит пару целиком внутри полосы, но после него стоит ещё
    одна рельса — остаток недели и дня. Она берёт действия по порядку, пока
    хватает денег, и способна взять получателя, а донора отложить: скидка за
    встречное движение денег выдана, компенсации не будет, и кабинет получил
    доливку по цене переноса.

    Здесь бюджета хватает ровно на одно действие из пары. Правильный исход —
    цена пересчитывается на выжившем наборе, и половина пары уезжает уже по
    ПОЛНОЙ цене, а не по цене компенсированного переноса.
    """
    give = _transfer("g", "1", -70_000.0)
    take = _transfer("t", "2", +70_000.0)
    daily_cost = {"1": 10_000.0, "2": 10_000.0}
    priced = [take, give]
    risks = agent_e1.net_risk(priced, daily_cost)
    assert risks["t"] < 70_000.0, "скидка за перенос не выдана — стенд не тот"

    prepared, deferred = agent_e1.fit_week_budget(
        priced, dict(risks), risks["t"] + 1.0, {}, {}, daily_cost)

    assert not prepared, (
        "получатель уехал один по цене компенсированного переноса — "
        "кабинет получил доливку со скидкой, которую нечем компенсировать")
    # Донор не влез по деньгам, получатель — потому что в одиночку стоит
    # полную цену: 70 000 ₽ вместо 17 500 со скидкой за компенсацию.
    assert sorted(a["idempotency_key"] for a in deferred) == ["g", "t"]


def test_weekly_rail_keeps_arithmetic_free_across_repricing():
    """Уберите этот тест — и первый же урезанный круг отправит в отложенные
    все отсечения разом.

    Кто платит риском, решил отбор полос: класс 0 приехал оттуда с нулевой
    ценой, потому что минус-фраза снимает деньги с огня, а не ставит их под
    удар. net_risk про классы не знает и на пересчёте назначил бы им полную
    цену — а пересчёт случается ровно тогда, когда бюджет и так кончился.
    Обещание «класс 0 вносится весь и сразу» умирало бы тем вернее, чем
    больше в кабинете мусора.
    """
    cut = {"idempotency_key": "cut", "action_kind": "negative.add",
           "object_level": "campaign", "object_id": "1",
           "exposure": {"daily_rub": 300.0, "cut_daily_rub": 300.0},
           "payload": {"CampaignId": 1, "AddedPhrases": ["мгсу"]}}
    give = _transfer("g", "1", -70_000.0)
    take = _transfer("t", "2", +70_000.0)
    daily_cost = {"1": 10_000.0, "2": 10_000.0}
    risks = agent_e1.net_risk([take, give], daily_cost)
    risks["cut"] = 0.0  # так его оценил отбор полос

    prepared, _ = agent_e1.fit_week_budget(
        [take, give, cut], risks, risks["t"] + 1.0, {}, {}, daily_cost)

    assert [a["idempotency_key"] for a in prepared] == ["cut"]
    assert prepared[0]["risk_rub"] == 0.0

def test_lane_price_is_the_net_price_of_the_tact():
    # Цена, по которой списывается недельный бюджет, берётся из отбора полос
    # (net_risk на наборе такта), а не считается заново по действию. Иначе
    # перенос денег внутри кабинета платит обеими сторонами сразу: полоса
    # видит компенсацию, а недельная рельса — нет, и 100 000 ₽ переноса стоят
    # кабинету 200 000 ₽ лимита (дефект 8б плана беты).
    import inspect

    source = inspect.getsource(agent_e1.run_account)
    start = source.index("lanes.select(")
    assert "action_risk(" not in source[start:], (
        "после отбора полос цена действия считается заново — скидка за "
        "встречное движение денег теряется")


def test_run_verdict_names_the_run_by_its_worst_account():
    # Вердикт прогона едет в edu_agent_runs и оттуда на экран агента. Пустое
    # место читается как «прошло тихо», поэтому отказ кабинета обязан дожить
    # до истории, а не остаться в логе Actions.
    assert agent_e1.run_verdict([{"account": "a"}, {"account": "b"}]) == "GREEN"
    assert agent_e1.run_verdict([]) == "NOTHING_TO_DO"
    assert agent_e1.run_verdict(
        [{"verdict": "NOTHING_TO_DO"}, {"verdict": "NOTHING_TO_DO"}]
    ) == "NOTHING_TO_DO"
    # Один работал, второму нечего делать — прогон рабочий.
    assert agent_e1.run_verdict([{"account": "a"}, {"verdict": "NOTHING_TO_DO"}]) == "GREEN"
    assert agent_e1.run_verdict(
        [{"account": "a"}, {"verdict": "ACCOUNT_FAILED"}]
    ) == "PARTIAL_FAILURE"
    assert agent_e1.run_verdict(
        [{"verdict": "ACCOUNT_FAILED"}, {"verdict": "ACCOUNT_FAILED"}]
    ) == "RED"
    # Протухший расчёт важнее молчания: он объясняет, ПОЧЕМУ прогон молчит.
    assert agent_e1.run_verdict(
        [{"verdict": "NOTHING_TO_DO"}, {"verdict": "STALE_COMPUTED_SETTINGS"}]
    ) == "STALE_COMPUTED_SETTINGS"
    assert agent_e1.run_verdict(
        [{"verdict": "NO_COMPUTED_SETTINGS"}]
    ) == "NO_COMPUTED_SETTINGS"


def test_run_verdict_reaches_the_black_box():
    # Проверка у получателя: вердикт обязан лежать в том самом словаре,
    # который читает blackbox.save_run (report["verdict"]), а не только
    # печататься рядом.
    import inspect

    source = inspect.getsource(agent_e1._run_all)
    saved = source[source.index('stage="e1"'):]
    assert '"verdict": run_verdict(account_reports)' in saved


# --- экономика кампании для ожидания: проводка в боевой прогон -------------

def _portfolio_row(cost, leads):
    return {"setting_kind": "budget_target", "setting_key": "target_28d",
            "raw_value": cost, "support_n": leads}


def test_expectation_context_carries_spend_and_lead_price():
    # Без этих двух чисел expectation.of возвращает None, и семь рычагов из
    # девяти остаются без обещания именно в проде: тесты рычагов подставляют
    # контекст сами и ожидание видят, а прогон отдал бы в кабинет действия,
    # которые нечем ранжировать при отборе и нечем судить в замере такта.
    context = agent_e1.campaign_expectation_context(
        "114057545", {"114057545": 5553.71},
        {"114057545": [_portfolio_row(280_000.0, 140.0)]})

    assert context["daily_cost_rub"] == 5553.71
    assert context["cpa_rub"] == 2000.0


def test_expectation_context_omits_lead_price_when_there_is_none():
    # Строки портфеля нет или в ней ноль лидов — ключа быть не должно.
    # Ноль означал бы «лид бесплатен», и ожидание вышло бы бесконечным;
    # отсутствие ключа честно читается рычагом как «обещать не из чего».
    no_row = agent_e1.campaign_expectation_context(
        "1", {"1": 1000.0}, {})
    no_leads = agent_e1.campaign_expectation_context(
        "1", {"1": 1000.0}, {"1": [_portfolio_row(50_000.0, 0.0)]})

    assert "cpa_rub" not in no_row
    assert "cpa_rub" not in no_leads
    assert no_row["daily_cost_rub"] == 1000.0


def test_expectation_context_omits_spend_when_the_directory_is_silent():
    # Пустой справочник расхода — пробел в витрине. Ноль рублей в день сделал
    # бы любое ожидание нулевым, а нулевое ожидание петля обучения зачла бы
    # сбывшимся прогнозом.
    context = agent_e1.campaign_expectation_context(
        "1", {}, {"1": [_portfolio_row(280_000.0, 140.0)]})

    assert "daily_cost_rub" not in context
    assert context["cpa_rub"] == 2000.0


def test_cutting_levers_get_their_conversions_and_their_threshold():
    """Уберите этот тест — и основание класса 0 снова не доедет до боя.

    diff_negatives и diff_placements умеют собирать evidence, но собирают его
    ТОЛЬКО из того, что им передали. До 27.08.2026 прогон звал их с одним
    cut_cost: конверсии вырезаемого трафика оставались нулём по умолчанию, а
    порога не было вовсе — значит evidence не производился, и каждая
    минус-фраза приезжала в отбор классом 2. Тесты рычагов при этом зелёные:
    они передают всё сами.
    """
    import inspect

    source = inspect.getsource(agent_e1.run_account)
    for call in ("negatives.diff_negatives(", "placements.diff_placements("):
        start = source.index(call)
        args = source[start:start + 400]
        assert "cut_conversions=" in args, call
        assert "baseline_cpa=" in args, call


class _UnitsDrainedClient(_RecordingWriteClient):
    """Тот же кабинет, но баллов почти не осталось (1 из 1000 при резерве 5 %)."""

    def __init__(self, login, sandbox=True, dry_run=True):
        super().__init__(login, sandbox=sandbox, dry_run=dry_run)
        self.units_left = 1
        self.units_limit = 1000


def test_units_low_reaches_the_black_box_not_just_the_counter(monkeypatch, capsys):
    """Уберите этот тест — и строка отказа умрёт по дороге в базу.

    apply_actions умеет отдавать отказы строками, но пока run_account их не
    подхватывает, они остаются внутри отчёта и в edu_agent_rejects не попадают:
    ровно тот дефект, когда «зелёный тест» стоит рядом с неработающим рычагом.
    В бою это выглядит как прогон, который ничего не применил и ничего про это
    не сказал.
    """
    saved = {}
    _patch_single_account(
        monkeypatch,
        computed=[_setting("bid_modifier:device", "DESKTOP", 30.0)],
    )
    monkeypatch.setattr(agent_e1, "WriteClient", _UnitsDrainedClient)
    monkeypatch.setattr(agent_e1.blackbox, "save_run",
                        lambda *a, **k: saved.update(k) or {
                            "run_id": "test", "saved": False, "rejects": 0,
                            "error": "тест"})

    assert agent_e1.main() == 0
    report = _reports(capsys)[0]

    assert report["result"]["units_low"] == 1
    assert _RecordingWriteClient.instances[0].sent == []
    rows = [r for r in saved.get("rejects", [])
            if r["reason"] == rejects_mod.UNITS_LOW]
    assert len(rows) == 1, "отказ по баллам не доехал до чёрного ящика"
    assert rows[0]["account"] == "acc-1"
    assert rows[0]["stage"] == "e1"
    # Строки отказов не дублируются в отчёт прогона: там остаются счётчики.
    assert "rejects" not in report["result"]
    assert report["rejects"][rejects_mod.UNITS_LOW] == 1
