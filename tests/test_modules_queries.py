# -*- coding: utf-8 -*-
"""Модуль queries: окно и цели отчёта, пропуски, конвейер агента, запись и откат."""

from datetime import datetime, timezone

import pytest

from sync.modules import queries as qm
from sync.modules.accounts import DirectAccount
from sync.modules.defaults import settings_for
from sync.modules.types import Context, StoredFinding, fingerprint

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)
ACC = DirectAccount("login1", "tok", "RUB", "X")
GOAL = 111


def _ctx(settings=None, accounts=(ACC,), now=NOW):
    return Context(slug="edunetwork", module_key="queries", mode="report",
                   settings=settings_for("queries", settings),
                   accounts=list(accounts), now=now, log=lambda m: None)


def _camp(cid, goal=GOAL, state="ON", ctype="TEXT_CAMPAIGN", negatives=()):
    block = "UnifiedCampaign" if ctype == "UNIFIED_CAMPAIGN" else "TextCampaign"
    search = {"BiddingStrategyType": "HIGHEST_POSITION"}
    if goal:
        search = {"BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
                  "WbMaximumConversionRate": {"GoalId": goal}}
    return {"Id": cid, "Name": "К%d" % cid, "Type": ctype, "State": state,
            "NegativeKeywords": {"Items": list(negatives)} if negatives else None,
            block: {"BiddingStrategy": {"Search": search,
                                        "Network": {"BiddingStrategyType": "SERVING_OFF"}}}}


def _row(cid, query, clicks, cost, conv, goal=GOAL):
    return {"CampaignId": str(cid), "Query": query,
            "Clicks": str(clicks), "Cost": str(cost),
            "Conversions_%d_AUTO" % goal: str(conv) if conv else "--"}


def _base(cid=1, goal=GOAL):
    # Минус-слово «бесплатно» (3 фразы, 120 кл без конверсий) и минус-фраза «купить диплом»
    # (100 кл без конверсий): база 10/320, порог наблюдаемости 96 кл, CPA кабинета 3400 ₽.
    return [_row(cid, "высшее образование заочно", 100, 10000, 10, goal),
            _row(cid, "курсы бесплатно", 40, 4000, 0, goal),
            _row(cid, "бесплатно скачать", 40, 4000, 0, goal),
            _row(cid, "бесплатно онлайн", 40, 4000, 0, goal),
            _row(cid, "купить диплом", 100, 12000, 0, goal)]


class FakeDirect:
    """Директ в памяти: кампании, отчёт, ключи, списки минус-фраз."""

    def __init__(self, monkeypatch, campaigns, rows=(), keywords=None, drop=()):
        self.c = {c["Id"]: c for c in campaigns}
        self.rows = list(rows)
        self.keywords = keywords if keywords is not None else {}
        self.reports, self.writes, self.kw_calls = [], [], []
        self.drop = set(drop)  # фразы, которые Директ «принимает» и не сохраняет
        for name in ("get_all", "report_rows", "keywords_by_campaign",
                     "update_campaign_list", "read_campaign_list"):
            monkeypatch.setattr(qm.direct_api, name, getattr(self, name))

    def get_all(self, token, login, service, criteria, fields, extra=None, **kw):
        assert service == "campaigns"
        if "Ids" in criteria:
            return [self.c[i] for i in criteria["Ids"] if i in self.c]
        return list(self.c.values())

    def report_rows(self, token, login, params, *, prefix, **kw):
        self.reports.append(params)
        return self.rows

    def keywords_by_campaign(self, token, login, ids, **kw):
        self.kw_calls.append(list(ids))
        return {k: list(v) for k, v in self.keywords.items() if int(k) in ids}

    def update_campaign_list(self, token, login, cid, field, items):
        assert field == "NegativeKeywords"
        self.writes.append((cid, None if items is None else list(items)))
        self.c[cid][field] = (None if items is None
                              else {"Items": [i for i in items if i not in self.drop]})
        return {"Id": cid}

    def read_campaign_list(self, token, login, cid, field):
        return list((self.c[cid].get(field) or {}).get("Items") or [])


def _objects(out, cid=None):
    return {f.object_id for f in out.findings if cid is None or f.campaign_id == cid}


