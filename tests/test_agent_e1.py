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
        lambda *_: [{"setting_kind": "bid_modifier:device", "setting_key": "mobile",
                  "value": 30.0, "support_n": 1000, "raw_value": 30.0}],
    )
    monkeypatch.setattr(agent_e1.agent_db, "load_holdout_ids", lambda: [])
    monkeypatch.setattr(agent_e1.agent_db, "load_daily_cost_by_campaign",
                         lambda *_: {"111": 500.0})
    monkeypatch.setattr(agent_e1.agent_db, "load_baseline_cpa", lambda *_: {})
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit", lambda *_: 50_000.0)
    monkeypatch.setattr(agent_e1.writer_db, "spent_risk", lambda *_: 0.0)
    monkeypatch.setattr(agent_e1.writer_db, "stale_planned", lambda *a, **k: [])
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
        lambda *_: [
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
    monkeypatch.setattr(agent_e1.writer_db, "stale_planned", lambda *a, **k: [])
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


# =========================================================================
# Оркестратор прогона: настройки по кабинетам, лимит и риск на прогон,
# состав репетиции и зависшие записи журнала.
# =========================================================================


class _MultiCabinetClient:
    """Кабинет с заданным набором кампаний и пустыми корректировками.

    campaigns_by_login задаётся тестом; mutate только записывает вызовы.
    """

    instances = []
    campaigns_by_login = {}

    def __init__(self, login, sandbox=True, dry_run=True):
        self.login = login
        self.sandbox = sandbox
        self.dry_run = dry_run
        self.units_left = None
        self.sent = []
        _MultiCabinetClient.instances.append(self)

    def get(self, service, params):
        if service == "campaigns":
            ids = self.campaigns_by_login.get(self.login, [])
            return {"Campaigns": [{"Id": i} for i in ids]}
        if service == "bidmodifiers":
            return {"BidModifiers": []}
        raise AssertionError(f"неожиданный сервис: {service}")

    def mutate(self, service, method, params):
        self.sent.append((service, method, params))
        return {"dry_run": True}


def _setting(kind, key, value, support=1000):
    return {"setting_kind": kind, "setting_key": key, "value": float(value),
            "support_n": support, "raw_value": 1.0 + value / 100.0}


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
               baseline_cpa=None, stale=(), journal=None):
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
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit", lambda *_: 50_000.0)
    monkeypatch.setattr(agent_e1.writer_db, "spent_risk", lambda *_: 0.0)
    monkeypatch.setattr(agent_e1.writer_db, "stale_planned", lambda *a, **k: list(stale))
    monkeypatch.setattr(agent_e1.writer_db, "find_action_by_key", lambda *_: None)
    rows = journal if journal is not None else []
    monkeypatch.setattr(agent_e1.writer_db, "insert_action",
                        lambda row: (rows.append(row), row["idempotency_key"])[1])
    monkeypatch.setattr(agent_e1.writer_db, "mark_action", lambda *a, **k: None)
    monkeypatch.setattr(agent_e1, "WriteClient", _MultiCabinetClient)
    return rows


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

    reports = {r["account"]: r for r in _reports(capsys)}
    silent = reports["acc-2"]
    assert silent["verdict"] == "NO_COMPUTED_SETTINGS"
    assert silent["computed_settings"] == 0
    assert "object_id" in silent["reason"]
    assert "acc-2" in silent["reason"]
    # Кабинет с настройками при этом отработал как обычно.
    assert reports["acc-1"]["desired"] == 1


# ------------------------------------- дефект 3: лимит действий — на прогон


def test_action_cap_is_shared_across_cabinets(monkeypatch, capsys):
    # cap_actions вызывался внутри цикла по кабинетам: при четырёх кабинетах
    # потолок был вчетверо выше заявленного. Смысл рельсы — ограничить объём
    # изменений, которые можно проверить и осмысленно откатить, а он не
    # зависит от числа кабинетов.
    monkeypatch.setattr(agent_e1, "MAX_ACTIONS_PER_RUN", 2)
    settings = [_setting("bid_modifier:device", "DESKTOP", 30)]
    _patch_run(
        monkeypatch,
        computed_by_login={"acc-1": settings, "acc-2": settings},
        campaigns_by_login={"acc-1": [111, 112], "acc-2": [221, 222]},
        daily_cost={"111": 10.0, "112": 10.0, "221": 10.0, "222": 10.0},
    )

    assert agent_e1.main() == 0

    sent = sum(len(c.sent) for c in _MultiCabinetClient.instances)
    assert sent == 2, "потолок прогона обязан считаться на все кабинеты сразу"
    reports = {r["account"]: r for r in _reports(capsys)}
    assert reports["acc-1"]["actions_left_in_run"] == 0
    assert reports["acc-2"]["deferred_by_cap"] == 2


# --------------------------- дефект 4: риск кампании списывается один раз


def test_campaign_risk_is_charged_once_per_run(monkeypatch, capsys):
    # Четыре действия по одной кампании списывали четырёхкратный расход:
    # бюджет исчерпывался на второй-третьей кампании, и посчитанные
    # корректировки капали по паре в неделю.
    journal = []
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
    # Кампания одна — платим за неё один раз, а не трижды.
    assert report["risk_charged_rub"] == 7000.0
    assert sum(row["risk_rub"] for row in journal) == 7000.0


def test_budget_is_not_exhausted_by_repeated_actions_on_same_campaign(monkeypatch, capsys):
    # Тот же расчёт «в лоб»: три действия по кампании с расходом 1000 ₽/день
    # стоили бы 21 000 ₽ при остатке 10 000 ₽ — два из трёх ушли бы в
    # отложенные, хотя реальная цена ошибки по этой кампании 7000 ₽.
    journal = []
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
    monkeypatch.setattr(agent_e1.writer_db, "risk_limit", lambda *_: 10_000.0)

    assert agent_e1.main() == 0

    report = _reports(capsys)[0]
    assert report["deferred_by_risk"] == 0
    assert report["result"]["dry_run"] == 3


# ------------------------- дефект 6: репетиция показывает, что было бы записано


def test_dry_run_report_shows_what_would_be_written(monkeypatch, capsys):
    # Главный артефакт для решения «включать боевую запись» показывал нули и
    # не содержал ни числа готовых действий, ни их состава.
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
    )
    monkeypatch.setattr(agent_e1.writer_db, "stale_planned",
                        lambda minutes, account=None: calls.append((minutes, account)) or [])

    assert agent_e1.main() == 0

    assert calls == [(agent_e1.STALE_PLANNED_MINUTES, "acc-1"),
                     (agent_e1.STALE_PLANNED_MINUTES, "acc-2")]
