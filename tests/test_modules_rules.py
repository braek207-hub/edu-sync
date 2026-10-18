# -*- coding: utf-8 -*-
"""Движок пользовательских правил: валидация, нормализация, поля × операторы, порядок."""

import pytest

from sync.modules import rules as R


def _rule(rid="aaaaaa", conds=(), action="block", window=7, **extra):
    raw = {"id": rid, "name": "Правило " + rid, "enabled": True, "action": action,
           "window": window, "where": "seen", "campaigns": {"only": [], "except": []},
           "conditions": [{"field": f, "op": o, "value": v} for f, o, v in conds],
           "created_at": "2026-10-18T10:00:00Z", "created_by": "u1",
           "updated_at": "2026-10-18T10:00:00Z"}
    raw.update(extra)
    return raw


def _qrule(rid="qqqqqq", conds=(), action="minus_phrase", **extra):
    raw = _rule(rid, conds, action, **extra)
    raw.pop("window")
    raw.pop("where")
    return raw


def _one(module, raw):
    rules, notes = R.compile_rules(module, [raw])
    assert notes == [], notes
    return rules


METRICS = {"clicks": 34.0, "cost": 1500.0, "impressions": 1200.0, "ctr": 2.83,
           "leads": 2.0, "cr": 5.88, "cpl": 750.0}
ACCOUNT = {"cr": 2.0, "cpl": 1000.0}


def _row(obj="play.google.com/game.puzzle", category="game", metrics=None, account=None,
         leads_known=True, money_known=True, campaign_id=1, scope="campaign"):
    return R.Row(object=obj, campaign_id=campaign_id, category=category,
                 metrics=dict(METRICS if metrics is None else metrics),
                 account=dict(ACCOUNT if account is None else account),
                 leads_known=leads_known, money_known=money_known, scope=scope)


def _eval(module, field, op, value, row):
    make = _rule if module == "placements" else _qrule
    action = "block" if module == "placements" else "minus_phrase"
    return R.evaluate(_one(module, make(conds=[(field, op, value)], action=action)), row)


def _assert_outcome(m, expected):
    # Ложь обязана быть чистым промахом (None), а не «не проверено»: иначе
    # неопределённая метрика молча держала бы находки вместо отказа.
    if expected:
        assert m is not None and m.rule is not None
        assert m.unverified == [] and m.held_by is None
    else:
        assert m is None


def _hit(module, field, op, value, row):
    m = _eval(module, field, op, value, row)
    return m is not None and m.rule is not None


