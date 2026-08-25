# -*- coding: utf-8 -*-
"""Сверка журнала с кабинетом: «применено» ≠ «стоит».

Журнал доказывает, что вызов ушёл и API ответил успехом. Что настройка
живёт в кабинете сейчас, он не доказывает: её возвращают руками, её не
применяет пакетная стратегия, кампанию архивируют. Всё это время агент
судит исходы изменения, которого больше нет.
"""

from sync.agent import drift


def _strategy(weekly=None, cpa=None):
    block = {}
    if weekly is not None:
        block["WeeklySpendLimit"] = weekly
    if cpa is not None:
        block["AverageCpa"] = cpa
    return {"Search": {"WbMaximumConversionRate": block},
            "Network": {"NetworkDefault": {}}}


def _budget_action(new=140_000_000, old=100_000_000, **extra):
    return {"action_id": "a1", "account": "acc", "object_id": "111",
            "direct_type": "WEEKLY_SPEND_LIMIT", "setting_key": "search",
            "applied_at": "2026-08-20 10:00:00",
            "payload": {"WeeklySpendLimit": new},
            "previous_state": {"WeeklySpendLimit": old}, **extra}


def _tcpa_action(new=800_000_000, old=1_000_000_000):
    return {"action_id": "a2", "account": "acc", "object_id": "111",
            "direct_type": "AVERAGE_CPA", "setting_key": "target_cpa",
            "applied_at": "2026-08-20 10:00:00",
            "payload": {"TargetCpa": new},
            "previous_state": {"TargetCpa": old}}


def test_cabinet_shows_what_the_agent_set():
    row = drift.check(_budget_action(),
                      {"strategy": _strategy(weekly=140_000_000)})

    assert row["verdict"] == drift.MATCH


def test_value_returned_to_the_starting_point_is_a_rollback_not_drift():
    # У возврата ровно в previous_state есть автор, и его надо искать:
    # это другая новость, чем «разъехалось на пару процентов».
    row = drift.check(_budget_action(),
                      {"strategy": _strategy(weekly=100_000_000)})

    assert row["verdict"] == drift.REVERTED
    assert row["previous"] == 100_000_000


def test_third_value_is_drift():
    row = drift.check(_budget_action(),
                      {"strategy": _strategy(weekly=90_000_000)})

    assert row["verdict"] == drift.DRIFTED
    assert row["actual"] == 90_000_000


def test_rounding_by_direct_is_not_drift():
    # Директ хранит микрорубли с собственным округлением; допуск заведомо
    # меньше минимального шага любого рычага.
    row = drift.check(_budget_action(),
                      {"strategy": _strategy(weekly=140_000_100)})

    assert row["verdict"] == drift.MATCH


def test_target_cpa_is_checked_the_same_way():
    row = drift.check(_tcpa_action(), {"strategy": _strategy(cpa=800_000_000)})

    assert row["verdict"] == drift.MATCH


def test_daily_budget_is_read_from_its_own_field():
    action = {"action_id": "a3", "object_id": "111", "direct_type": "DAILY_BUDGET",
              "payload": {"DailyBudget": {"Amount": 20_000_000}},
              "previous_state": {"DailyBudget": {"Amount": 30_000_000}}}

    row = drift.check(action, {"daily_budget": {"Amount": 30_000_000}})

    assert row["verdict"] == drift.REVERTED


def test_suspended_campaign_that_is_running_again():
    action = {"action_id": "a4", "object_id": "111", "direct_type": "CAMPAIGN_STATE",
              "payload": {"CampaignId": 111}, "previous_state": {"State": "ON"}}

    assert drift.check(action, {"state": "SUSPENDED"})["verdict"] == drift.MATCH
    assert drift.check(action, {"state": "ON"})["verdict"] == drift.REVERTED


def test_missing_object_is_not_silently_a_match():
    row = drift.check(_budget_action(), None)

    assert row["verdict"] == drift.OBJECT_GONE


