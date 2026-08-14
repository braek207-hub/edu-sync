from sync.agent.objects import build_object_rows, content_hash, minus_word_candidates


def test_hash_is_stable_regardless_of_key_order():
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


def test_hash_changes_when_content_changes():
    assert content_hash({"a": 1}) != content_hash({"a": 2})


def test_hash_handles_cyrillic():
    # Кириллица в текстах объявлений не должна ломать хеш и не должна экранироваться
    # по-разному между прогонами.
    assert content_hash({"title": "Поступление в вуз"}) == content_hash({"title": "Поступление в вуз"})


def test_build_object_rows_fills_hash_and_dates():
    items = [{"Id": 10, "CampaignId": 1, "AdGroupId": 5, "Keyword": "вуз москва"}]
    rows = build_object_rows(items, "keyword", seen_on="2026-08-14")
    assert len(rows) == 1
    row = rows[0]
    assert row["object_level"] == "keyword"
    assert row["object_id"] == "10"
    assert row["campaign_id"] == "1"
    assert row["parent_id"] == "5"
    assert row["first_seen"] == "2026-08-14"
    assert row["last_seen"] == "2026-08-14"
    assert row["content_hash"]


def test_build_object_rows_same_content_same_hash():
    items = [{"Id": 10, "CampaignId": 1, "AdGroupId": 5, "Keyword": "вуз"}]
    a = build_object_rows(items, "keyword", seen_on="2026-08-14")
    b = build_object_rows(items, "keyword", seen_on="2026-08-20")
    assert a[0]["content_hash"] == b[0]["content_hash"]


def test_minus_candidates_flags_spend_without_conversions():
    queries = [
        {"campaign_id": "1", "query": "вуз бесплатно", "cost": 5000.0, "clicks": 50, "conversions": 0},
        {"campaign_id": "1", "query": "вуз москва", "cost": 5000.0, "clicks": 50, "conversions": 3},
        {"campaign_id": "1", "query": "вуз отзывы", "cost": 100.0, "clicks": 2, "conversions": 0},
    ]
    out = minus_word_candidates(queries, cpa_limit=1000.0, multiplier=3.0)
    flagged = {r["query"] for r in out}
    assert flagged == {"вуз бесплатно"}  # расход > 3 CPA и ноль конверсий


def test_minus_candidates_empty_when_nothing_qualifies():
    queries = [{"campaign_id": "1", "query": "вуз", "cost": 10.0, "clicks": 1, "conversions": 0}]
    assert minus_word_candidates(queries, cpa_limit=1000.0) == []


def test_top_queries_keeps_expensive_and_drops_tail():
    # Миллион запросов с одним кликом решений не даёт, но занял 450 МБ.
    from sync.agent.objects import top_queries_by_cost

    queries = [{"campaign_id": "1", "query": f"q{i}", "cost": float(i), "clicks": 1}
               for i in range(100)]
    out = top_queries_by_cost(queries, per_campaign=10)
    assert len(out) == 10
    assert min(float(q["cost"]) for q in out) == 90.0


def test_top_queries_applies_limit_per_campaign():
    from sync.agent.objects import top_queries_by_cost

    queries = ([{"campaign_id": "1", "query": f"a{i}", "cost": 10.0, "clicks": 1} for i in range(5)]
               + [{"campaign_id": "2", "query": f"b{i}", "cost": 10.0, "clicks": 1} for i in range(5)])
    out = top_queries_by_cost(queries, per_campaign=3)
    assert len(out) == 6
    assert {q["campaign_id"] for q in out} == {"1", "2"}