P_CASES = [
    # (поле, оператор, значение, строка, ожидание)
    ("name", "contains", "game", _row(), True),
    ("name", "contains", "avito", _row(), False),
    ("name", "starts", "play.google", _row(), True),
    ("name", "starts", "google", _row(), False),
    ("name", "ends", "puzzle", _row(), True),
    ("name", "ends", "play", _row(), False),
    ("name", "equals", "Play.Google.com/game.puzzle/", _row(), True),
    ("name", "equals", "play.google.com", _row(), False),
    ("category", "is", "game", _row(), True),
    ("category", "is", "app", _row(), False),
    ("category", "is_not", "app", _row(), True),
    ("category", "is_not", "game", _row(), False),
    ("category", "is_not", "app", _row(category=None), False),
    ("clicks", "gte", 34, _row(), True),
    ("clicks", "gte", 35, _row(), False),
    ("clicks", "lte", 34, _row(), True),
    ("clicks", "lte", 33, _row(), False),
    ("clicks", "eq", 34, _row(), True),
    ("clicks", "eq", 33, _row(), False),
    ("cost", "gte", 1500, _row(), True),
    ("cost", "gte", 1500.01, _row(), False),
    ("cost", "lte", 1500, _row(), True),
    ("cost", "lte", 1499, _row(), False),
    ("impressions", "gte", 1000, _row(), True),
    ("impressions", "gte", 2000, _row(), False),
    ("impressions", "lte", 1200, _row(), True),
    ("impressions", "lte", 1199, _row(), False),
    ("ctr", "gte", 2.83, _row(), True),
    ("ctr", "gte", 3, _row(), False),
    ("ctr", "lte", 3, _row(), True),
    ("ctr", "lte", 2, _row(), False),
    ("leads", "gte", 2, _row(), True),
    ("leads", "gte", 3, _row(), False),
    ("leads", "lte", 2, _row(), True),
    ("leads", "lte", 1, _row(), False),
    ("leads", "eq", 2, _row(), True),
    ("leads", "eq", 0, _row(), False),
    ("cr", "gte", 5.88, _row(), True),
    ("cr", "gte", 6, _row(), False),
    ("cr", "lte", 6, _row(), True),
    ("cr", "lte", 5, _row(), False),
    # cr_x = 5.88 / 2 = 2.94
    ("cr_x", "gte", 2.94, _row(), True),
    ("cr_x", "gte", 3, _row(), False),
    ("cr_x", "lte", 3, _row(), True),
    ("cr_x", "lte", 2.9, _row(), False),
    ("cr_x", "gte", 0, _row(account={"cr": 0, "cpl": 1000.0}), False),
    ("cr_x", "lte", 100, _row(account={"cr": None, "cpl": 1000.0}), False),
    ("cpl", "gte", 750, _row(), True),
    ("cpl", "gte", 751, _row(), False),
    ("cpl", "lte", 750, _row(), True),
    ("cpl", "lte", 749, _row(), False),
    ("cpl", "lte", 100000, _row(metrics=dict(METRICS, leads=0.0, cpl=None)), False),
    # cpl_x = 750 / 1000 = 0.75
    ("cpl_x", "gte", 0.75, _row(), True),
    ("cpl_x", "gte", 0.76, _row(), False),
    ("cpl_x", "gte", 0, _row(account={"cr": 2.0, "cpl": None}), False),
    # Неопределённые метрики движок решает сам, что бы ни прислал вызывающий.
    ("cpl", "lte", 100, _row(metrics=dict(METRICS, leads=0.0, cpl=0.0)), False),
    ("cpl", "gte", 0, _row(metrics=dict(METRICS, leads=None, cpl=0.0)), False),
    ("cpl_x", "gte", 0, _row(metrics=dict(METRICS, leads=0.0, cpl=0.0)), False),
    ("cr", "lte", 100, _row(metrics=dict(METRICS, clicks=0.0, cr=0.0)), False),
    ("cr_x", "lte", 100, _row(metrics=dict(METRICS, clicks=0.0, cr=0.0)), False),
    ("ctr", "lte", 100, _row(metrics=dict(METRICS, impressions=0.0, ctr=0.0)), False),
    ("ctr", "lte", 100, _row(metrics=dict(METRICS, impressions=None, ctr=0.0)), False),
    ("leads", "eq", 0, _row(metrics=dict(METRICS, leads=0.0, cpl=None)), True),
]


@pytest.mark.parametrize("field,op,value,row,expected", P_CASES)
def test_placements_field_op(field, op, value, row, expected):
    _assert_outcome(_eval("placements", field, op, value, row), expected)


def _qrow(query, **kw):
    return _row(obj=query, category=None, **kw)


Q_CASES = [
    ("query", "has_word", "бесплатно", _qrow("курсы бесплатно онлайн"), True),
    ("query", "has_word", "бесплатн", _qrow("курсы бесплатно онлайн"), False),
    ("query", "has_word", "учеба", _qrow("учёба за рубежом"), True),
    ("query", "has_word", "учёба", _qrow("учеба за рубежом"), True),
    # Без основы: «учеба» не ловит «учебный».
    ("query", "has_word", "учеба", _qrow("учебный центр"), False),
    ("query", "has_phrase", "за рубежом", _qrow("учёба за рубежом бесплатно"), True),
    ("query", "has_phrase", "рубежом за", _qrow("учёба за рубежом бесплатно"), False),
    ("query", "has_phrase", "учеба рубежом", _qrow("учёба за рубежом"), False),
    ("query", "equals", "Учеба  за рубежом", _qrow("учёба за рубежом"), True),
    ("query", "equals", "учеба", _qrow("учёба за рубежом"), False),
    ("words", "gte", 3, _qrow("учёба за рубежом"), True),
    ("words", "gte", 4, _qrow("учёба за рубежом"), False),
    ("words", "lte", 3, _qrow("учёба за рубежом"), True),
    ("words", "lte", 2, _qrow("учёба за рубежом"), False),
    ("clicks", "eq", 34, _qrow("x"), True),
    ("cost", "lte", 1000, _qrow("x"), False),
    ("leads", "eq", 2, _qrow("x"), True),
    ("cpl_x", "gte", 0.5, _qrow("x"), True),
]


@pytest.mark.parametrize("field,op,value,row,expected", Q_CASES)
def test_queries_field_op(field, op, value, row, expected):
    _assert_outcome(_eval("queries", field, op, value, row), expected)


def test_eq_rounds_to_two_digits():
    row = _row(metrics=dict(METRICS, ctr=2.8333333))
    assert _hit("placements", "ctr", "gte", 2.83, row)
    rules = _one("placements", _rule(conds=[("cost", "gte", 1), ("clicks", "eq", 34.004)]))
    m = R.evaluate(rules, _row(metrics=dict(METRICS, clicks=34.0)))
    assert m.rule is not None


