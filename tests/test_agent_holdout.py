from sync.agent.holdout import (
    dead_holdout_ids,
    kish_n_eff,
    select_holdout,
)


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


# ------------------------------------------- квота направления — по деньгам


def _account_like_production(n_directions_tail=6):
    """Кабинет формы прод-кабинета: два тяжёлых направления и хвост мелких.

    Числа из docs/AGENT-TICK-POWER.md (30 дней до 28.08.2026): vpo 22 кампании
    и 9,5 млн ₽, spo 27 кампаний и 7,0 млн ₽ — вместе 64 % расхода; остальные
    направления мельче на порядок, но алфавитно стоят ПЕРЕД ними.
    """
    out = []
    heavy = (("vpo", 22, 434_000.0), ("spo", 27, 257_000.0))
    for direction, count, cost in heavy:
        for i in range(count):
            out.append({"campaign_id": f"{direction}-{i}", "direction": direction,
                        "cost_30d": cost, "leads_30d": 150})
    # dist, it, med, ntb, other, school — те самые, что забирали все места.
    for direction in ("dist", "it", "med", "ntb", "other", "school")[:n_directions_tail]:
        for i in range(7):
            out.append({"campaign_id": f"{direction}-{i}", "direction": direction,
                        "cost_30d": 20_000.0, "leads_30d": 20})
    return out


def test_the_biggest_directions_are_not_left_without_control():
    # Дефект: обход направлений по кругу шёл по алфавиту, а мест всего пять.
    # До spo и vpo очередь не доходила НИКОГДА — 64 % расхода кабинета
    # оставались без контроля, и «сезон» для них вычитался по кампаниям
    # совсем других направлений (docs/AGENT-TICK-POWER.md).
    picked = select_holdout(_account_like_production(), share=0.06)

    assert {"vpo", "spo"} <= {c["direction"] for c in picked}


def test_control_repeats_the_account_by_money():
    # Контроль должен представлять кабинет по деньгам: у направлений с 64 %
    # расхода мест не меньше половины.
    campaigns = _account_like_production()
    picked = select_holdout(campaigns, share=0.06)
    heavy = [c for c in picked if c["direction"] in ("vpo", "spo")]

    assert len(heavy) >= len(picked) / 2


def test_a_direction_without_money_does_not_take_a_seat_by_its_name():
    # Прямая проверка порядка: направление «aaa» стоит первым по алфавиту и
    # последним по расходу. Место оно занимать не должно.
    campaigns = _account_like_production() + [
        {"campaign_id": "aaa-0", "direction": "aaa", "cost_30d": 1.0, "leads_30d": 1}]
    picked = select_holdout(campaigns, share=0.06)

    assert "aaa" not in {c["direction"] for c in picked}


def test_quotas_never_exceed_the_target():
    # Метод наибольших остатков обязан раздать РОВНО целевое число мест: и
    # недобор, и перебор одинаково ломают размер заповедника.
    campaigns = _account_like_production()
    for share in (0.02, 0.06, 0.1, 0.25):
        alive = len(campaigns)
        assert len(select_holdout(campaigns, share=share)) == round(alive * share)


def test_a_tiny_direction_is_not_asked_for_more_than_it_has():
    # У направления одна кампания, а доля расхода тянет на две: квота не
    # может быть больше очереди, и лишнее место обязано уйти соседям.
    campaigns = [{"campaign_id": "big-0", "direction": "big",
                  "cost_30d": 1_000_000.0, "leads_30d": 100}]
    campaigns += [{"campaign_id": f"small-{i}", "direction": "small",
                   "cost_30d": 1_000.0, "leads_30d": 10} for i in range(19)]
    picked = select_holdout(campaigns, share=0.5)

    assert len(picked) == 10
    assert len([c for c in picked if c["direction"] == "big"]) == 1


# ------------------------------------------------ выбытие мёртвых из когорты


def test_a_campaign_without_leads_leaves_the_holdout():
    # Заповедник из девяти кампаний за месяц терял четыре, а эффективный
    # размер контроля падал с 5,19 до 2,93 (docs/AGENT-TICK-POWER.md).
    # Мёртвая кампания не двигается вместе с кабинетом — вычитать по ней
    # сезон значит вычитать ноль.
    campaigns = [{"campaign_id": "alive", "direction": "vpo", "leads_30d": 40},
                 {"campaign_id": "dead", "direction": "vpo", "leads_30d": 0}]

    assert dead_holdout_ids(["alive", "dead"], campaigns) == ["dead"]


def test_a_campaign_gone_from_the_mart_counts_as_dead():
    # Кампании нет в агрегатах окна вовсе: это не молчание витрины, а
    # отсутствие открутки. Оставлять её контролем не на чем.
    campaigns = [{"campaign_id": "alive", "direction": "vpo", "leads_30d": 40}]

    assert dead_holdout_ids(["alive", "vanished"], campaigns) == ["vanished"]


def test_a_living_holdout_loses_nobody():
    campaigns = [{"campaign_id": f"c-{i}", "direction": "vpo", "leads_30d": 5}
                 for i in range(3)]

    assert dead_holdout_ids(["c-0", "c-1"], campaigns) == []


# ------------------------------------- эффективный размер контроля (Kish)


def test_equal_campaigns_weigh_as_many_as_there_are():
    assert kish_n_eff([10, 10, 10, 10, 10]) == 5.0


def test_one_dominant_campaign_makes_the_group_a_single_campaign():
    # 560 лидов у одной кампании и по 14 у четырёх: сумма лидов порог в 20
    # проходит тридцатикратно, а контроль — это одна кампания.
    assert kish_n_eff([560, 14, 14, 14, 14]) < 1.5


def test_no_leads_no_effective_size():
    assert kish_n_eff([]) == 0.0
    assert kish_n_eff([0, 0]) == 0.0