def test_window_ends_yesterday(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1)], _base())
    # 22:30 UTC 18.10 — уже 19.10 по Москве: «вчера» = 18.10
    qm.run(_ctx(now=datetime(2026, 10, 18, 22, 30, tzinfo=timezone.utc)))
    crit = fake.reports[-1]["SelectionCriteria"]
    assert (crit["DateFrom"], crit["DateTo"]) == ("2026-09-19", "2026-10-18")
    qm.run(_ctx({"window_days": 7}))
    crit = fake.reports[-1]["SelectionCriteria"]
    assert (crit["DateFrom"], crit["DateTo"]) == ("2026-10-11", "2026-10-17")
    params = fake.reports[-1]
    assert params["ReportType"] == "SEARCH_QUERY_PERFORMANCE_REPORT"
    assert params["DateRangeType"] == "CUSTOM_DATE" and params["IncludeVAT"] == "YES"
    assert {"Field": "Clicks", "Operator": "GREATER_THAN", "Values": ["0"]} in crit["Filter"]


def test_goals_from_settings_override_strategy(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, goal=GOAL)], _base(goal=541664134))
    out = qm.run(_ctx({"goal_ids": [541664134]}))
    assert fake.reports[0]["Goals"] == ["541664134"]
    assert {"бесплатно", "купить диплом"} <= _objects(out)


def test_strategy_goals_text_unified_and_priority(monkeypatch):
    unified = _camp(2, goal=222, ctype="UNIFIED_CAMPAIGN")
    placeholder = _camp(3, goal=13)
    placeholder["TextCampaign"]["PriorityGoals"] = {"Items": [{"GoalId": 333, "Value": 0}]}
    nogoal = _camp(4, goal=None)
    fake = FakeDirect(monkeypatch, [_camp(1), unified, placeholder, nogoal], _base())
    out = qm.run(_ctx())
    assert fake.reports[0]["Goals"] == ["111", "222", "333"]
    assert any("login1 4 " in s and "судить без конверсий нельзя" in s
               for s in out.summary["skipped"])


def test_blind_report_skips_account(monkeypatch):
    rows = [{k: v for k, v in r.items() if not k.startswith("Conversions_")} for r in _base()]
    FakeDirect(monkeypatch, [_camp(1)], rows)
    out = qm.run(_ctx())
    assert out.findings == []
    assert any(s.startswith("login1:") and "без столбцов конверсий" in s
               for s in out.summary["skipped"])
    assert out.scanned_logins == []


def test_campaign_with_zero_goal_conversions_skipped(monkeypatch):
    dead = [_row(2, "курсы бесплатно", 400, 40000, 0), _row(2, "купить диплом", 400, 40000, 0),
            _row(2, "бесплатно скачать", 400, 40000, 0), _row(2, "бесплатно онлайн", 400, 4000, 0)]
    FakeDirect(monkeypatch, [_camp(1), _camp(2)], _base() + dead)
    out = qm.run(_ctx())
    assert any("login1 2 " in s and "цель не дала конверсий" in s
               for s in out.summary["skipped"])
    assert _objects(out, 2) == set()
    assert {"бесплатно", "купить диплом"} <= _objects(out, 1)
    assert out.scanned_logins == ["login1"]


def test_findings_shape_and_fingerprint(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _base(), keywords={"1": ["высшее образование"]})
    out = qm.run(_ctx())
    by_obj = {f.object_id: f for f in out.findings}
    assert set(by_obj) == {"бесплатно", "купить диплом"}
    word, phrase = by_obj["бесплатно"], by_obj["купить диплом"]
    assert word.rule == "word_zero_conv" and phrase.rule == "phrase_waste"
    assert word.object_type == "phrase" and word.campaign_id == 1
    assert word.action == {"op": "add_negative", "campaign_id": 1, "phrase": "бесплатно"}
    assert word.details["clicks"] == 120 and word.details["cost"] == 12000
    assert word.details["conversions"] == 0
    assert (word.details["window_from"], word.details["window_to"]) == ("2026-09-18",
                                                                         "2026-10-17")
    assert sorted(word.details["queries_sample"]) == ["бесплатно онлайн", "бесплатно скачать",
                                                      "курсы бесплатно"]
    assert set(word.details) == {"clicks", "cost", "conversions", "window_from", "window_to",
                                 "queries_sample"}
    assert fingerprint("queries", word) == "queries:word_zero_conv:login1:1:бесплатно"


