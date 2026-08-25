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
    # Кликов у «вуз бесплатно» хватает, чтобы ноль конверсий что-то значил
    # (правило трёх против базовой конверсии набора), а расход уже выше цены,
    # которую мы согласны платить даже по верхней границе.
    queries = [
        {"campaign_id": "1", "query": "вуз бесплатно", "cost": 5000.0, "clicks": 300, "conversions": 0},
        {"campaign_id": "1", "query": "вуз москва", "cost": 5000.0, "clicks": 500, "conversions": 30},
        {"campaign_id": "1", "query": "вуз отзывы", "cost": 100.0, "clicks": 2, "conversions": 0},
    ]
    out = minus_word_candidates(queries, cpa_limit=1000.0)
    flagged = {r["query"] for r in out}
    assert flagged == {"вуз бесплатно"}


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


def test_payload_has_no_duplicate_identifiers():
    # Id/CampaignId/AdGroupId лежат отдельными колонками — в JSONB они лишний вес.
    items = [{"Id": 10, "CampaignId": 1, "AdGroupId": 5, "Keyword": "вуз", "State": "ON"}]
    payload = build_object_rows(items, "keyword", seen_on="2026-08-14")[0]["payload"]
    assert "Id" not in payload
    assert "CampaignId" not in payload
    assert "AdGroupId" not in payload
    assert payload["Keyword"] == "вуз"
    assert payload["State"] == "ON"


# ------------------- минус-слова: агрегат по фразе и статистический критерий


def test_minus_candidates_aggregate_a_phrase_across_rows():
    # Правило применялось к СТРОКЕ «фраза × кампания × окно»: одна такая
    # строка редко жжёт больше трёх CPA, и на бою кандидатов было ноль при
    # 678 фразах без конверсий на 5,4 млн ₽. Фраза оценивается целиком.
    from sync.agent.objects import minus_word_candidates
    queries = [
        {"campaign_id": "1", "query": "мгсу", "cost": 2000.0, "clicks": 20,
         "conversions": 0},
        {"campaign_id": "1", "query": "мгсу", "cost": 2000.0, "clicks": 20,
         "conversions": 0},
        {"campaign_id": "1", "query": "мгсу", "cost": 2000.0, "clicks": 20,
         "conversions": 0},
    ]
    out = minus_word_candidates(queries, cpa_limit=1000.0, base_conversion=0.05)
    assert len(out) == 1
    assert out[0]["query"] == "мгсу"
    assert out[0]["cost"] == 6000.0
    assert out[0]["clicks"] == 60


def test_minus_candidates_need_enough_clicks_to_judge():
    # Ноль конверсий на трёх кликах — не приговор, а отсутствие наблюдений:
    # верхняя граница конверсии при нуле успехов ≈ 3/N, и при малом N она
    # выше средней конверсии кабинета.
    from sync.agent.objects import minus_word_candidates
    queries = [{"campaign_id": "1", "query": "редкий", "cost": 9000.0,
                "clicks": 3, "conversions": 0}]
    assert minus_word_candidates(queries, cpa_limit=1000.0) == []


def test_minus_candidates_flag_expensive_converting_phrases_too():
    # Фраза с конверсиями, но по цене вдвое выше допустимой, жжёт деньги не
    # меньше нулевой: критерий экономический, а не «есть ли конверсия».
    from sync.agent.objects import minus_word_candidates
    queries = [{"campaign_id": "1", "query": "дорогая", "cost": 30_000.0,
                "clicks": 100, "conversions": 3}]
    out = minus_word_candidates(queries, cpa_limit=1000.0)
    assert out and out[0]["cpa"] == 10_000.0
    assert out[0]["reason"] == "cpa_above_limit"


def test_minus_candidates_keep_profitable_phrases():
    from sync.agent.objects import minus_word_candidates
    queries = [{"campaign_id": "1", "query": "хорошая", "cost": 30_000.0,
                "clicks": 100, "conversions": 40}]
    assert minus_word_candidates(queries, cpa_limit=1000.0) == []


# ------------------- площадки РСЯ: кандидаты на запрет


def test_placement_candidates_reuse_the_minus_word_logic():
    # Правило то же, что у фраз: ноль конверсий при достаточном объёме или
    # конверсии дороже допустимого. Площадка агрегируется по всем кампаниям.
    from sync.agent.objects import placement_candidates
    rows = [
        {"campaign_id": "1", "placement": "trash.site", "cost": 30_000.0,
         "clicks": 300, "conversions": 0},
        {"campaign_id": "2", "placement": "trash.site", "cost": 20_000.0,
         "clicks": 200, "conversions": 0},
        {"campaign_id": "1", "placement": "good.site", "cost": 30_000.0,
         "clicks": 300, "conversions": 40},
    ]
    out = placement_candidates(rows, cpa_limit=1000.0, base_conversion=0.05)
    assert [r["placement"] for r in out] == ["trash.site"]
    assert out[0]["cost"] == 50_000.0
    assert out[0]["campaigns"] == ["1", "2"]


def test_placement_candidates_need_enough_clicks():
    from sync.agent.objects import placement_candidates
    rows = [{"campaign_id": "1", "placement": "rare.site", "cost": 9000.0,
             "clicks": 5, "conversions": 0}]
    assert placement_candidates(rows, cpa_limit=1000.0, base_conversion=0.05) == []


# ------------------- минус-СЛОВА: одно слово гасит семейство фраз


