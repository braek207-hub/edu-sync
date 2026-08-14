from sync.agent.holdout import select_holdout


def _campaigns(n_per_dir=10):
    out = []
    for direction in ("vpo", "spo", "dist"):
        for i in range(n_per_dir):
            out.append({
                "campaign_id": f"{direction}-{i}",
                "direction": direction,
                "cost_30d": 1000.0 * (i + 1),
                "leads_30d": 10 * (i + 1),
            })
    return out


def test_selects_about_target_share():
    # Доля считается от всего кабинета: 30 кампаний × 10% = 3, а не «по одной на страту».
    picked = select_holdout(_campaigns(), share=0.1)
    assert len(picked) == 3


def test_share_six_percent_stays_small():
    # Регрессия: заповедник разрастался до 26% кабинета из-за минимума на каждую страту.
    picked = select_holdout(_campaigns(n_per_dir=28), share=0.06)
    assert len(picked) == 5  # 84 кампании × 6%


def test_is_deterministic_across_runs():
    a = select_holdout(_campaigns(), share=0.1)
    b = select_holdout(_campaigns(), share=0.1)
    assert [c["campaign_id"] for c in a] == [c["campaign_id"] for c in b]


def test_covers_every_direction():
    picked = select_holdout(_campaigns(), share=0.2)
    assert {c["direction"] for c in picked} == {"vpo", "spo", "dist"}


def test_does_not_pick_only_worst_campaigns():
    # Заповедник обязан быть репрезентативным: если в нём только дно,
    # база сравнения кривая и заслуга агента завышена.
    picked = select_holdout(_campaigns(), share=0.2)
    picked_ids = {p["campaign_id"] for p in picked}
    costs = [c["cost_30d"] for c in _campaigns() if c["campaign_id"] in picked_ids]
    assert max(costs) > 5000.0


def test_skips_campaigns_without_traffic():
    campaigns = _campaigns() + [{"campaign_id": "dead-1", "direction": "vpo",
                                 "cost_30d": 0.0, "leads_30d": 0}]
    picked = select_holdout(campaigns, share=0.2)
    assert "dead-1" not in {c["campaign_id"] for c in picked}


def test_empty_input_returns_empty():
    assert select_holdout([], share=0.1) == []
