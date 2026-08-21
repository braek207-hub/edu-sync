import sync.agent.segments as segments
from sync.agent.segments import _stamp_report_name


def _payload(fields, goals=None):
    params = {
        "SelectionCriteria": {"DateFrom": "2026-05-16", "DateTo": "2026-08-14"},
        "FieldNames": fields,
        "ReportName": "agent-device",
        "ReportType": "CUSTOM_REPORT",
    }
    if goals:
        params["Goals"] = goals
    return {"params": params}


def test_name_is_deterministic_for_same_params():
    a, b = _payload(["Device", "Clicks"]), _payload(["Device", "Clicks"])
    _stamp_report_name(a)
    _stamp_report_name(b)
    assert a["params"]["ReportName"] == b["params"]["ReportName"]


def test_name_changes_when_params_change():
    # Директ помнит связку имя↔параметры: тот же ReportName с другими параметрами
    # отвергается ошибкой 4000.
    without = _payload(["Device", "Clicks"])
    with_goals = _payload(["Device", "Clicks", "Conversions"], goals=["123"])
    _stamp_report_name(without)
    _stamp_report_name(with_goals)
    assert without["params"]["ReportName"] != with_goals["params"]["ReportName"]


def test_name_keeps_readable_prefix():
    p = _payload(["Device"])
    _stamp_report_name(p)
    assert p["params"]["ReportName"].startswith("agent-device-")


def test_stamping_twice_keeps_stable_prefix():
    # Повторный вызов не должен ломать формат (хеш пересчитается от нового имени).
    p = _payload(["Device"])
    _stamp_report_name(p)
    first = p["params"]["ReportName"]
    _stamp_report_name(p)
    assert p["params"]["ReportName"].startswith(first)


# --------------- цели кабинета: без них Reports API не отдаёт Conversions
# Прогон 32406152097: цели брались ТОЛЬКО из секрета DIRECT_CLIENTS_JSON, там их
# не было, Conversions в запрос не попадала, и все двенадцать сегментных срезов
# отказали с «в срезе нет ни одной конверсии». Агент оказался слеп молча — по
# логу это выглядело как «данных нет», а не как «мы их не спросили».

def test_priority_goals_are_extracted_from_the_strategy():
    campaign = {"TextCampaign": {"BiddingStrategy": {"Search": {
        "AverageCpa": {"PriorityGoals": [{"GoalId": 111}, {"GoalId": 222}]}}}}}
    assert segments.goal_ids_from_campaign(campaign) == [111, 222]


def test_single_goal_strategies_are_extracted_too():
    # MaximumConversionRate / PayForConversion несут одиночный GoalId, а не список.
    campaign = {"TextCampaign": {"BiddingStrategy": {"Network": {
        "PayForConversion": {"GoalId": 333, "Cpa": 1000}}}}}
    assert segments.goal_ids_from_campaign(campaign) == [333]


def test_goals_are_collected_across_campaign_types_and_channels():
    # Кампании трёх типов приходят в одном ответе разными ключами; цель могла
    # быть задана только в одном канале, и потерять его нельзя.
    campaign = {
        "TextCampaign": {"BiddingStrategy": {
            "Search": {"AverageCpa": {"PriorityGoals": [{"GoalId": 1}]}},
            "Network": {"AverageCpa": {"PriorityGoals": [{"GoalId": 2}]}}}},
        "UnifiedCampaign": {"BiddingStrategy": {
            "Search": {"AverageCpa": {"PriorityGoals": [{"GoalId": 3}]}}}},
        "MobileAppCampaign": {"BiddingStrategy": {
            "Network": {"PayForConversion": {"GoalId": 4}}}},
    }
    assert segments.goal_ids_from_campaign(campaign) == [1, 2, 3, 4]


def test_goals_are_deduplicated_and_ordered():
    # Одна цель у разных кампаний и каналов — обычное дело. Порядок обязан быть
    # стабилен: цели уходят в параметр запроса, от которого зависит имя отчёта.
    campaign = {"TextCampaign": {"BiddingStrategy": {
        "Search": {"AverageCpa": {"PriorityGoals": [{"GoalId": 9}, {"GoalId": 5}]}},
        "Network": {"AverageCpa": {"PriorityGoals": [{"GoalId": 5}]}}}}}
    assert segments.goal_ids_from_campaign(campaign) == [5, 9]


def test_campaign_without_goals_yields_nothing():
    assert segments.goal_ids_from_campaign({"TextCampaign": {"BiddingStrategy": {
        "Search": {"HighestPosition": {}}}}}) == []
    assert segments.goal_ids_from_campaign({}) == []


def test_account_goals_request_asks_for_strategies_of_all_campaign_types(monkeypatch):
    # Форма запроса взята из рабочего edu_direct_settings: три типа кампаний
    # запрашиваются тремя *FieldNames сразу. Запросишь только Id — стратегия не
    # придёт вовсе, и целей не будет ни одной.
    seen = {}

    def fake_post(url, login, payload, what):
        seen.update({"url": url, "login": login, "payload": payload, "what": what})
        return {"Campaigns": [{"Id": 1, "TextCampaign": {"BiddingStrategy": {
            "Search": {"AverageCpa": {"PriorityGoals": [{"GoalId": 77}]}}}}}]}

    monkeypatch.setattr(segments, "_api_post", fake_post)
    assert segments.fetch_account_goal_ids("cab") == [77]

    params = seen["payload"]["params"]
    assert params["FieldNames"] == ["Id"]
    for key in ("TextCampaignFieldNames", "UnifiedCampaignFieldNames",
                "MobileAppCampaignFieldNames"):
        assert params[key] == ["BiddingStrategy"], key
    assert seen["login"] == "cab"


def test_account_goals_walk_all_pages(monkeypatch):
    # Кабинет EDU — сотни кампаний. Оборвись обход на первой странице, цели
    # поздних кампаний потерялись бы, а отчёт всё равно выглядел бы успешным.
    pages = [
        {"Campaigns": [{"Id": i, "TextCampaign": {"BiddingStrategy": {"Search": {
            "AverageCpa": {"PriorityGoals": [{"GoalId": 10}]}}}}}
            for i in range(segments.PAGE_LIMIT)]},
        {"Campaigns": [{"Id": 9999, "TextCampaign": {"BiddingStrategy": {"Search": {
            "AverageCpa": {"PriorityGoals": [{"GoalId": 20}]}}}}}]},
    ]
    calls = []

    def fake_post(url, login, payload, what):
        calls.append(payload["params"]["Page"]["Offset"])
        return pages[len(calls) - 1]

    monkeypatch.setattr(segments, "_api_post", fake_post)
    assert segments.fetch_account_goal_ids("cab") == [10, 20]
    assert calls == [0, segments.PAGE_LIMIT]