def test_word_candidates_aggregate_phrases_by_word():
    # Отдельная фраза редко набирает объём для приговора, а слово, общее для
    # полусотни фраз, — набирает. Слово «мгту» на живых данных: 43 фразы,
    # 67 кликов, ноль конверсий, 11 506 ₽ за месяц.
    from sync.agent.objects import word_minus_candidates
    queries = [
        {"campaign_id": "1", "query": f"мгту {i}", "cost": 1000.0,
         "clicks": 20, "conversions": 0} for i in range(5)
    ] + [
        {"campaign_id": "1", "query": "колледж москва", "cost": 5000.0,
         "clicks": 100, "conversions": 10},
    ]
    out = word_minus_candidates(queries, cpa_limit=1000.0, base_conversion=0.05)
    words = {r["query"] for r in out}
    assert "мгту" in words
    assert "колледж" not in words          # слово работающих фраз не трогаем
    mgtu = next(r for r in out if r["query"] == "мгту")
    assert mgtu["cost"] == 5000.0
    assert mgtu["phrases"] == 5


def test_word_candidates_need_the_word_to_appear_in_several_phrases():
    # Слово из одной-единственной фразы — это та же фраза, и судить его
    # отдельно значит обходить порог наблюдаемости через переименование.
    from sync.agent.objects import word_minus_candidates
    queries = [{"campaign_id": "1", "query": "уникальный запрос", "cost": 9000.0,
                "clicks": 300, "conversions": 0}]
    assert word_minus_candidates(queries, cpa_limit=1000.0,
                                 base_conversion=0.05) == []


def test_word_candidates_skip_short_words():
    from sync.agent.objects import word_minus_candidates
    queries = [{"campaign_id": "1", "query": f"в мгту {i}", "cost": 1000.0,
                "clicks": 50, "conversions": 0} for i in range(4)]
    out = word_minus_candidates(queries, cpa_limit=1000.0, base_conversion=0.05)
    assert {r["query"] for r in out} == {"мгту"}


def test_candidate_carries_cost_split_by_campaign():
    # Расход фразы, записанный целиком в каждую её кампанию, при обратной
    # сборке складывается сам с собой: репетиция Э1 отчиталась о 29,7 млн ₽
    # «покрытого расхода» при месячном расходе кабинета 8,5 млн. Кандидат
    # обязан нести РАЗБИВКУ по кампаниям.
    from sync.agent.objects import minus_word_candidates
    queries = [
        {"campaign_id": "1", "query": "мгсу", "cost": 4000.0, "clicks": 40,
         "conversions": 0},
        {"campaign_id": "2", "query": "мгсу", "cost": 2000.0, "clicks": 20,
         "conversions": 0},
    ]
    out = minus_word_candidates(queries, cpa_limit=1000.0, base_conversion=0.05)
    assert out[0]["cost"] == 6000.0
    assert out[0]["cost_by_campaign"] == {"1": 4000.0, "2": 2000.0}


def test_word_candidate_carries_cost_split_by_campaign():
    from sync.agent.objects import word_minus_candidates
    queries = [
        {"campaign_id": "1", "query": f"мгту {i}", "cost": 1000.0, "clicks": 20,
         "conversions": 0} for i in range(3)
    ] + [
        {"campaign_id": "2", "query": "мгту заочно", "cost": 500.0, "clicks": 10,
         "conversions": 0},
    ]
    out = word_minus_candidates(queries, cpa_limit=1000.0, base_conversion=0.05)
    mgtu = next(r for r in out if r["query"] == "мгту")
    assert mgtu["cost_by_campaign"] == {"1": 3000.0, "2": 500.0}


# ------------------- защита семантического ядра при минусации СЛОВ


def test_word_with_conversions_is_never_a_minus_candidate():
    # Ветка «конверсии есть, но дорого» законна для ФРАЗЫ и катастрофична для
    # СЛОВА: минус-слово гасит ВСЁ семейство фраз, включая конверсионные. На
    # бою правило предложило заминусовать «высшее» (226 конверсий, 1,18 млн ₽)
    # — то есть отрезать ядро трафика образовательного проекта.
    from sync.agent.objects import word_minus_candidates
    queries = [
        {"campaign_id": "1", "query": f"высшее образование {i}", "cost": 50_000.0,
         "clicks": 200, "conversions": 0} for i in range(4)
    ] + [
        {"campaign_id": "1", "query": "высшее образование", "cost": 20_000.0,
         "clicks": 100, "conversions": 3},
    ]
    out = word_minus_candidates(queries, cpa_limit=1500.0, base_conversion=0.03)
    assert not [r for r in out if r["query"] == "высшее"]


def test_word_from_our_own_keywords_is_protected():
    # Слово, входящее в КЛЮЧЕВЫЕ фразы кабинета (matched_key), мы купили
    # осознанно. Дорого — регулируется ставкой и целью CPA (Э3.5), а не
    # запретом: запрет отменяет собственную семантику.
    from sync.agent.objects import word_minus_candidates
    queries = [
        {"campaign_id": "1", "query": f"институты москвы {i}", "cost": 40_000.0,
         "clicks": 300, "conversions": 0, "matched_key": "институты москвы"}
        for i in range(4)
    ]
    out = word_minus_candidates(queries, cpa_limit=1500.0, base_conversion=0.03)
    assert not [r for r in out if r["query"] == "институты"]


def test_junk_word_without_conversions_is_still_a_candidate():
    # Обратная половина: слово, которого нет в нашей семантике и которое не
    # дало ни одной конверсии, по-прежнему кандидат.
    from sync.agent.objects import word_minus_candidates
    queries = [
        {"campaign_id": "1", "query": f"бесплатно скачать {i}", "cost": 30_000.0,
         "clicks": 300, "conversions": 0, "matched_key": "институты москвы"}
        for i in range(4)
    ]
    out = word_minus_candidates(queries, cpa_limit=1500.0, base_conversion=0.03)
    assert [r for r in out if r["query"] == "скачать"]