def test_own_keywords_protected_from_snapshot(monkeypatch):
    # matched_key у всех строк пуст (автотаргетинг): защиту даёт только снимок ключей
    fake = FakeDirect(monkeypatch, [_camp(1)], _base(), keywords={"1": ["Бесплатно курсы"]})
    out = qm.run(_ctx())
    assert "бесплатно" not in _objects(out)
    assert fake.kw_calls == [[1]]


def test_existing_negative_not_proposed_again(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1, negatives=["!бесплатно"])], _base())
    out = qm.run(_ctx())
    assert "бесплатно" not in _objects(out)
    assert "купить диплом" in _objects(out)


def test_word_with_any_conversion_not_minused(monkeypatch):
    # урок 25.08: «высшее» с конверсиями (226 шт.) агент предлагал отрезать целиком
    tail = [_row(1, "высшее дистанционно", 150, 20000, 0),
            _row(1, "высшее вечернее", 150, 20000, 0),
            _row(1, "колледж после 9", 100, 1000, 10)]
    fake = FakeDirect(monkeypatch, [_camp(1)], _base() + tail, keywords={"1": []})
    assert "высшее" not in _objects(qm.run(_ctx()))
    # контроль: те же деньги без конверсий у «высшее» — слово уходит в минус
    fake.rows = [_row(1, "высшее образование заочно", 100, 10000, 0)] + _base()[1:] + tail
    assert "высшее" in _objects(qm.run(_ctx()))


def test_stop_word_dictionary(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _base())
    out = qm.run(_ctx({"stop_words": ["Скачать"]}))
    stop = [f for f in out.findings if f.rule == "stop_word"]
    assert [f.object_id for f in stop] == ["скачать"]
    assert stop[0].details["clicks"] == 40


def test_never_minus_respected(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _base())
    out = qm.run(_ctx({"never_minus": ["бесплатно"], "stop_words": ["онлайн бесплатно"]}))
    assert not any("бесплатно" in o for o in _objects(out))
    assert "купить диплом" in _objects(out)
    assert any("never_minus" in r for r in out.summary["refused"])


def test_max_per_run_cap(monkeypatch):
    rows, stops = [], []
    for cid, n, top in ((1, 15, 1000), (2, 10, 900), (3, 5, 800)):
        rows.append(_row(cid, "хороший запрос", 10, 100, 5))
        for i in range(n):
            word = "junk%d%02d" % (cid, i)
            stops.append(word)
            rows.append(_row(cid, word, 1, top - i, 0))
    FakeDirect(monkeypatch, [_camp(1), _camp(2), _camp(3)], rows)
    out = qm.run(_ctx({"stop_words": stops}))
    assert len(stops) == 30
    assert len(out.findings) == 20
    costs = [f.details["cost"] for f in out.findings]
    assert costs == sorted(costs, reverse=True)
    per = {}
    for f in out.findings:
        per[f.campaign_id] = per.get(f.campaign_id, 0) + 1
    assert per == {1: 10, 2: 10}
    assert out.summary["over_cap"] == 10


def test_kzt_account_skipped(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1)], _base())
    kz = DirectAccount("lime-kz", "tok", "KZT", "LIME KZ")
    out = qm.run(_ctx(accounts=(kz, ACC)))
    assert {f.login for f in out.findings} == {"login1"}
    assert any(s.startswith("lime-kz:") and "KZT" in s for s in out.summary["skipped"])
    assert out.scanned_logins == ["login1"]
    assert len(fake.reports) == 1


def test_failed_login_not_scanned(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _base())
    real = qm._campaigns

    def flaky(acc):
        if acc.login == "bad":
            raise RuntimeError("503")
        return real(acc)

    monkeypatch.setattr(qm, "_campaigns", flaky)
    out = qm.run(_ctx(accounts=(DirectAccount("bad", "t"), ACC)))
    assert out.scanned_logins == ["login1"]
    assert out.summary["errors"] == ["bad: 503"]
    with pytest.raises(RuntimeError):
        qm.run(_ctx(accounts=(DirectAccount("bad", "t"),)))


