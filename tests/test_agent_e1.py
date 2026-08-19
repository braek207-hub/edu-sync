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

import sync.agent_e1 as agent_e1


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


def test_normalize_actual_splits_combined_gender_and_age():
    # Одна запись DemographicsAdjustment может нести Gender И Age одновременно
    # (ставка на пересечение сегментов) — без раскладки diff потерял бы
    # вторую половину и предложил add там, где нужен set.
    # BidModifier здесь — то, что реально отдаёт API: 100-базный коэффициент
    # (120 = «+20 %»). Нормализация переводит его в дельту плана.
    item = {"Id": 55, "DemographicsAdjustment": {
        "BidModifier": 120, "Gender": "GENDER_MALE", "Age": "AGE_25_34"}}

    out = agent_e1._normalize_actual(item)

    assert len(out) == 2
    keys = {(r["Type"], r["key"]) for r in out}
    assert keys == {
        ("DEMOGRAPHICS_ADJUSTMENT", "GENDER_MALE"),
        ("DEMOGRAPHICS_ADJUSTMENT", "AGE_25_34"),
    }
    assert all(r["Id"] == 55 and r["percent"] == 20 for r in out)


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


def test_main_excludes_action_and_reports_reason_when_baseline_cpa_empty(monkeypatch, capsys):
    # Сквозной тест правки по код-ревью: если справочник базовых CPA пуст
    # целиком, ни у одного действия нет работающей красной линии — main()
    # обязан не применять их (mutate не вызывается) и показать причину в
    # отчёте прогона (no_red_line), а не тихо использовать дефолт-плейсхолдер.
    monkeypatch.setattr(agent_e1, "_clients", lambda: [{"login": "acc-1"}])
    monkeypatch.setattr(agent_e1.writer_db, "ensure_writer_tables", lambda: None)
    monkeypatch.setattr(
        agent_e1.agent_db, "load_latest_computed_settings",
        lambda: [{"setting_kind": "bid_modifier:device", "setting_key": "mobile",
                  "value": 30.0, "support_n": 1000, "raw_value": 30.0}],
    )
    monkeypatch.setattr(agent_e1.agent_db, "load_holdout_ids", lambda: [])
    monkeypatch.setattr(agent_e1.agent_db, "load_daily_cost_by_campaign",
                         lambda *_: {"111": 500.0})
    monkeypatch.setattr(agent_e1.agent_db, "load_baseline_cpa", lambda *_: {})
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit", lambda *_: 50_000.0)
    monkeypatch.setattr(agent_e1.writer_db, "spent_risk", lambda *_: 0.0)
    monkeypatch.setattr(agent_e1, "WriteClient", _FakeWriteClient)

    exit_code = agent_e1.main()

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["no_red_line"] == {"count": 1, "reason": agent_e1.NO_RED_LINE_REASON}
    assert report["absolute_max_cpa"] is None
    assert report["result"]["applied"] == 0
    assert report["result"]["rejected"] == 0
    assert report["result"]["failed"] == 0


# --------------------------------- неприменимые настройки видны в отчёте


class _RecordingWriteClient:
    """Кампания 111 без корректировок; mutate только записывает вызовы."""

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
        lambda: [
            {"setting_kind": "bid_modifier:region", "setting_key": "Москва",
             "value": 30.0, "support_n": 1000, "raw_value": 1.3},
            {"setting_kind": "bid_modifier:device", "setting_key": "DESKTOP",
             "value": 30.0, "support_n": 1000, "raw_value": 1.3},
        ],
    )
    monkeypatch.setattr(agent_e1.agent_db, "load_holdout_ids", lambda: [])
    monkeypatch.setattr(agent_e1.agent_db, "load_daily_cost_by_campaign",
                         lambda *_: {"111": 500.0})
    monkeypatch.setattr(agent_e1.agent_db, "load_baseline_cpa", lambda *_: {"111": 1000.0})
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit", lambda *_: 50_000.0)
    monkeypatch.setattr(agent_e1.writer_db, "spent_risk", lambda *_: 0.0)
    monkeypatch.setattr(agent_e1.writer_db, "find_action_by_key", lambda *_: None)
    monkeypatch.setattr(agent_e1.writer_db, "insert_action", lambda action: 1)
    monkeypatch.setattr(agent_e1.writer_db, "mark_action", lambda *a, **k: None)
    monkeypatch.setattr(agent_e1, "WriteClient", _RecordingWriteClient)

    assert agent_e1.main() == 0

    report = json.loads(capsys.readouterr().out)
    assert report["desired"] == 1                      # только устройство
    assert report["unsupported"]["count"] == 1         # регион — с причиной
    assert sum(report["unsupported"]["by_reason"].values()) == 1
    assert report["result"]["failed"] == 0

    sent = _RecordingWriteClient.instances[0].sent
    assert len(sent) == 1
    item = sent[0][2]["BidModifiers"][0]
    assert item["DesktopAdjustment"]["BidModifier"] == 130   # дельта +30 → 100-база
    assert "MobileAdjustment" not in item
