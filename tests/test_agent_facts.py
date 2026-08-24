from sync.agent.facts import assemble_facts

DIRECT = [
    {"date": "2026-08-01", "campaign_id": "111", "campaign_name": "vuz / A", "project": "vuz",
     "direction": "vpo", "cost": 1000.0, "clicks": 50, "impressions": 900,
     "w_avg_impr_pos": 1.5, "w_auction_win_share": 0.4},
]
LEADS = [
    {"lead_id": "L1", "campaign_id": "111", "created_date": "2026-08-01", "is_eff": True, "is_paid": True},
    {"lead_id": "L2", "campaign_id": "111", "created_date": "2026-08-01", "is_eff": False, "is_paid": False},
]
SCORES = [
    {"lead_id": "L1", "scoring_point": "at_creation", "p_pay": 0.03},
    {"lead_id": "L2", "scoring_point": "at_creation", "p_pay": 0.01},
]


def test_joins_cost_and_leads_on_date_and_campaign():
    rows = assemble_facts(DIRECT, LEADS, SCORES)
    assert len(rows) == 1
    row = rows[0]
    assert row["cost"] == 1000.0
    assert row["leads"] == 2
    assert row["eff_leads"] == 1
    assert row["payments_fact"] == 1
    assert abs(row["sum_p_pay"] - 0.04) < 1e-9


def test_campaign_without_leads_still_present():
    rows = assemble_facts(DIRECT, [], [])
    assert rows[0]["leads"] == 0
    assert rows[0]["sum_p_pay"] == 0.0


def test_leads_without_direct_row_create_own_row():
    # Лид на кампании, по которой нет расхода в этот день — строка обязана появиться,
    # иначе конверсии молча теряются.
    rows = assemble_facts([], LEADS, SCORES)
    assert len(rows) == 1
    assert rows[0]["campaign_id"] == "111"
    assert rows[0]["cost"] == 0.0
    assert rows[0]["leads"] == 2


def test_uses_only_at_creation_scoring_point():
    scores = SCORES + [{"lead_id": "L1", "scoring_point": "post_connection", "p_pay": 0.9}]
    rows = assemble_facts(DIRECT, LEADS, scores)
    # post_connection не должен задваивать сумму: витрина живёт на at_creation.
    assert abs(rows[0]["sum_p_pay"] - 0.04) < 1e-9


def test_grain_is_date_times_campaign():
    direct = DIRECT + [{**DIRECT[0], "date": "2026-08-02"}]
    rows = assemble_facts(direct, LEADS, SCORES)
    assert len(rows) == 2
    assert {r["fact_date"] for r in rows} == {"2026-08-01", "2026-08-02"}


def test_aggregates_crm_depth():
    direct = [{"date": "2026-08-01", "campaign_id": "111", "campaign_name": "A", "project": "vuz",
               "direction": "vpo", "cost": 1000.0, "clicks": 50, "impressions": 900,
               "w_avg_impr_pos": 1.5, "w_auction_win_share": 0.4}]
    leads = [
        {"lead_id": "L1", "campaign_id": "111", "created_date": "2026-08-01",
         "is_eff": True, "is_paid": True, "is_connected": True, "is_deal": True,
         "created_ts": "2026-08-01T10:00:00+00:00", "connected_ts": "2026-08-01T10:30:00+00:00",
         "payment_date": "2026-08-11", "revenue": 50000.0},
        {"lead_id": "L2", "campaign_id": "111", "created_date": "2026-08-01",
         "is_eff": False, "is_paid": False, "is_connected": False, "is_deal": False,
         "created_ts": "2026-08-01T12:00:00+00:00", "connected_ts": None,
         "payment_date": None, "revenue": 0.0},
    ]
    row = assemble_facts(direct, leads, [])[0]
    assert row["connected_leads"] == 1
    assert row["deals"] == 1
    assert row["mins_to_connection_sum"] == 30.0
    assert row["mins_to_connection_count"] == 1      # недозвон в среднее не входит
    assert row["days_to_pay_sum"] == 10.0
    assert row["days_to_pay_count"] == 1
    assert row["revenue"] == 50000.0


def test_crm_depth_is_zero_without_timestamps():
    leads = [{"lead_id": "L9", "campaign_id": "111", "created_date": "2026-08-01",
              "is_eff": False, "is_paid": False, "is_connected": True, "is_deal": False,
              "created_ts": None, "connected_ts": None, "payment_date": None, "revenue": 0.0}]
    row = assemble_facts([], leads, [])[0]
    assert row["connected_leads"] == 1
    assert row["mins_to_connection_count"] == 0     # без меток времени длительность не считаем


def test_sums_not_averages_are_stored():
    # Храним суммы и счётчики: среднее по среднему не складывается при перегруппировке
    # по неделям или направлениям.
    leads = [
        {"lead_id": f"L{i}", "campaign_id": "111", "created_date": "2026-08-01",
         "is_eff": True, "is_paid": False, "is_connected": True, "is_deal": False,
         "created_ts": "2026-08-01T10:00:00+00:00",
         "connected_ts": f"2026-08-01T10:{10 * (i + 1):02d}:00+00:00",
         "payment_date": None, "revenue": 0.0}
        for i in range(3)
    ]
    row = assemble_facts([], leads, [])[0]
    assert row["mins_to_connection_sum"] == 10.0 + 20.0 + 30.0
    assert row["mins_to_connection_count"] == 3


def test_conversions_of_direct_goals_reach_the_mart():
    # Цель CPA в Директе назначается за КОНВЕРСИЮ ЦЕЛИ (страница «Спасибо»),
    # а не за эффективный лид CRM. Рычаг целевого CPA (Э3.5) не построить, не
    # зная, сколько таких конверсий было и сколько они стоили: витрина обязана
    # нести их наравне с кликами.
    direct = [{**DIRECT[0], "conversions": 12}]
    rows = assemble_facts(direct, LEADS, SCORES)
    assert rows[0]["conversions"] == 12


def test_missing_conversions_are_zero_not_absent():
    rows = assemble_facts(DIRECT, LEADS, SCORES)
    assert rows[0]["conversions"] == 0