def _stored(i, phrase, cid=1, rule="word_zero_conv"):
    return StoredFinding(
        id=i, status="approved", fingerprint="queries:%s:login1:%d:%s" % (rule, cid, phrase),
        last_seen_at=NOW, rule=rule, severity="info", title="t", login="login1",
        campaign_id=cid, campaign_name="К", object_type="phrase", object_id=phrase,
        details={}, action={"op": "add_negative", "campaign_id": cid, "phrase": phrase})


def test_apply_merges_reads_back_and_checks_limits(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, negatives=["Ручная фраза"])], drop={"мусор"})
    res = qm.apply(_ctx(), [_stored(1, "бесплатно"), _stored(2, "купить диплом"),
                            _stored(3, "мусор")])
    assert fake.writes == [(1, ["Ручная фраза", "бесплатно", "купить диплом", "мусор"])]
    assert res[1].ok and res[2].ok
    assert not res[3].ok and res[3].error == "Директ принял, но не применил"

    big = "я" * (20_000 - 5)
    fake = FakeDirect(monkeypatch, [_camp(1, negatives=[big])])
    res = qm.apply(_ctx(), [_stored(1, "купить диплом"), _stored(2, "фуфло")])
    assert not res[1].ok and "список минус-фраз заполнен" in res[1].error
    # короткая фраза влезла в остаток: лимит не превышен, запись одна
    assert res[2].ok and fake.writes == [(1, [big, "фуфло"])]

    fake = FakeDirect(monkeypatch, [_camp(1, negatives=[big + "яяяя"])])
    res = qm.apply(_ctx(), [_stored(1, "купить диплом")])
    assert not res[1].ok and "список минус-фраз заполнен" in res[1].error
    assert fake.writes == []


def test_apply_conflict_with_keyword_fails(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1)], keywords={"1": ["бесплатно курсы"]})
    res = qm.apply(_ctx(), [_stored(1, "бесплатно")])
    assert not res[1].ok and "конфликт с ключом" in res[1].error
    assert fake.writes == []


def test_apply_refuses_never_minus_and_foreign_phrase(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, negatives=["Купить  Диплом"])])
    res = qm.apply(_ctx({"never_minus": ["бесплатно"]}),
                   [_stored(1, "бесплатно"), _stored(2, "купить диплом")])
    assert not res[1].ok and "never_minus" in res[1].error
    assert not res[2].ok and "поставлена не модулем" in res[2].error
    assert fake.writes == []


def test_apply_campaign_off_or_invisible(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, state="SUSPENDED")])
    res = qm.apply(_ctx(), [_stored(1, "бесплатно"), _stored(2, "мусор", cid=9)])
    assert not res[1].ok and "кампания не запущена" in res[1].error
    assert not res[2].ok and "не видна API" in res[2].error
    assert fake.writes == []


def test_rollback_removes_only_own_phrase(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, negatives=["ручная", "бесплатно", "купить диплом"])])
    res = qm.rollback(_ctx(), [_stored(1, "бесплатно")])
    assert res[1].ok
    assert fake.writes == [(1, ["ручная", "купить диплом"])]
    # последняя фраза снимается null'ом, а не пустым Items
    res = qm.rollback(_ctx(), [_stored(2, "ручная"), _stored(3, "купить диплом")])
    assert res[2].ok and res[3].ok and fake.writes[-1] == (1, None)


def test_rollback_fails_when_phrase_stays(monkeypatch):
    class Stubborn(FakeDirect):
        def update_campaign_list(self, token, login, cid, field, items):
            self.writes.append((cid, list(items)))
            return {"Id": cid}  # Директ ответил успехом, список не изменил

    Stubborn(monkeypatch, [_camp(1, negatives=["бесплатно"])])
    res = qm.rollback(_ctx(), [_stored(1, "бесплатно")])
    assert not res[1].ok


def test_module_registered():
    from sync.modules.registry import MODULES
    assert MODULES["queries"] is qm



def test_report_request_shape(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(10), _camp(9), _camp(11, goal=None)], _base(cid=10))
    qm.run(_ctx())
    params = fake.reports[0]
    assert {"Field": "CampaignId", "Operator": "IN",
            "Values": ["9", "10"]} in params["SelectionCriteria"]["Filter"]
    assert params["AttributionModels"] == ["AUTO"]
    assert params["FieldNames"] == ["CampaignId", "Query", "Clicks", "Cost", "Conversions"]


