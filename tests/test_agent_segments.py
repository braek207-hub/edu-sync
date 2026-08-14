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