def test_unknown_kind_is_reported_as_unverified():
    # «Не проверено» обязано отличаться от «проверено и сошлось», иначе
    # покрытие сверки нельзя измерить.
    action = {"action_id": "a5", "object_id": "111",
              "direct_type": "NEGATIVE_KEYWORDS", "payload": {}, "previous_state": {}}

    assert drift.check(action, {})["verdict"] == drift.UNVERIFIABLE


def test_unreadable_cabinet_value_is_not_a_match():
    row = drift.check(_budget_action(), {"strategy": {"Search": {"X": {}}}})

    assert row["verdict"] == drift.UNREADABLE


def test_only_the_last_action_on_a_segment_is_checked():
    # Иначе сверка объявила бы дрейфом собственную работу агента: вчерашнее
    # изменение сегодня перекрыто сегодняшним.
    old = _budget_action(new=120_000_000, applied_at="2026-08-19 10:00:00")
    old["action_id"] = "вчера"
    new = _budget_action(new=140_000_000, applied_at="2026-08-20 10:00:00")

    kept = drift.latest_per_segment([old, new])

    assert [a["action_id"] for a in kept] == ["a1"]


def test_different_segments_both_survive():
    kept = drift.latest_per_segment([_budget_action(), _tcpa_action()])

    assert len(kept) == 2


def test_alarms_name_only_what_needs_a_human():
    rows = [{"verdict": drift.MATCH}, {"verdict": drift.REVERTED},
            {"verdict": drift.UNVERIFIABLE}]

    assert drift.alarms(rows) == ["reverted: 1"]


def test_summary_counts_every_verdict():
    rows = [{"verdict": drift.MATCH}, {"verdict": drift.MATCH},
            {"verdict": drift.DRIFTED}]

    assert drift.summarize(rows) == {drift.MATCH: 2, drift.DRIFTED: 1}


class _Cabinet:
    """Двойник кабинета: отдаёт кампании и запоминает, о чём спросили."""

    def __init__(self, campaigns):
        self.campaigns = campaigns
        self.asked = []
        self.units_left = 1000

    def get(self, service, params):
        self.asked.append((service, params))
        ids = set(params["SelectionCriteria"]["Ids"])
        return {"Campaigns": [c for c in self.campaigns if c["Id"] in ids]}


def _campaign(cid=111, weekly=140_000_000, state="ON"):
    return {"Id": cid, "Type": "TEXT_CAMPAIGN", "State": state,
            "DailyBudget": None,
            "TextCampaign": {"BiddingStrategy": _strategy(weekly=weekly)}}


def test_account_check_reads_the_cabinet_and_matches():
    from sync import agent_drift

    cabinet = _Cabinet([_campaign()])

    report = agent_drift.check_account(cabinet, [_budget_action()])

    assert report["checked"] == 1
    assert report["verdicts"] == {drift.MATCH: 1}
    assert report["alarms"] == []


def test_account_check_reports_a_rollback_by_a_human():
    from sync import agent_drift

    cabinet = _Cabinet([_campaign(weekly=100_000_000)])

    report = agent_drift.check_account(cabinet, [_budget_action()])

    assert report["alarms"] == ["reverted: 1"]
    assert report["mismatched"][0]["object_id"] == "111"


def test_campaign_missing_from_the_cabinet_is_flagged():
    from sync import agent_drift

    report = agent_drift.check_account(_Cabinet([]), [_budget_action()])

    assert report["verdicts"] == {drift.OBJECT_GONE: 1}


def test_only_needed_fields_are_requested():
    # Лишние поля стоят баллов API, а недостающие превращают сверку в
    # молчаливое «нечитаемо».
    from sync import agent_drift

    cabinet = _Cabinet([_campaign()])
    agent_drift.check_account(cabinet, [_budget_action()])

    _, params = cabinet.asked[0]
    assert set(params["FieldNames"]) == {"Id", "Type", "State", "Status", "DailyBudget"}
    assert params["TextCampaignFieldNames"] == ["BiddingStrategy"]