def test_rollback_leaves_human_variant(monkeypatch):
    # наша «купить диплом» снята человеком, он поставил свой вариант в другом регистре
    fake = FakeDirect(monkeypatch, [_camp(1, negatives=["Ручная", "Купить Диплом"])])
    res = qm.rollback(_ctx(), [_stored(1, "купить диплом")])
    assert not res[1].ok and res[1].error == "вариант поставлен не модулем"
    assert fake.writes == []
    # наш и ручной вариант с двойным пробелом: снимается только наш
    fake = FakeDirect(monkeypatch, [_camp(1, negatives=["купить диплом", "Купить  диплом"])])
    res = qm.rollback(_ctx(), [_stored(1, "купить диплом")])
    assert res[1].ok and fake.writes == [(1, ["Купить  диплом"])]
    # своей фразы уже нет — снимать нечего, запись не нужна
    fake = FakeDirect(monkeypatch, [_camp(1, negatives=["ручная"])])
    res = qm.rollback(_ctx(), [_stored(1, "купить диплом")])
    assert res[1].ok and res[1].response["already_absent"] and fake.writes == []


def test_word_forms_protect_keyword(monkeypatch):
    rows = [_row(1, "высшее образование заочно", 100, 10000, 10),
            _row(1, "диплом бесплатно", 40, 4000, 0),
            _row(1, "диплом скачать", 40, 4000, 0),
            _row(1, "диплом онлайн", 40, 4000, 0)]
    FakeDirect(monkeypatch, [_camp(1)], rows, keywords={"1": ["дипломы купить"]})
    out = qm.run(_ctx())
    assert "диплом" not in _objects(out)
    assert any("«диплом»: конфликт с ключом" in r for r in out.summary["refused"])
    # контроль: без ключа слово уходит в минус
    FakeDirect(monkeypatch, [_camp(1)], rows, keywords={"1": []})
    assert "диплом" in _objects(qm.run(_ctx()))

    fake = FakeDirect(monkeypatch, [_camp(1)], keywords={"1": ["дипломы купить"]})
    res = qm.apply(_ctx(), [_stored(1, "диплом")])
    assert not res[1].ok and "конфликт с ключом" in res[1].error and fake.writes == []
    res = qm.apply(_ctx({"never_minus": ["Дипломы"]}), [_stored(1, "диплом")])
    assert not res[1].ok and "never_minus" in res[1].error


def test_word_forms_with_conversions_refused(monkeypatch):
    rows = [_row(1, "высшее образование заочно", 100, 10000, 10),
            _row(1, "дипломы бесплатно", 40, 4000, 0),
            _row(1, "дипломы скачать", 40, 4000, 0),
            _row(1, "дипломы онлайн", 40, 4000, 0)]
    FakeDirect(monkeypatch, [_camp(1)], rows, keywords={"1": []})
    assert "дипломы" in _objects(qm.run(_ctx()))
    FakeDirect(monkeypatch, [_camp(1)], rows + [_row(1, "диплом мгу", 5, 500, 1)],
               keywords={"1": []})
    out = qm.run(_ctx())
    assert "дипломы" not in _objects(out)
    assert any("«дипломы»: словоформы" in r for r in out.summary["refused"])


def test_multiword_conflict_direction(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1)], keywords={"1": ["купить диплом москва"]})
    res = qm.apply(_ctx(), [_stored(1, "купить диплом"), _stored(2, "купить дипломы дёшево")])
    # все слова минус-фразы есть в ключе — ключ погаснет
    assert not res[1].ok and "конфликт с ключом" in res[1].error
    # у минус-фразы есть слово, которого нет в ключе, — ключ живёт
    assert res[2].ok and fake.writes == [(1, ["купить дипломы дёшево"])]


