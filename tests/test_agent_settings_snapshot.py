from sync.agent.settings_snapshot import build_snapshot_rows, normalize_settings


def test_normalize_drops_volatile_fields():
    # Поля, меняющиеся сами по себе, обязаны выпасть из хеша — иначе новая версия
    # снимка будет писаться каждый день и таблица распухнет.
    raw = {"strategy": "AVERAGE_CPA", "weeklyBudget": 70000, "statusClarification": "Идут показы",
           "lastChange": "2026-08-14T03:00:00", "todaySpend": 12345.0}
    out = normalize_settings(raw)
    assert "lastChange" not in out
    assert "todaySpend" not in out
    assert "statusClarification" not in out
    assert out["strategy"] == "AVERAGE_CPA"
    assert out["weeklyBudget"] == 70000


def test_same_settings_give_same_hash():
    rows_a = build_snapshot_rows({"111": {"strategy": "A", "lastChange": "x"}}, seen_on="2026-08-14")
    rows_b = build_snapshot_rows({"111": {"strategy": "A", "lastChange": "y"}}, seen_on="2026-08-20")
    assert rows_a[0]["content_hash"] == rows_b[0]["content_hash"]


def test_changed_settings_give_new_hash():
    rows_a = build_snapshot_rows({"111": {"strategy": "A"}}, seen_on="2026-08-14")
    rows_b = build_snapshot_rows({"111": {"strategy": "B"}}, seen_on="2026-08-14")
    assert rows_a[0]["content_hash"] != rows_b[0]["content_hash"]


def test_snapshot_row_shape():
    rows = build_snapshot_rows({"111": {"strategy": "A"}}, seen_on="2026-08-14")
    assert rows[0]["campaign_id"] == "111"
    assert rows[0]["first_seen"] == "2026-08-14"
    assert rows[0]["last_seen"] == "2026-08-14"
    assert isinstance(rows[0]["settings"], dict)


def test_empty_input():
    assert build_snapshot_rows({}, seen_on="2026-08-14") == []