def test_values_normalized_on_compile():
    (r,) = _one("placements", _rule(conds=[("name", "contains", " ИгРЁ ")]))
    assert r.conditions[0].value == "игре"
    (q,) = _one("queries", _qrule(conds=[("query", "has_phrase", "Учёба   ЗА")]))
    assert q.conditions[0].value == "учеба за"
    assert _hit("placements", "name", "contains", "ИГРЁ", _row(obj="Мои-Игре.ru"))


def test_first_by_list_and_all_matched():
    raw = [_rule("aaaaa1", [("clicks", "gte", 100)]),
           _rule("aaaaa2", [("clicks", "gte", 10)], action="propose"),
           _rule("aaaaa3", [("name", "contains", "game")])]
    rules, notes = R.compile_rules("placements", raw)
    assert notes == [] and [r.id for r in rules] == ["aaaaa1", "aaaaa2", "aaaaa3"]
    m = R.evaluate(rules, _row())
    assert m.rule.id == "aaaaa2"
    assert [x["id"] for x in m.matched] == ["aaaaa2", "aaaaa3"]
    assert m.matched[0] == {"id": "aaaaa2", "name": "Правило aaaaa2",
                            "conds": [{"field": "clicks", "op": "gte", "value": 10,
                                       "actual": 34.0}]}
    assert m.unverified == []


def test_nothing_matched_is_none():
    rules = _one("placements", _rule(conds=[("clicks", "gte", 1000)]))
    assert R.evaluate(rules, _row()) is None


def test_leads_unknown_goes_unverified_other_rule_matches():
    raw = [_rule("lead01", [("name", "contains", "game"), ("leads", "eq", 0)]),
           _rule("click1", [("clicks", "gte", 10)])]
    rules, _ = R.compile_rules("placements", raw)
    m = R.evaluate(rules, _row(leads_known=False))
    assert m.unverified == ["lead01"]
    assert m.rule.id == "click1"
    assert [x["id"] for x in m.matched] == ["click1"]


def test_unknown_leads_with_false_condition_is_plain_miss():
    # Имя не совпало — правило не сработает при любых лидах, «не проверено» тут ложь.
    rules = _one("placements", _rule(conds=[("name", "contains", "avito"), ("leads", "eq", 0)]))
    assert R.evaluate(rules, _row(leads_known=False)) is None


def test_money_unknown_goes_unverified():
    rules, _ = R.compile_rules("placements", [
        _rule("money1", [("cost", "gte", 100)]),
        _rule("cplx01", [("cpl_x", "gte", 0.1)]),
        _rule("ctr001", [("ctr", "gte", 1)])])
    m = R.evaluate(rules, _row(money_known=False))
    assert m.unverified == ["money1", "cplx01"]
    assert m.rule.id == "ctr001"


def test_protect_beats_earlier_block():
    rules, _ = R.compile_rules("placements", [
        _rule("block1", [("clicks", "gte", 10)]),
        _rule("prot01", [("name", "contains", "game")], action="protect")])
    m = R.evaluate(rules, _row())
    assert (m.rule.id, m.held_by, m.unverified) == ("prot01", None, [])
    assert [x["id"] for x in m.matched] == ["block1", "prot01"]


def test_shape_matched():
    rules, _ = R.compile_rules("placements", [_rule("block1", [("clicks", "gte", 10)])])
    m = R.evaluate(rules, _row())
    assert (m.rule.id, m.held_by, m.unverified) == ("block1", None, [])


def test_shape_held_by_protect():
    rules, _ = R.compile_rules("placements", [
        _rule("block1", [("clicks", "gte", 10)]),
        _rule("lead01", [("leads", "eq", 0)]),
        _rule("prot01", [("leads", "gte", 1)], action="protect")])
    m = R.evaluate(rules, _row(leads_known=False))
    assert m.rule is None and m.held_by == "prot01"
    # unverified — только правила, чьи собственные условия не проверить.
    assert m.unverified == ["lead01", "prot01"]
    assert [x["id"] for x in m.matched] == ["block1"]


def test_shape_held_by_protect_without_any_match():
    # Держит строку и тогда, когда пользовательские не сработали: словарь и спам
    # по ней тоже не действуют (решает прогон по held_by).
    rules = _one("placements", _rule("prot01", [("cpl", "gte", 1)], action="protect"))
    m = R.evaluate(rules, _row(money_known=False))
    assert (m.rule, m.held_by, m.matched, m.unverified) == (None, "prot01", [], ["prot01"])


