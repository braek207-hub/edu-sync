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


def test_own_semantics_come_from_the_structure_snapshot_not_the_report():
    # Колонка matched_key отчёта запросов НЕ равна «что мы купили»: Директ
    # возвращает в ней и подбор автотаргетинга. Замер 29.08.2026: из 32 266
    # строк, где matched_key совпадает с самим запросом (7,70 млн ₽), ключами
    # кабинета оказались 625 — 1,9 %. Защита принимала остальные за нашу
    # закупку и не давала минусовать ровно тот мусор, ради которого рычаг есть.
    from sync.agent.objects import own_semantics
    queries = [
        {"campaign_id": "1", "query": "диплом купить срочно",
         "matched_key": "диплом купить срочно"},
    ]
    words, phrases = own_semantics(queries, {"1": ["институты москвы"]})
    assert words["1"] == {"институты", "москвы"}
    assert frozenset({"диплом", "купить", "срочно"}) not in phrases["1"]


def test_sleeping_keyword_is_protected_though_the_report_never_saw_it():
    # Ключ без показов в окне отчёт не покажет вовсе, а минус поверх него убил
    # бы его будущие показы молча. Снимок структуры знает такие ключи — ради
    # этого он и стал источником.
    from sync.agent.objects import own_semantics
    words, phrases = own_semantics([], {"1": ["заочное обучение москва"]})
    assert "заочное" in words["1"]
    assert frozenset({"заочное", "обучение", "москва"}) in phrases["1"]


def test_campaign_missing_from_the_snapshot_falls_back_to_matched_key():
    # Пробел синка — не разрешение минусовать свои ключи. Кампании, которой в
    # снимке нет вовсе (3 кампании, 0,59 млн ₽ в замере 29.08.2026), защита
    # достаётся из прежнего источника.
    from sync.agent.objects import own_semantics
    queries = [
        {"campaign_id": "9", "query": "мти институт отзывы",
         "matched_key": "мти институт"},
    ]
    words, phrases = own_semantics(queries, {"1": ["институты москвы"]})
    assert words["9"] == {"мти", "институт"}
    assert frozenset({"мти", "институт"}) in phrases["9"]


def test_campaign_with_structure_but_no_keywords_is_protected_by_nothing():
    # Мастер кампаний и автотаргетинг: структура снята, ключей в кампании нет
    # (6 кампаний, 1,03 млн ₽ в замере 29.08.2026). Это факт, а не пробел
    # данных, — защищать нечего, и откат на matched_key был бы ошибкой.
    from sync.agent.objects import own_semantics
    queries = [
        {"campaign_id": "1", "query": "диплом купить", "matched_key": "диплом купить"},
    ]
    words, phrases = own_semantics(queries, {"1": []})
    assert words["1"] == set()
    assert phrases["1"] == set()


def test_direct_operators_do_not_leak_into_the_protection():
    # Ключ кабинета несёт операторы («+в», «!москва», кавычки), а минус-фраза
    # их не различает. Слово с приклеенным оператором защитило бы не то.
    from sync.agent.objects import own_semantics
    words, phrases = own_semantics([], {"1": ['"!институты +в москве"']})
    assert words["1"] == {"институты", "москве"}
    assert frozenset({"институты", "в", "москве"}) in phrases["1"]


def test_word_protection_covers_only_the_campaign_that_bought_it():
    # Запрет пишется В КАМПАНИЮ, значит и «своим» слово бывает по кампаниям.
    # Общая на кабинет защита снимала 2156 слов-кандидатов из 2163 (замер по
    # выгрузке 29.08.2026) — рычаг минус-слов не работал вовсе.
    from sync.agent.objects import word_minus_candidates
    queries = [
        {"campaign_id": "1", "query": f"институты москвы {i}", "cost": 40_000.0,
         "clicks": 300, "conversions": 0, "matched_key": "институты москвы"}
        for i in range(4)
    ] + [
        {"campaign_id": "2", "query": f"институты москвы {i}", "cost": 30_000.0,
         "clicks": 200, "conversions": 0, "matched_key": "вузы москвы"}
        for i in range(4)
    ]
    out = word_minus_candidates(queries, cpa_limit=1500.0, base_conversion=0.03)
    row = [r for r in out if r["query"] == "институты"]
    assert row, "в кампании 2 слово не куплено — кандидат обязан выжить"
    # Приговор и цена риска считаются только по кампании, куда поедет запрет:
    # трафик защищённой кампании отсечение не тронет.
    assert row[0]["campaigns"] == ["2"]
    assert row[0]["cost_by_campaign"] == {"2": 120_000.0}


def test_word_conversions_of_a_protected_campaign_do_not_judge_it():
    # Обратная сторона того же правила: конверсии кампании, где слово куплено,
    # к кандидату отношения не имеют — их поток запрет не гасит. Считать их
    # значило бы оправдывать слово трафиком, которого действие не касается.
    from sync.agent.objects import word_minus_candidates
    queries = [
        {"campaign_id": "1", "query": "институты москвы", "cost": 20_000.0,
         "clicks": 100, "conversions": 30, "matched_key": "институты москвы"},
    ] + [
        {"campaign_id": "2", "query": f"институты москвы {i}", "cost": 30_000.0,
         "clicks": 200, "conversions": 0, "matched_key": "вузы москвы"}
        for i in range(4)
    ]
    out = word_minus_candidates(queries, cpa_limit=1500.0, base_conversion=0.03)
    row = [r for r in out if r["query"] == "институты"]
    assert row and row[0]["campaigns"] == ["2"]


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