def test_phrase_waste_judged_per_campaign(monkeypatch):
    # «курсы онлайн» в К1 окупается (CPA 1000), в К2 жжёт; общий CPA выше 2×потолка
    rows = [_row(1, "база один", 200, 20000, 20), _row(2, "база два", 200, 20000, 20),
            _row(1, "курсы онлайн", 30, 3000, 3), _row(2, "курсы онлайн", 400, 80000, 0)]
    FakeDirect(monkeypatch, [_camp(1), _camp(2)], rows, keywords={"1": [], "2": []})
    out = qm.run(_ctx({"cpa_limit": 1500}))
    assert [(f.campaign_id, f.object_id) for f in out.findings] == [(2, "курсы онлайн")]
    assert any(r.startswith("login1 1 «курсы онлайн»") and "окупается" in r
               for r in out.summary["refused"])


def test_write_timeout_after_accept_is_applied(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, negatives=["ручная"])])
    real = fake.update_campaign_list

    def accepted_then_timeout(token, login, cid, field, items):
        real(token, login, cid, field, items)
        raise TimeoutError("read timed out")

    monkeypatch.setattr(qm.direct_api, "update_campaign_list", accepted_then_timeout)
    res = qm.apply(_ctx(), [_stored(1, "бесплатно")])
    assert res[1].ok
    res = qm.rollback(_ctx(), [_stored(1, "бесплатно")])
    assert res[1].ok and fake.c[1]["NegativeKeywords"] == {"Items": ["ручная"]}

    def refused(token, login, cid, field, items):
        raise TimeoutError("connect timed out")

    monkeypatch.setattr(qm.direct_api, "update_campaign_list", refused)
    res = qm.apply(_ctx(), [_stored(1, "мусор")])
    assert not res[1].ok and res[1].error == "TimeoutError: connect timed out"

    monkeypatch.setattr(qm.direct_api, "update_campaign_list", real)
    monkeypatch.setattr(qm.direct_api, "read_campaign_list", lambda *a: None)
    res = qm.apply(_ctx(), [_stored(1, "мусор")])
    assert not res[1].ok and res[1].error == "кампания не найдена при чтении обратно"


def test_account_skip_keeps_campaign_reasons(monkeypatch):
    rows = [{k: v for k, v in r.items() if not k.startswith("Conversions_")} for r in _base()]
    FakeDirect(monkeypatch, [_camp(1), _camp(2, goal=None)], rows)
    skipped = qm.run(_ctx()).summary["skipped"]
    assert any("login1 2 " in s and "судить без конверсий" in s for s in skipped)
    assert any(s.startswith("login1:") and "без столбцов конверсий" in s for s in skipped)


def test_settings_guarded(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1)], _base(goal=541664134))
    out = qm.run(_ctx({"goal_ids": ["abc", 541664134, " 541664135", True]}))
    assert fake.reports[0]["Goals"] == ["541664134", "541664135"]
    assert len(out.summary["settings_notes"]) == 2
    for bad in ({"multiplier": 0.5}, {"multiplier": 11}, {"window_days": 3},
                {"max_per_run": 0}, {"cpa_limit": -1}):
        with pytest.raises(ValueError, match="неверные настройки"):
            qm.run(_ctx(bad))


def test_apply_duplicate_phrase_in_group(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1)])
    res = qm.apply(_ctx(), [_stored(1, "бесплатно"), _stored(2, "бесплатно", rule="stop_word")])
    assert res[1].ok
    assert not res[2].ok and "дубль" in res[2].error
    assert fake.writes == [(1, ["бесплатно"])]
    # ё=е: «учёба» и «учеба» — одна фраза для Директа, вторая запись была бы дублем
    fake = FakeDirect(monkeypatch, [_camp(1)], keywords={"1": []})
    res = qm.apply(_ctx(), [_stored(1, "учёба"), _stored(2, "учеба", rule="stop_word")])
    assert res[1].ok and not res[2].ok and "дубль" in res[2].error
    assert fake.writes == [(1, ["учёба"])]