def test_shape_only_unverified():
    rules = _one("placements", _rule("lead01", [("leads", "eq", 0)]))
    m = R.evaluate(rules, _row(leads_known=False))
    assert (m.rule, m.held_by, m.matched, m.unverified) == (None, None, [], ["lead01"])


def test_matched_protect_beats_unverified_protect():
    rules, _ = R.compile_rules("placements", [
        _rule("prot01", [("leads", "gte", 1)], action="protect"),
        _rule("prot02", [("name", "contains", "game")], action="protect"),
        _rule("block1", [("clicks", "gte", 10)])])
    m = R.evaluate(rules, _row(leads_known=False))
    assert (m.rule.id, m.held_by, m.unverified) == ("prot02", None, ["prot01"])


def test_scope_account_rule_not_on_campaign_row():
    acc = _one("placements", _rule("acc001", [("clicks", "gte", 1)], where="account"))
    assert R.evaluate(acc, _row(scope="campaign")) is None
    assert R.evaluate(acc, _row(scope="account", campaign_id=0)).rule.id == "acc001"
    seen = _one("placements", _rule("seen01", [("clicks", "gte", 1)]))
    assert R.evaluate(seen, _row(scope="account", campaign_id=0)) is None


def test_scope_protect_applies_to_both():
    for where in ("seen", "account"):
        rules = _one("placements", _rule("prot01", [("name", "contains", "game")],
                                         action="protect", where=where))
        for scope in ("campaign", "account"):
            assert R.evaluate(rules, _row(scope=scope)).rule.id == "prot01"


def test_scope_queries_rules_are_campaign():
    rules = _one("queries", _qrule(conds=[("clicks", "gte", 1)]))
    assert R.evaluate(rules, _row(obj="x", scope="campaign")).rule is not None
    assert R.evaluate(rules, _row(obj="x", scope="account")) is None


def test_except_refuses_on_campaign_row_for_every_action():
    for action in ("block", "propose", "protect"):
        rules = _one("placements", _rule(conds=[("clicks", "gte", 1)], action=action,
                                         campaigns={"only": [], "except": [1]}))
        assert R.evaluate(rules, _row(campaign_id=1)) is None
        assert R.evaluate(rules, _row(campaign_id=2)).rule is not None


def test_campaign_filters():
    only = _rule(conds=[("clicks", "gte", 1)], campaigns={"only": [5, "7"], "except": []})
    exc = _rule(conds=[("clicks", "gte", 1)], campaigns={"only": [], "except": [1]})
    assert R.evaluate(_one("placements", only), _row(campaign_id=1)) is None
    assert R.evaluate(_one("placements", only), _row(campaign_id=7)).rule is not None
    assert R.evaluate(_one("placements", exc), _row(campaign_id=1)) is None
    assert R.evaluate(_one("placements", exc), _row(campaign_id=2)).rule is not None
    # where=account: строка по логину, кампании выбирает прогон.
    acc = dict(only, where="account")
    assert R.evaluate(_one("placements", acc),
                      _row(campaign_id=0, scope="account")).rule is not None


def _invalid(module, raw_list):
    rules, notes = R.compile_rules(module, raw_list)
    assert all(n["code"] == "rule_invalid" and n["detail"] for n in notes)
    return rules, notes


def test_window_zero_with_lead_conditions_invalid():
    for field in ("leads", "cr", "cr_x", "cpl", "cpl_x"):
        op = "gte"
        rules, notes = _invalid("placements", [_rule(conds=[(field, op, 1)], window=0)])
        assert rules == [] and notes[0]["rule_id"] == "aaaaaa"
    assert _one("placements", _rule(conds=[("clicks", "gte", 1)], window=0))


def test_disabled_skipped_silently():
    raw = [_rule("off001", [("clicks", "gte", 1)], enabled=False),
           _rule("on0001", [("clicks", "gte", 1)])]
    rules, notes = R.compile_rules("placements", raw)
    assert [r.id for r in rules] == ["on0001"] and notes == []


def test_rule_and_condition_limits():
    raw = [_rule("r%05d" % i, [("clicks", "gte", 1)]) for i in range(31)]
    rules, notes = _invalid("placements", raw)
    assert len(rules) == 30
    assert [n["rule_id"] for n in notes] == ["r00030"]
    six = [("clicks", "gte", 1)] * 6
    assert _one("placements", _rule(conds=six))
    rules, notes = _invalid("placements", [_rule(conds=six + [("cost", "gte", 1)])])
    assert rules == [] and len(notes) == 1