# ------------------- расширение семантики: обратная сторона минусации


def test_expansion_candidates_are_converting_queries_we_do_not_buy():
    # Зеркало минусации: запрос уже приносит конверсии дешевле допустимого,
    # но своей ключевой фразы у него нет — мы получаем его случайно, по
    # широкому соответствию, и не управляем ни ставкой, ни объявлением.
    from sync.agent.objects import expansion_candidates
    queries = [
        # Конверсионный и дешёвый, но не куплен: кандидат.
        {"campaign_id": "1", "query": "кинорежиссер москва", "matched_key": "вуз москва",
         "cost": 2183.0, "clicks": 30, "conversions": 12},
        # Куплен явно — управлять уже можем, кандидатом не является.
        {"campaign_id": "1", "query": "вуз москва", "matched_key": "вуз москва",
         "cost": 5000.0, "clicks": 40, "conversions": 10},
        # Дороже допустимого — расширяться на него незачем.
        {"campaign_id": "1", "query": "дорогой запрос", "matched_key": "вуз москва",
         "cost": 90_000.0, "clicks": 50, "conversions": 2},
        # Одна конверсия — шум, а не сигнал.
        {"campaign_id": "1", "query": "случайный запрос", "matched_key": "вуз москва",
         "cost": 300.0, "clicks": 5, "conversions": 1},
    ]
    from sync.agent.objects import own_semantics
    bought = own_semantics(queries, {"1": ["вуз москва"]})[1]
    out = expansion_candidates(queries, cpa_limit=1700.0, bought=bought)
    assert [c["query"] for c in out] == ["кинорежиссер москва"]
    row = out[0]
    assert row["conversions"] == 12
    assert row["cpa"] < 1700.0
    assert row["campaigns"] == ["1"]


def test_expansion_candidates_rank_by_headroom_not_by_volume():
    # Порядок — по недополученной выгоде: конверсии × (допустимый CPA −
    # фактический). Дешёвый запрос с меньшим числом конверсий может стоить
    # выше, чем дорогой с большим.
    from sync.agent.objects import expansion_candidates
    queries = [
        {"campaign_id": "1", "query": "дешёвый", "matched_key": "ключ",
         "cost": 600.0, "clicks": 20, "conversions": 6},      # CPA 100
        {"campaign_id": "1", "query": "средний", "matched_key": "ключ",
         "cost": 12_000.0, "clicks": 40, "conversions": 10},  # CPA 1200
    ]
    from sync.agent.objects import own_semantics
    out = expansion_candidates(
        queries, cpa_limit=1700.0,
        bought=own_semantics(queries, {"1": ["ключ"]})[1])
    assert [c["query"] for c in out] == ["дешёвый", "средний"]


def test_expansion_ignores_queries_already_bought_in_another_campaign():
    from sync.agent.objects import expansion_candidates
    queries = [
        {"campaign_id": "1", "query": "заочное обучение", "matched_key": "вуз",
         "cost": 1000.0, "clicks": 20, "conversions": 5},
        {"campaign_id": "2", "query": "другой запрос", "matched_key": "заочное обучение",
         "cost": 1000.0, "clicks": 20, "conversions": 5},
    ]
    from sync.agent.objects import own_semantics
    bought = own_semantics(queries, {"1": ["вуз"],
                                     "2": ["заочное обучение"]})[1]
    out = expansion_candidates(queries, cpa_limit=1700.0, bought=bought)
    assert [c["query"] for c in out] == ["другой запрос"]


def test_expansion_cpa_never_counts_more_conversions_than_clicks():
    # Директ приписывает конверсию запросу по атрибуции, а клик — по факту
    # показа: на живых данных 61 фраза имеет конверсий больше, чем кликов
    # («1 клик, 5 конверсий»). CPA по таким числам занижен в разы, и фраза
    # уезжает в топ расширения на основании чужого окна. Доказанный объём не
    # может превышать собственные клики.
    from sync.agent.objects import expansion_candidates
    queries = [
        {"campaign_id": "1", "query": "странная фраза", "matched_key": "ключ",
         "cost": 900.0, "clicks": 3, "conversions": 12},
    ]
    from sync.agent.objects import own_semantics
    out = expansion_candidates(
        queries, cpa_limit=1700.0,
        bought=own_semantics(queries, {"1": ["ключ"]})[1])
    assert out[0]["conversions"] == 12          # факт не переписываем
    assert out[0]["proven_conversions"] == 3    # но считаем по кликам
    assert out[0]["cpa"] == 300.0               # 900 / 3, а не 900 / 12


