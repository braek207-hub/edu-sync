# -*- coding: utf-8 -*-
"""Минус-ФРАЗА судится по тому, что реально отсекает, а не по себе одной.

Справка Директа: минус-фраза без операторов запрещает показ по ВСЕМ запросам,
содержащим все её слова, в любом порядке и с учётом словоформ. То есть
«университет синергия» гасит и «университет синергия красноярск», и
«синергия университет отзывы». Судить такую фразу по её собственной строке —
та же ошибка, что уже поймана на минус-СЛОВАХ (коммит 8717405): единица
суждения обязана совпадать с единицей действия.
"""

from sync.agent import objects


def _q(query, cost, clicks, conversions, campaign="1", matched_key=None):
    return {"query": query, "cost": cost, "clicks": clicks,
            "conversions": conversions, "campaign_id": campaign,
            "matched_key": matched_key}


def test_phrase_is_dropped_when_its_family_pays_off():
    # Сама фраза дорогая, но минус-фраза заберёт с собой хвост, который
    # окупается: суммарно поток в пределах допустимого — резать нечего.
    queries = [
        _q("университет синергия", 9000.0, 40, 1),
        _q("университет синергия красноярск", 1800.0, 26, 6),
        _q("университет синергия поступление", 1200.0, 20, 5),
    ]
    candidates = [{"query": "университет синергия", "cost": 9000.0,
                   "clicks": 40, "conversions": 1, "cpa": 9000.0,
                   "reason": "cpa_above_limit", "campaigns": ["1"],
                   "cost_by_campaign": {"1": 9000.0}}]

    kept, dropped = objects.phrases_cutting_only_waste(
        candidates, queries, cpa_limit=1700.0)

    assert kept == []
    assert dropped[0]["reason"] == "family_pays_off"
    assert dropped[0]["family"]["conversions"] == 12


def test_phrase_survives_when_the_whole_family_is_waste():
    queries = [
        _q("диплом купить срочно", 6000.0, 50, 0),
        _q("диплом купить срочно недорого", 3000.0, 25, 0),
    ]
    candidates = [{"query": "диплом купить срочно", "cost": 6000.0,
                   "clicks": 50, "conversions": 0, "cpa": 2000.0,
                   "reason": "zero_conversions", "campaigns": ["1"],
                   "cost_by_campaign": {"1": 6000.0}}]

    kept, dropped = objects.phrases_cutting_only_waste(
        candidates, queries, cpa_limit=1700.0)

    assert dropped == []
    assert kept[0]["query"] == "диплом купить срочно"


def test_family_is_matched_regardless_of_word_order():
    # Директ не требует порядка: «синергия университет отзывы» входит в
    # семейство «университет синергия» так же, как прямой порядок.
    queries = [
        _q("университет синергия", 9000.0, 40, 1),
        _q("синергия университет отзывы", 1500.0, 22, 6),
    ]
    candidates = [{"query": "университет синергия", "cost": 9000.0,
                   "clicks": 40, "conversions": 1, "cpa": 9000.0,
                   "reason": "cpa_above_limit", "campaigns": ["1"],
                   "cost_by_campaign": {"1": 9000.0}}]

    kept, _ = objects.phrases_cutting_only_waste(
        candidates, queries, cpa_limit=1700.0)

    assert kept == []


def test_own_keyword_is_never_minused():
    # Фраза, которую кабинет купил сам, снимается человеком, а не агентом:
    # запрет отменил бы собственную закупку. То же правило, что у слов
    # (core_words), — но на уровне фразы целиком.
    queries = [
        _q("мти институт", 9000.0, 40, 1, matched_key="мти институт"),
    ]
    candidates = [{"query": "мти институт", "cost": 9000.0, "clicks": 40,
                   "conversions": 1, "cpa": 9000.0,
                   "reason": "cpa_above_limit", "campaigns": ["1"],
                   "cost_by_campaign": {"1": 9000.0}}]

    kept, dropped = objects.phrases_cutting_only_waste(
        candidates, queries, cpa_limit=1700.0)

    assert kept == []
    assert dropped[0]["reason"] == "own_keyword"


def test_kept_candidate_carries_the_cost_of_everything_it_cuts():
    # Цена риска и обещанная экономия считаются по реально отсекаемому
    # потоку: иначе экспозиция занижена ровно на хвост.
    queries = [
        _q("диплом купить срочно", 6000.0, 50, 0, campaign="1"),
        _q("диплом купить срочно недорого", 3000.0, 25, 0, campaign="2"),
    ]
    candidates = [{"query": "диплом купить срочно", "cost": 6000.0,
                   "clicks": 50, "conversions": 0, "cpa": 2000.0,
                   "reason": "zero_conversions", "campaigns": ["1"],
                   "cost_by_campaign": {"1": 6000.0}}]

    kept, _ = objects.phrases_cutting_only_waste(
        candidates, queries, cpa_limit=1700.0)

    assert kept[0]["cost"] == 9000.0
    assert kept[0]["cost_by_campaign"] == {"1": 6000.0, "2": 3000.0}
    assert sorted(kept[0]["campaigns"]) == ["1", "2"]


def test_family_counts_short_words_too():
    # Порог длины слова (MIN_WORD_CHARS) существует для минус-СЛОВ. В
    # семействе он неприменим: «в», «на» участвуют в сопоставлении Директа,
    # и выбросить их значит посчитать семейство шире, чем оно есть, — а
    # значит зря отбраковать законного кандидата.
    queries = [
        _q("работа в вузе", 6000.0, 50, 0),
        _q("работа вузе преподавателем", 4000.0, 30, 9),
    ]
    candidates = [{"query": "работа в вузе", "cost": 6000.0, "clicks": 50,
                   "conversions": 0, "cpa": 2000.0,
                   "reason": "zero_conversions", "campaigns": ["1"],
                   "cost_by_campaign": {"1": 6000.0}}]

    kept, _ = objects.phrases_cutting_only_waste(
        candidates, queries, cpa_limit=1700.0)

    # «работа вузе преподавателем» не содержит «в» как отдельное слово,
    # поэтому в семейство не входит и конверсиями кандидата не спасает.
    assert kept and kept[0]["cost"] == 6000.0


def test_no_queries_means_no_verdict_change():
    # Пустой источник — не повод раздать индульгенции: без данных о
    # семействе кандидат не подтверждён, и рычаг молчит.
    candidates = [{"query": "диплом купить", "cost": 6000.0, "clicks": 50,
                   "conversions": 0, "cpa": 2000.0,
                   "reason": "zero_conversions", "campaigns": ["1"],
                   "cost_by_campaign": {"1": 6000.0}}]

    kept, dropped = objects.phrases_cutting_only_waste(
        candidates, [], cpa_limit=1700.0)

    assert kept == []
    assert dropped[0]["reason"] == "family_unknown"