@pytest.mark.parametrize("module,raw", [
    ("placements", _rule(rid="ABC123", conds=[("clicks", "gte", 1)])),
    ("placements", _rule(rid="abc12", conds=[("clicks", "gte", 1)])),
    ("placements", _rule(conds=[("clicks", "gte", 1)], name="")),
    ("placements", _rule(conds=[("clicks", "gte", 1)], name="x" * 81)),
    ("placements", _rule(conds=[("clicks", "gte", 1)], action="minus_word")),
    ("placements", _rule(conds=[("clicks", "gte", 1)], window=14)),
    ("placements", _rule(conds=[("clicks", "gte", 1)], window=False)),
    ("placements", _rule(conds=[("clicks", "gte", 1)], where="all")),
    ("placements", _rule(conds=[("clicks", "gte", 1)], campaigns={"only": ["x"]})),
    ("placements", _rule(conds=[])),
    ("placements", _rule(conds=[("query", "has_word", "x")])),
    ("placements", _rule(conds=[("clicks", "contains", 1)])),
    ("placements", _rule(conds=[("cpl_x", "lte", 1)])),
    ("placements", _rule(conds=[("clicks", "gte", True)])),
    ("placements", _rule(conds=[("clicks", "gte", "10")])),
    ("placements", _rule(conds=[("clicks", "gte", -1)])),
    ("placements", _rule(conds=[("name", "contains", "  ")])),
    ("placements", _rule(conds=[("category", "is", "casino")])),
    ("placements", _rule(conds=[("clicks", "gte", 1)], enabled="yes")),
    ("placements", "не правило"),
    ("queries", _qrule(conds=[("impressions", "gte", 1)])),
    ("queries", _qrule(conds=[("name", "contains", "x")])),
    ("queries", _qrule(conds=[("query", "has_word", "два слова")])),
    ("queries", _qrule(conds=[("words", "gte", 2.5)])),
    ("queries", _qrule(conds=[("clicks", "gte", 3)], action="minus_word")),
    ("queries", _qrule(conds=[("clicks", "gte", 3)], action="propose_word")),
    ("queries", _qrule(conds=[("clicks", "gte", 3)], action="block")),
])
def test_invalid_rules(module, raw):
    rules, notes = _invalid(module, [raw])
    assert rules == [] and len(notes) == 1


def test_duplicate_id_invalid_both():
    raw = [_rule(conds=[("clicks", "gte", 1)]), _rule(conds=[("cost", "gte", 1)])]
    rules, notes = _invalid("placements", raw)
    assert rules == [] and [n["rule_id"] for n in notes] == ["aaaaaa", "aaaaaa"]


def test_duplicate_id_invalid_even_if_first_invalid():
    raw = [_rule(conds=[]), _rule(conds=[("cost", "gte", 1)]),
           _rule("other1", [("cost", "gte", 1)])]
    rules, notes = _invalid("placements", raw)
    assert [r.id for r in rules] == ["other1"]
    assert [n["rule_id"] for n in notes] == ["aaaaaa", "aaaaaa"]


def test_has_word_rejects_hyphenated():
    # Директ делит слово через дефис на два: минус-словом это не одно слово.
    rules, notes = _invalid("queries", [_qrule(conds=[("query", "has_word", "санкт-петербург")],
                                               action="minus_word")])
    assert rules == [] and len(notes) == 1
    assert _one("queries", _qrule(conds=[("query", "has_phrase", "санкт-петербург")]))


def test_queries_window_not_stored_and_minus_word_ok():
    raw = _qrule(conds=[("query", "has_word", "бесплатно"), ("clicks", "gte", 3),
                        ("leads", "eq", 0)], action="minus_word")
    # Окно и where у правил запросов не хранятся: окно одно — window_days модуля.
    raw.update(window=0, where="account")
    (r,) = _one("queries", raw)
    assert r.window is None and r.where is None


def test_snapshot():
    (r,) = _one("placements", _rule(conds=[("name", "contains", "Game"), ("clicks", "gte", 10),
                                           ("leads", "eq", 0)]))
    assert R.snapshot(r) == {
        "id": "aaaaaa", "name": "Правило aaaaaa", "action": "block", "window": 7,
        "where": "seen",
        "conditions": [{"field": "name", "op": "contains", "value": "game"},
                       {"field": "clicks", "op": "gte", "value": 10},
                       {"field": "leads", "op": "eq", "value": 0}]}


def test_where_defaults_to_seen():
    raw = _rule(conds=[("clicks", "gte", 1)])
    raw.pop("where")
    (r,) = _one("placements", raw)
    assert r.where == "seen"