def test_expansion_needs_a_few_clicks_of_its_own():
    # Одна-две случайности не должны поднимать фразу в топ: расширяемся на
    # то, у чего есть собственный доказанный объём.
    from sync.agent.objects import expansion_candidates
    queries = [
        {"campaign_id": "1", "query": "один клик", "matched_key": "ключ",
         "cost": 90.0, "clicks": 1, "conversions": 5},
    ]
    from sync.agent.objects import own_semantics
    assert expansion_candidates(
        queries, cpa_limit=1700.0,
        bought=own_semantics(queries, {"1": ["ключ"]})[1]) == []


def test_expansion_does_not_take_autotargeting_matches_for_our_own_keys():
    # Дефект, измеренный 29.08.2026: «уже куплено» бралось из колонки
    # matched_key отчёта запросов, а Директ возвращает в ней и подбор
    # автотаргетинга — запрос слово в слово. Из 31 745 запросов окна 27 520
    # снимались как «наши ключи», и генератор запусков оставался без входа:
    # 35 запросов с двумя конверсиями вместо 281. Источник правды — снимок
    # структуры кабинета, и запрос, которого в снимке нет, остаётся кандидатом
    # сколько бы раз отчёт ни повторил его в matched_key.
    from sync.agent.objects import expansion_candidates, own_semantics
    queries = [
        {"campaign_id": "1", "query": "колледж дизайна заочно",
         "matched_key": "колледж дизайна заочно",
         "cost": 1200.0, "clicks": 20, "conversions": 4},
    ]
    bought = own_semantics(queries, {"1": ["колледж"]})[1]
    out = expansion_candidates(queries, cpa_limit=1700.0, bought=bought)
    assert [c["query"] for c in out] == ["колледж дизайна заочно"]


def test_expansion_falls_back_to_the_report_when_the_snapshot_missed_a_campaign():
    # Кампания, которой снимок не видел, откатывается на matched_key
    # (own_semantics). Пустая защита на пробеле данных предлагала бы докупить
    # собственные ключи.
    from sync.agent.objects import expansion_candidates, own_semantics
    queries = [
        {"campaign_id": "9", "query": "вуз заочно", "matched_key": "вуз заочно",
         "cost": 1200.0, "clicks": 20, "conversions": 4},
    ]
    bought = own_semantics(queries, {"1": ["колледж"]})[1]
    assert expansion_candidates(queries, cpa_limit=1700.0, bought=bought) == []


# ------------------- чёрный список площадок владельца (Павел, 02.09.2026)


def test_blocklist_bans_a_matching_site_in_the_top_regardless_of_conversions():
    # DSP-обменники и VPN-приложения дают спам-лиды: конверсии Директа у них
    # живые, и статистический критерий такую площадку защищает. Решение о
    # качестве принимает человек паттерном — конверсии не спасают.
    from sync.agent.objects import blocklist_placement_candidates
    rows = [
        {"campaign_id": "1", "placement": "com.pingsecure.client.app",
         "cost": 3500.0, "clicks": 700, "conversions": 13},
        {"campaign_id": "1", "placement": "dzen.ru",
         "cost": 30000.0, "clicks": 1200, "conversions": 82},
    ]
    out = blocklist_placement_candidates(rows, patterns=["vpn", "pingsecure"],
                                         top_n=30)
    assert [c["placement"] for c in out] == ["com.pingsecure.client.app"]
    assert out[0]["reason"] == "blocklist"
    assert out[0]["conversions"] == 13


def test_blocklist_waits_until_the_site_is_in_the_top_by_cost():
    # Ворота топ-N — защита лимита кабинета (1000 слотов): копеечный матч
    # ждёт, пока не станет заметен деньгами, а не занимает слот сразу.
    from sync.agent.objects import blocklist_placement_candidates
    rows = [{"campaign_id": "1", "placement": f"site{i}.ru",
             "cost": 1000.0 + i, "clicks": 10, "conversions": 0}
            for i in range(30)]
    rows.append({"campaign_id": "1", "placement": "cheap-vpn.app",
                 "cost": 5.0, "clicks": 2, "conversions": 0})
    assert blocklist_placement_candidates(rows, ["vpn"], top_n=30) == []
    # Тот же матч в топе — режется.
    rows.append({"campaign_id": "2", "placement": "cheap-vpn.app",
                 "cost": 50000.0, "clicks": 500, "conversions": 0})
    out = blocklist_placement_candidates(rows, ["vpn"], top_n=30)
    assert [c["placement"] for c in out] == ["cheap-vpn.app"]
    # Расход и кампании агрегированы по всем строкам площадки.
    assert out[0]["cost"] == 50005.0
    assert out[0]["campaigns"] == ["1", "2"]


def test_blocklist_matching_is_case_insensitive_and_empty_list_is_silent():
    from sync.agent.objects import blocklist_placement_candidates
    rows = [{"campaign_id": "1", "placement": "DSP-Opera-Exchange.yandex.ru",
             "cost": 1000.0, "clicks": 100, "conversions": 0}]
    out = blocklist_placement_candidates(rows, ["dsp-"], top_n=10)
    assert [c["placement"] for c in out] == ["dsp-opera-exchange.yandex.ru"]
    assert blocklist_placement_candidates(rows, [], top_n=10) == []