def test_yo_keyword_protected(monkeypatch):
    # Директ не различает ё и е: минус «учеба» погасит ключ «учёба в колледже»
    rows = [_row(1, "высшее образование заочно", 100, 10000, 10),
            _row(1, "учеба бесплатно", 40, 4000, 0),
            _row(1, "учеба скачать", 40, 4000, 0),
            _row(1, "учеба онлайн", 40, 4000, 0)]
    FakeDirect(monkeypatch, [_camp(1)], rows, keywords={"1": ["учёба в колледже"]})
    out = qm.run(_ctx())
    assert "учеба" not in _objects(out)
    assert any("«учеба»: конфликт с ключом" in r for r in out.summary["refused"])
    FakeDirect(monkeypatch, [_camp(1)], rows, keywords={"1": []})
    assert "учеба" in _objects(qm.run(_ctx()))

    fake = FakeDirect(monkeypatch, [_camp(1)], keywords={"1": ["учёба в колледже"]})
    res = qm.apply(_ctx(), [_stored(1, "учеба")])
    assert not res[1].ok and "конфликт с ключом" in res[1].error and fake.writes == []
    fake = FakeDirect(monkeypatch, [_camp(1)], keywords={"1": []})
    res = qm.apply(_ctx({"never_minus": ["учёба"]}), [_stored(1, "учеба")])
    assert not res[1].ok and "never_minus" in res[1].error and fake.writes == []


def test_yo_existing_negative_and_variant(monkeypatch):
    # стоит «учёба» руками — «учеба» модуля это та же фраза: не ставим и не трогаем при откате
    rows = [_row(1, "высшее образование заочно", 100, 10000, 10),
            _row(1, "учеба бесплатно", 40, 4000, 0),
            _row(1, "учеба скачать", 40, 4000, 0),
            _row(1, "учеба онлайн", 40, 4000, 0)]
    FakeDirect(monkeypatch, [_camp(1, negatives=["учёба"])], rows, keywords={"1": []})
    assert "учеба" not in _objects(qm.run(_ctx()))
    fake = FakeDirect(monkeypatch, [_camp(1, negatives=["Учёба"])], keywords={"1": []})
    res = qm.apply(_ctx(), [_stored(1, "учеба")])
    assert not res[1].ok and "поставлена не модулем" in res[1].error and fake.writes == []
    res = qm.rollback(_ctx(), [_stored(1, "учеба")])
    assert not res[1].ok and res[1].error == "вариант поставлен не модулем"
    assert fake.writes == []


def test_stop_word_exact_form_not_stem(monkeypatch):
    # «скачать» и «скачки» делят 5 букв, но это разные слова: минус по скачкам лишний
    # К2: стоп-слова нет вовсе — ни находки, ни ложного отказа по ключу «скачать игры».
    # К3: оно есть — в деталях только точная форма (20 кл), скачки (30 кл) не в счёт.
    rows = _base() + [_row(2, "база два", 100, 1000, 10),
                      _row(2, "скачки на ипподроме", 30, 3000, 0),
                      _row(3, "база три", 100, 1000, 10),
                      _row(3, "скачать торрент", 20, 2000, 0),
                      _row(3, "скачки на ипподроме", 30, 3000, 0)]
    FakeDirect(monkeypatch, [_camp(1), _camp(2), _camp(3)], rows,
               keywords={"1": [], "2": ["скачать игры"], "3": []})
    out = qm.run(_ctx({"stop_words": ["скачать"]}))
    stop = {f.campaign_id: f.details["clicks"] for f in out.findings if f.rule == "stop_word"}
    assert stop == {1: 40, 3: 20}
    assert not any(r.startswith("login1 2 ") for r in out.summary["refused"])
    # ё=е и регистр при этом совпадают
    FakeDirect(monkeypatch, [_camp(1)], _base() + [_row(1, "учеба заочно", 20, 2000, 0)],
               keywords={"1": []})
    out = qm.run(_ctx({"stop_words": ["Учёба"]}))
    stop = [f for f in out.findings if f.rule == "stop_word"]
    assert [f.object_id for f in stop] == ["учёба"] and stop[0].details["clicks"] == 20
    # стоп-слово в обоих написаниях — одна находка на кампанию
    out = qm.run(_ctx({"stop_words": ["учёба", "учеба"]}))
    assert [f.object_id for f in out.findings if f.rule == "stop_word"] == ["учёба"]


def test_rollback_duplicate_in_group_already_absent(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, negatives=["ручная", "бесплатно"])])
    res = qm.rollback(_ctx(), [_stored(1, "бесплатно"),
                               _stored(2, "бесплатно", rule="stop_word")])
    assert res[1].ok and not res[1].response.get("already_absent")
    assert res[2].ok and res[2].response["already_absent"]
    assert fake.writes == [(1, ["ручная"])]
