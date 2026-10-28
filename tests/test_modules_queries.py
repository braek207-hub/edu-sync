# -*- coding: utf-8 -*-
"""Модуль queries: окно и цели отчёта, пропуски, конвейер агента, запись и откат."""

import dataclasses
from datetime import datetime, timezone

import pytest

from sync.modules import queries as qm
from sync.modules.accounts import DirectAccount
from sync.modules.defaults import settings_for
from sync.modules.types import Context, StoredFinding, fingerprint
from sync.placements.direct import DirectError

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
    return {"CampaignId": str(cid), "Query": query, "Impressions": str(clicks * 10),
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
    out = qm.run(_ctx({"lead_goal_ids": [541664134]}))
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
    # Ключи этапа 1 на месте; этап 2 дописывает снимок правила, окно и метрики.
    assert set(word.details) == {"clicks", "cost", "conversions", "window_from", "window_to",
                                 "queries_sample", "rule", "matched", "window", "metrics"}
    assert word.details["rule"] == {"id": "word_zero_conv", "name": "Слово без конверсий",
                                    "builtin": True,
                                    "params": {"cpa_limit": 3400.0, "cpa_auto": True,
                                               "multiplier": 2.0}}
    assert word.details["matched"] == [{"id": "word_zero_conv", "name": "Слово без конверсий",
                                        "conds": []}]
    assert word.details["window"] == {"from": "2026-09-18", "to": "2026-10-17"}
    assert word.details["metrics"] == {"clicks": 120, "cost": 12000.0, "impressions": 1200,
                                       "leads": 0, "cr": 0.0, "cpl": None,
                                       "account_cr": 3.12, "account_cpl": 3400.0}
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
    assert params["FieldNames"] == ["CampaignId", "Query", "Impressions", "Clicks", "Cost",
                                   "Conversions"]


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
    assert not res[1].ok and res[1].error == "кампания не найдена в Директе после записи"


def test_account_skip_keeps_campaign_reasons(monkeypatch):
    rows = [{k: v for k, v in r.items() if not k.startswith("Conversions_")} for r in _base()]
    FakeDirect(monkeypatch, [_camp(1), _camp(2, goal=None)], rows)
    skipped = qm.run(_ctx()).summary["skipped"]
    assert any("login1 2 " in s and "судить без конверсий" in s for s in skipped)
    assert any(s.startswith("login1:") and "без столбцов конверсий" in s for s in skipped)


def test_settings_guarded(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1)], _base(goal=541664134))
    out = qm.run(_ctx({"lead_goal_ids": ["abc", 541664134, " 541664135", True]}))
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


def test_query_rows_carry_impressions_and_per_goal_conversions():
    raw = _base() + [_row(1, "высшее образование заочно", 10, 1000, 2)]
    rows = {q["query"]: q for q in qm._query_rows(raw, {"1": [GOAL]})}
    top = rows["высшее образование заочно"]
    assert top["impressions"] == 1100 and top["clicks"] == 110
    assert top["conversions"] == 12 and top["goal_conversions"] == {GOAL: 12}
    assert rows["купить диплом"]["goal_conversions"] == {GOAL: 0}
    # Без колонки показов (старый отчёт) — ноль, а не падение.
    bare = [{k: v for k, v in r.items() if k != "Impressions"} for r in _base()]
    assert all(q["impressions"] == 0 for q in qm._query_rows(bare, {"1": [GOAL]}))


# --- этап 2: правила пользователя и переключатели встроенных ---------------------------

def _rule(rid, action, *conds, **extra):
    item = {"id": rid, "name": "Правило %s" % rid, "action": action,
            "conditions": [{"field": f, "op": o, "value": v} for f, o, v in conds]}
    item.update(extra)
    return item


FREE = ("query", "has_word", "бесплатно")
CLICKS3 = ("clicks", "gte", 3)
NO_LEADS = ("leads", "eq", 0)


def _small(cid=1, goal=GOAL):
    # Хвост мелкий: встроенным судить нечего (порог наблюдаемости), правилу — есть.
    return [_row(cid, "высшее образование заочно", 100, 10000, 10, goal),
            _row(cid, "курсы бесплатно", 3, 300, 0, goal),
            _row(cid, "бесплатно онлайн", 2, 200, 0, goal),
            _row(cid, "купить диплом дешево", 4, 400, 0, goal)]


def _codes(out, code=None):
    items = [r for r in out.summary["refused"] if isinstance(r, dict)]
    return [r for r in items if code is None or r["code"] == code]


def test_user_minus_word_finding(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _small(), keywords={"1": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL],
                       "rules": [_rule("free01", "minus_word", FREE, CLICKS3, NO_LEADS)]}))
    assert [(f.rule, f.object_id) for f in out.findings] == [("u:free01", "бесплатно")]
    f = out.findings[0]
    assert fingerprint("queries", f) == "queries:u:free01:login1:1:бесплатно"
    assert f.action == {"op": "add_negative", "campaign_id": 1, "phrase": "бесплатно"}
    assert f.title == "Минус-слово «бесплатно» в кампании 1"
    d = f.details
    assert d["rule"] == {"id": "u:free01", "name": "Правило free01", "action": "minus_word",
                         "window": None, "where": None,
                         "campaigns": {"only": [], "except": []},
                         "conditions": [{"field": "query", "op": "has_word",
                                         "value": "бесплатно"},
                                        {"field": "clicks", "op": "gte", "value": 3},
                                        {"field": "leads", "op": "eq", "value": 0}]}
    assert d["matched"] == [{"id": "u:free01", "name": "Правило free01", "conds": [
        {"field": "query", "op": "has_word", "value": "бесплатно", "actual": "курсы бесплатно"},
        {"field": "clicks", "op": "gte", "value": 3, "actual": 5.0},
        {"field": "leads", "op": "eq", "value": 0, "actual": 0.0}]}]
    # Текст сработал на «курсы бесплатно», метрики — поток семьи: оба запроса со словом.
    assert (d["clicks"], d["cost"], d["conversions"]) == (5, 500.0, 0)
    assert d["queries_sample"] == ["курсы бесплатно", "бесплатно онлайн"]
    assert d["query"] == "курсы бесплатно"
    assert d["window"] == {"from": "2026-09-18", "to": "2026-10-17"}
    assert (d["window_from"], d["window_to"]) == ("2026-09-18", "2026-10-17")
    assert d["metrics"] == {"clicks": 5, "cost": 500.0, "impressions": 50, "leads": 0,
                            "cr": 0.0, "cpl": None, "account_cr": 9.17, "account_cpl": 1090.0}
    assert (d["metrics_scope"], d["family_queries"]) == ("family", 2)
    assert "propose" not in d
    assert out.summary["by_rule"]["u:free01"] == {"found": 1, "refused": 0, "over_cap": 0}
    assert out.summary["read"] == {"logins": 1, "campaigns": 1, "objects": 4}
    assert out.unverified == []


def test_user_rule_in_never_minus(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _small(), keywords={"1": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "never_minus": ["Бесплатно"],
                       "rules": [_rule("free01", "minus_word", FREE, CLICKS3, NO_LEADS)]}))
    assert out.findings == []
    assert _codes(out) == [{"code": "in_never_minus", "object": "бесплатно", "campaign_id": 1,
                            "rule": "u:free01", "login": "login1"}]
    assert out.summary["by_rule"]["u:free01"]["refused"] == 1


def test_user_rule_conflicts_keyword(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1)], _small(), keywords={"1": ["Бесплатные курсы"]})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL],
                       "rules": [_rule("free01", "minus_word", FREE, CLICKS3, NO_LEADS)]}))
    assert out.findings == []
    assert [(r["code"], r["object"]) for r in _codes(out)] == [("conflicts_keyword",
                                                                "бесплатно")]
    assert fake.kw_calls == [[1]]


def test_user_rule_covered_by_minus(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1, negatives=["бесплатно"])], _small(), keywords={"1": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL],
                       "rules": [_rule("free01", "minus_word", FREE, CLICKS3, NO_LEADS)]}))
    assert out.findings == []
    assert [r["code"] for r in _codes(out)] == ["covered_by_minus"]


def test_user_minus_phrase_is_query(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _small(), keywords={"1": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
        _rule("dipl01", "minus_phrase", ("query", "has_word", "диплом"), CLICKS3, NO_LEADS)]}))
    assert [(f.rule, f.object_id) for f in out.findings] == [("u:dipl01",
                                                              "купить диплом дешево")]
    assert out.findings[0].title == "Минус-фраза «купить диплом дешево» в кампании 1"
    assert out.findings[0].details["query"] == "купить диплом дешево"


def test_user_propose_marked(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _small(), keywords={"1": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
        _rule("free01", "propose_word", FREE, CLICKS3),
        _rule("dipl01", "propose_phrase", ("query", "has_word", "диплом"), CLICKS3)]}))
    assert {f.object_id: f.details.get("propose") for f in out.findings} == {
        "бесплатно": True, "купить диплом дешево": True}


def test_no_lead_goals_user_rule_unverified_builtins_by_strategy(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1)], _base(), keywords={"1": []})
    out = qm.run(_ctx({"rules": [_rule("free01", "minus_word", FREE, CLICKS3, NO_LEADS)]}))
    # Встроенные — по цели стратегии, как в этапе 1.
    assert fake.reports[0]["Goals"] == [str(GOAL)]
    assert {(f.rule, f.object_id) for f in out.findings} == {
        ("word_zero_conv", "бесплатно"), ("phrase_waste", "купить диплом")}
    assert ("login1", "u:free01") in out.unverified
    assert _codes(out) == [{"code": "no_lead_goals", "object": None, "campaign_id": None,
                            "rule": "u:free01", "login": "login1"}]
    # Правило без условий по лидам целей-лидов не ждёт.
    out = qm.run(_ctx({"rules": [_rule("free02", "minus_word", FREE, CLICKS3)]}))
    assert out.unverified == [] and _codes(out) == []


def test_builtin_phrase_waste_off(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _base(), keywords={"1": []})
    out = qm.run(_ctx({"builtin": {"phrase_waste": False}}))
    assert {(f.rule, f.object_id) for f in out.findings} == {("word_zero_conv", "бесплатно")}
    assert out.unverified == [("login1", "phrase_waste")]
    assert out.summary["disabled"] == ["phrase_waste"]
    out = qm.run(_ctx({"builtin": {"phrase_waste": False, "word_zero_conv": False}}))
    assert out.findings == []
    assert out.unverified == [("login1", "phrase_waste"), ("login1", "word_zero_conv")]


def test_has_word_yo_equals_e(monkeypatch):
    rows = [_row(1, "высшее образование заочно", 100, 10000, 10),
            _row(1, "учёба бесплатно", 4, 400, 0), _row(1, "учеба онлайн", 3, 300, 0)]
    FakeDirect(monkeypatch, [_camp(1)], rows, keywords={"1": []})
    for word in ("учёба", "учеба"):
        out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
            _rule("study1", "minus_word", ("query", "has_word", word), CLICKS3)]}))
        # Пишется написание запроса, где сработало правило, а не значение условия.
        assert [f.object_id for f in out.findings] == ["учёба"]
        # «учеба» и «учёба» — один поток под одним минус-словом.
        assert out.findings[0].details["clicks"] == 7


def test_user_rule_merges_into_builtin(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _base(), keywords={"1": []})
    out = qm.run(_ctx({"rules": [_rule("free01", "minus_word", FREE, CLICKS3)]}))
    word = {f.object_id: f for f in out.findings}["бесплатно"]
    assert word.rule == "word_zero_conv"
    assert [m["id"] for m in word.details["matched"]] == ["word_zero_conv", "u:free01"]
    assert not any(f.rule.startswith("u:") for f in out.findings)


def test_user_protect_beats_builtin(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _base(), keywords={"1": []})
    out = qm.run(_ctx({"rules": [_rule("prot01", "protect", ("query", "has_word", "диплом"))]}))
    assert "купить диплом" not in _objects(out) and "бесплатно" in _objects(out)
    assert _codes(out, "user_protect") == [{
        "code": "user_protect", "object": "купить диплом", "campaign_id": 1,
        "rule": "phrase_waste", "login": "login1", "by": "u:prot01"}]
    assert out.summary["by_rule"]["u:prot01"]["found"] == 1
    assert out.summary["by_rule"]["phrase_waste"]["refused"] == 1
    # Защита по словоформе: минус «диплом» отрезал бы и запрос «дипломы …».
    rows = [_row(1, "высшее образование заочно", 100, 10000, 10),
            _row(1, "диплом бесплатно", 40, 4000, 0), _row(1, "диплом скачать", 40, 4000, 0),
            _row(1, "диплом онлайн", 40, 4000, 0), _row(1, "дипломы мгу", 1, 10, 0)]
    FakeDirect(monkeypatch, [_camp(1)], rows, keywords={"1": []})
    assert "диплом" in _objects(qm.run(_ctx()))
    out = qm.run(_ctx({"rules": [_rule("prot01", "protect", ("query", "has_word", "дипломы"))]}))
    assert "диплом" not in _objects(out)
    assert [r["object"] for r in _codes(out, "user_protect")] == ["диплом"]


def test_unverifiable_protect_holds(monkeypatch):
    # Защита с условием по лидам без целей-лидов: не проверить — значит держать.
    FakeDirect(monkeypatch, [_camp(1)], _base(), keywords={"1": []})
    out = qm.run(_ctx({"rules": [_rule("prot01", "protect", ("query", "has_word", "диплом"),
                                       ("leads", "gte", 1))]}))
    assert "купить диплом" not in _objects(out)
    assert {"code": "no_lead_goals", "object": "купить диплом", "campaign_id": 1,
            "rule": "phrase_waste", "login": "login1", "by": "u:prot01",
            "held": True} in _codes(out, "no_lead_goals")
    assert ("login1", "phrase_waste", 1) in out.unverified
    # Защита сама ничего не закрывает: её пары в unverified нет.
    assert ("login1", "u:prot01") not in out.unverified


def test_user_goal_not_visible_triple(monkeypatch):
    rows = _small() + [_row(2, "курсы бесплатно", 30, 3000, 0)]
    FakeDirect(monkeypatch, [_camp(1), _camp(2)], rows, keywords={"1": [], "2": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL],
                       "rules": [_rule("free01", "minus_word", FREE, CLICKS3, NO_LEADS)]}))
    assert [(f.campaign_id, f.object_id) for f in out.findings] == [(1, "бесплатно")]
    assert out.unverified == [("login1", k, 2)
                              for k in ("phrase_waste", "word_zero_conv", "stop_word")] + [
        ("login1", "u:free01", 2)]
    assert _codes(out, "goal_not_visible") == [{
        "code": "goal_not_visible", "object": None, "campaign_id": 2, "rule": "u:free01",
        "login": "login1", "cost": 3000.0}]


def test_user_rule_sees_campaign_without_strategy_goal(monkeypatch):
    # (b) Встроенные кампанию без цели пропускают, как в этапе 1; правило её судит.
    rows = _small() + [_row(4, "курсы бесплатно", 5, 500, 0)]
    fake = FakeDirect(monkeypatch, [_camp(1), _camp(4, goal=None)], rows,
                      keywords={"1": [], "4": []})
    out = qm.run(_ctx({"rules": [_rule("free01", "minus_phrase", FREE, CLICKS3)]}))
    assert any("login1 4 " in s and "судить без конверсий нельзя" in s
               for s in out.summary["skipped"])
    assert {(f.campaign_id, f.object_id) for f in out.findings} == {
        (1, "курсы бесплатно"), (4, "курсы бесплатно")}
    crit = fake.reports[0]["SelectionCriteria"]["Filter"]
    assert {"Field": "CampaignId", "Operator": "IN", "Values": ["1", "4"]} in crit
    # Цели-лиды проекта: встроенные и правила считают по ним и в кампании без цели стратегии.
    fake.reports.clear()
    ctx = _ctx({"rules": [_rule("free01", "minus_word", FREE, CLICKS3, NO_LEADS)]})
    ctx.project_goals = [GOAL]
    out = qm.run(ctx)
    assert fake.reports[0]["Goals"] == [str(GOAL)]
    assert not any("login1 4 " in s and "судить без конверсий" in s
                   for s in out.summary["skipped"])
    assert ("login1", "u:free01", 4) in out.unverified  # 500 ₽ и ноль лидов: цель не видна
    assert (1, "бесплатно") in {(f.campaign_id, f.object_id) for f in out.findings}


def test_report_goals_lead_first_and_no_silent_cut():
    # Цели-лиды входят целиком; места остаются стратегиям по возрастанию id.
    goals, dropped = qm._report_goals({"1": [500, 600], "2": [700], "3": [5]},
                                      [1, 2, 3, 4, 5, 6, 7, 8, 9])
    assert goals == [1, 2, 3, 4, 5, 6, 7, 8, 9, 500]
    assert dropped == ["1", "2"]
    assert qm._report_goals({"1": [3]}, []) == ([3], [])
    # Без целей-лидов — ровно этап 1: первые десять по id.
    goals, dropped = qm._report_goals({str(i): [100 + i] for i in range(1, 12)}, [])
    assert goals == list(range(101, 111)) and dropped == ["11"]


def test_too_many_strategy_goals_refused_explicitly(monkeypatch):
    camps = [_camp(i, goal=100 + i) for i in range(1, 12)]
    rows = []
    for i in range(1, 12):
        rows += [_row(i, "база %d" % i, 50, 5000, 5, goal=100 + i),
                 _row(i, "курсы бесплатно", 4, 400, 0, goal=100 + i)]
    # В отчёте Директа столбцы всех целей есть у каждой строки: без этого кампания чужой
    # цели выглядела бы «целью без конверсий».
    for r in rows:
        for g in range(101, 112):
            r.setdefault("Conversions_%d_AUTO" % g, "--")
    fake = FakeDirect(monkeypatch, camps, rows, keywords={str(i): [] for i in range(1, 12)})
    out = qm.run(_ctx())
    assert fake.reports[0]["Goals"] == [str(g) for g in range(101, 111)]
    assert any("login1 11 " in s and "не вошли в отчёт" in s for s in out.summary["skipped"])
    assert _codes(out) == [{"code": "report_goals_over_cap", "object": None,
                            "campaign_id": 11, "rule": None, "login": "login1"}]
    # Встроенные кампанию 11 не судили: их прошлые находки в ней остаются открытыми.
    assert out.unverified == [("login1", k, 11)
                              for k in ("phrase_waste", "word_zero_conv", "stop_word")]
    # Правило без лидов судит и кампанию, чьи цели стратегии не поместились.
    out = qm.run(_ctx({"max_per_run": 100,
                       "rules": [_rule("free01", "minus_phrase", FREE, CLICKS3)]}))
    assert 11 in {f.campaign_id for f in out.findings if f.rule == "u:free01"}
    assert "11" in fake.reports[-1]["SelectionCriteria"]["Filter"][1]["Values"]


def test_report_without_goals_when_none_needed(monkeypatch):
    rows = [{k: v for k, v in r.items() if not k.startswith("Conversions_")} for r in _small()]
    fake = FakeDirect(monkeypatch, [_camp(1, goal=None)], rows, keywords={"1": []})
    out = qm.run(_ctx({"rules": [_rule("free01", "minus_phrase", FREE, CLICKS3)]}))
    params = fake.reports[0]
    assert "Goals" not in params and "AttributionModels" not in params
    assert params["FieldNames"] == ["CampaignId", "Query", "Impressions", "Clicks", "Cost"]
    assert [f.object_id for f in out.findings] == ["курсы бесплатно"]
    assert out.scanned_logins == ["login1"]


def test_blind_report_with_user_rules(monkeypatch):
    rows = [{k: v for k, v in r.items() if not k.startswith("Conversions_")} for r in _small()]
    FakeDirect(monkeypatch, [_camp(1)], rows, keywords={"1": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
        _rule("free01", "minus_phrase", FREE, CLICKS3),
        _rule("free02", "minus_word", FREE, NO_LEADS)]}))
    assert any(s.startswith("login1:") and "без столбцов конверсий" in s
               for s in out.summary["skipped"])
    # Встроенные не судили — их находки держатся; правило без лидов работает.
    assert {("login1", k) for k in ("phrase_waste", "word_zero_conv", "stop_word")} \
        <= set(out.unverified)
    assert [f.rule for f in out.findings] == ["u:free01"]
    assert ("login1", "u:free02") in out.unverified
    assert [r["code"] for r in _codes(out) if r["rule"] == "u:free02"] == ["report_failed"]
    assert out.scanned_logins == ["login1"]


def test_kzt_account_with_user_rules(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _small(), keywords={"1": []})
    kz = DirectAccount("lime-kz", "tok", "KZT", "LIME KZ")
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
        _rule("free01", "minus_word", FREE, CLICKS3),
        _rule("cost01", "minus_word", FREE, ("cost", "gte", 100))]}, accounts=(kz,)))
    assert [f.rule for f in out.findings] == ["u:free01"]
    assert "lime-kz: кабинет в KZT, пороги модуля в рублях" in out.summary["skipped"]
    assert ("lime-kz", "phrase_waste") in out.unverified
    assert ("lime-kz", "u:cost01") in out.unverified
    assert [r["code"] for r in _codes(out)] == ["non_rub_money"]
    assert out.scanned_logins == ["lime-kz"]


def test_user_rules_share_cap_after_builtins(monkeypatch):
    rows, stops = [_row(1, "хороший запрос", 10, 100, 5)], []
    for i in range(3):
        stops.append("junk%d" % i)
        rows.append(_row(1, "junk%d" % i, 1, 900 - i, 0))
    for i in range(4):
        rows.append(_row(1, "курсы бесплатно %d" % i, 3, 300 - i, 0))
    FakeDirect(monkeypatch, [_camp(1)], rows, keywords={"1": []})
    out = qm.run(_ctx({"stop_words": stops, "max_per_run": 5, "rules": [
        _rule("free01", "minus_phrase", FREE, CLICKS3)]}))
    # Встроенные первыми (3), правилу осталось 2 места из 4 — дорогие первыми.
    assert [f.rule for f in out.findings] == ["stop_word"] * 3 + ["u:free01"] * 2
    assert [f.object_id for f in out.findings[3:]] == ["курсы бесплатно 0",
                                                      "курсы бесплатно 1"]
    assert out.summary["over_cap"] == 2
    assert out.summary["by_rule"]["u:free01"] == {"found": 2, "refused": 0, "over_cap": 2}


def test_first_rule_owns_phrase_others_matched(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _small(), keywords={"1": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
        _rule("first1", "propose_word", FREE, ("clicks", "gte", 3)),
        _rule("secnd1", "minus_word", FREE, ("clicks", "gte", 2))]}))
    f, = out.findings
    assert f.rule == "u:first1" and f.details["propose"] is True
    assert [m["id"] for m in f.details["matched"]] == ["u:first1", "u:secnd1"]


def test_invalid_and_disabled_rules_silent(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _small(), keywords={"1": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
        _rule("bad001", "minus_word", CLICKS3),
        _rule("off001", "minus_word", FREE, CLICKS3, enabled=False)]}))
    assert out.findings == []
    assert out.summary["disabled"] == ["u:bad001", "u:off001"]
    assert set(out.unverified) == {("login1", "u:bad001"), ("login1", "u:off001")}
    assert [(r["code"], r["rule"]) for r in _codes(out)] == [("rule_invalid", "u:bad001")]


def test_invalid_phrase_refused_with_code(monkeypatch):
    rows = _small() + [_row(1, "курсы " + " ".join("слово%d" % i for i in range(8)), 5, 500, 0)]
    FakeDirect(monkeypatch, [_camp(1)], rows, keywords={"1": []})
    out = qm.run(_ctx({"rules": [_rule("long01", "minus_phrase", ("words", "gte", 8))]}))
    assert out.findings == []
    ref, = _codes(out)
    assert ref["code"] == "phrase_invalid" and ref["rule"] == "u:long01"
    assert "слов при пределе" in ref["detail"]


def test_progress_reported(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _base())
    real = qm._campaigns

    def flaky(acc):
        if acc.login == "bad":
            raise RuntimeError("503")
        return real(acc)

    monkeypatch.setattr(qm, "_campaigns", flaky)
    seen = []
    ctx = _ctx(accounts=(DirectAccount("bad", "t"), ACC))
    ctx.progress = seen.append
    qm.run(ctx)
    assert seen == [{"step": "bad", "logins_done": 1, "logins_total": 2},
                    {"step": "login1", "logins_done": 2, "logins_total": 2}]


# --- раунд правок 1 ---------------------------------------------------------------------

def test_family_with_leads_not_cut(monkeypatch):
    # Пробы ревью: «leads = 0» судится на всём, что отрежет минус, а не на одной строке.
    rows = [_row(1, "высшее образование заочно", 100, 10000, 10),
            _row(1, "бесплатно", 6, 600, 0),
            _row(1, "курсы бесплатно", 3, 300, 0),
            _row(1, "бесплатно онлайн обучение", 20, 2000, 9),
            _row(1, "бесплатные курсы английского онлайн", 30, 3000, 12)]
    FakeDirect(monkeypatch, [_camp(1)], rows, keywords={"1": []})
    # Минус-фраза «бесплатно» из однословного запроса — это минус-слово: 9 лидов в точной
    # форме и 21 со словоформами.
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "builtin": {"phrase_waste": False,
                                                             "word_zero_conv": False},
                       "rules": [_rule("free02", "minus_phrase", FREE, CLICKS3, NO_LEADS)]}))
    assert out.findings == []
    # «курсы бесплатно» отрезала бы «бесплатные курсы английского онлайн» с 12 лидами.
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
        _rule("cour01", "minus_phrase", ("query", "has_word", "курсы"), CLICKS3, NO_LEADS)]}))
    assert out.findings == []
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
        _rule("free01", "minus_word", FREE, CLICKS3, NO_LEADS)]}))
    assert out.findings == []
    # Отдельного отказа нет: условие правила не выполнено на семье.
    assert _codes(out) == []
    # В находке — точная семья; широкая (со словоформами) — рядом, для объяснения отказов.
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
        _rule("free03", "propose_word", FREE, CLICKS3)]}))
    f, = out.findings
    assert (f.details["metrics"]["leads"], f.details["family_queries"]) == (9, 3)
    assert f.details["conversions"] == 9
    assert f.details["broad"] == {"queries": 4, "leads": 21, "clicks": 59}


def test_expensive_family_cpl_rule(monkeypatch):
    # «Цена лида ≥ 5000» — про поток слова: у одной строки с лидами её не посчитать.
    rows = [_row(1, "высшее образование заочно", 100, 10000, 10),
            _row(1, "курсы бесплатно", 40, 9000, 1),
            _row(1, "бесплатно онлайн", 30, 3000, 0)]
    FakeDirect(monkeypatch, [_camp(1)], rows, keywords={"1": []})
    # Встроенные выключены: «курсы бесплатно» они взяли бы минус-фразой сами.
    off = {"phrase_waste": False, "word_zero_conv": False}
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "builtin": off, "rules": [
        _rule("cpl001", "minus_word", FREE, ("cpl", "gte", 5000))]}))
    f, = out.findings
    assert (f.rule, f.object_id) == ("u:cpl001", "бесплатно")
    assert f.details["metrics"]["cpl"] == 12000.0
    assert f.details["matched"][0]["conds"][1] == {"field": "cpl", "op": "gte", "value": 5000,
                                                   "actual": 12000.0}
    # cpl_x — против той же базы кабинета: 12000 / (22000 / 11) = 6.
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "builtin": off, "rules": [
        _rule("cplx01", "minus_word", FREE, ("cpl_x", "gte", 6))]}))
    assert [f.object_id for f in out.findings] == ["бесплатно"]
    assert out.findings[0].details["metrics"]["account_cpl"] == 2000.0
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "builtin": off, "rules": [
        _rule("cplx02", "minus_word", FREE, ("cpl_x", "gte", 7))]}))
    assert out.findings == []


def test_text_only_minus_word_needs_no_lead_goals(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _small(), keywords={"1": []})
    out = qm.run(_ctx({"rules": [_rule("free01", "minus_word", FREE, CLICKS3)]}))
    assert [(f.rule, f.object_id) for f in out.findings] == [("u:free01", "бесплатно")]
    assert out.findings[0].details["metrics"]["leads"] is None
    assert out.unverified == [] and _codes(out) == []
    # С условием по лидам без целей-лидов — не проверено на весь логин.
    out = qm.run(_ctx({"rules": [_rule("free01", "minus_word", FREE, CLICKS3, NO_LEADS)]}))
    assert out.findings == [] and out.unverified == [("login1", "u:free01")]


def test_family_leads_unknown_in_campaign(monkeypatch):
    # Цель не видна кампании: условие по лидам семьи не проверить — тройка и расход.
    rows = [_row(2, "курсы бесплатно", 30, 3000, 0)]
    FakeDirect(monkeypatch, [_camp(2)], rows, keywords={"2": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL],
                       "rules": [_rule("free01", "minus_word", FREE, CLICKS3, NO_LEADS)]}))
    assert out.findings == []
    assert ("login1", "u:free01", 2) in out.unverified
    assert _codes(out) == [{"code": "goal_not_visible", "object": None, "campaign_id": 2,
                            "rule": "u:free01", "login": "login1", "cost": 3000.0}]
    # Без условий по лидам то же правило работает.
    out = qm.run(_ctx({"lead_goal_ids": [GOAL],
                       "rules": [_rule("free01", "minus_word", FREE, CLICKS3)]}))
    assert [f.object_id for f in out.findings] == ["бесплатно"]


def test_held_goal_not_visible_carries_cost(monkeypatch):
    rows = [_row(1, "высшее образование заочно", 100, 10000, 10),
            _row(1, "купить диплом", 100, 12000, 0),
            _row(2, "курсы бесплатно", 30, 3000, 0)]
    FakeDirect(monkeypatch, [_camp(1), _camp(2)], rows, keywords={"1": [], "2": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
        _rule("prot01", "protect", FREE, ("leads", "gte", 1)),
        _rule("free02", "minus_phrase", FREE, CLICKS3)]}))
    held = [r for r in _codes(out) if r.get("held")]
    assert held == [{"code": "goal_not_visible", "object": "курсы бесплатно",
                     "campaign_id": 2, "rule": "u:free02", "login": "login1",
                     "by": "u:prot01", "held": True, "cost": 3000.0}]


def test_more_than_ten_lead_goals_marked_once_per_run(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _base(), keywords={"1": []})
    ctx = _ctx()
    ctx.project_goals = list(range(1, 12))
    out = qm.run(ctx)
    # Встроенные судили по цели стратегии; видно, почему не по целям-лидам.
    assert {f.rule for f in out.findings} == {"word_zero_conv", "phrase_waste"}
    assert _codes(out) == [{"code": "too_many_goals", "object": None, "campaign_id": None,
                            "rule": None, "login": None}]
    assert out.unverified == []


def test_only_names_stopped_or_unsupported_campaign(monkeypatch):
    camps = [_camp(1), _camp(2, state="SUSPENDED"), _camp(3, ctype="MOBILE_APP_CAMPAIGN")]
    FakeDirect(monkeypatch, camps, _small(), keywords={"1": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
        _rule("free01", "minus_phrase", FREE, CLICKS3,
              campaigns={"only": [1, 2, 3, 99]})]}))
    assert [f.object_id for f in out.findings] == ["курсы бесплатно"]
    # 99 — не кампания этого логина: only общий на проект, чужие кампании не наши.
    assert [(r["code"], r["campaign_id"]) for r in _codes(out)] == [
        ("campaign_stopped", 2), ("campaign_type_unsupported", 3)]


def test_per_campaign_cap_applies_to_user_rules(monkeypatch):
    rows = [_row(1, "высшее образование заочно", 100, 10000, 10)]
    rows += [_row(1, "курсы бесплатно %d" % i, 3, 300 - i, 0) for i in range(12)]
    rows += [_row(2, "высшее образование заочно", 100, 10000, 10),
             _row(2, "курсы бесплатно 0", 3, 100, 0)]
    FakeDirect(monkeypatch, [_camp(1), _camp(2)], rows, keywords={"1": [], "2": []})
    # Встроенные выключены: «бесплатно» и «курсы» они взяли бы из тех же мест кампании.
    off = {"phrase_waste": False, "word_zero_conv": False}
    out = qm.run(_ctx({"max_per_run": 100, "lead_goal_ids": [GOAL], "builtin": off,
                       "rules": [_rule("free01", "minus_phrase", FREE, CLICKS3)]}))
    per = {}
    for f in out.findings:
        per[f.campaign_id] = per.get(f.campaign_id, 0) + 1
    # Лимит 10 — на кампанию: соседняя кампания своё получает.
    assert per == {1: 10, 2: 1}
    assert out.summary["over_cap"] == 2
    assert out.summary["by_rule"]["u:free01"] == {"found": 11, "refused": 0, "over_cap": 2}
    # Отложены самые дешёвые.
    assert "курсы бесплатно 10" not in _objects(out)
    assert "курсы бесплатно 11" not in _objects(out)


def test_user_phrase_with_special_chars_refused(monkeypatch):
    rows = _small() + [_row(1, "курсы - бесплатно", 5, 500, 0),
                       _row(1, "бесплатно ли учиться?", 6, 600, 0)]
    FakeDirect(monkeypatch, [_camp(1)], rows, keywords={"1": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
        _rule("free01", "minus_phrase", FREE, ("clicks", "gte", 5))]}))
    # «курсы бесплатно» сама по себе 3 клика, но её семья включает «курсы - бесплатно».
    assert [f.object_id for f in out.findings] == ["курсы бесплатно"]
    assert sorted((r["code"], r["object"]) for r in _codes(out)) == [
        ("phrase_invalid", "бесплатно ли учиться?"), ("phrase_invalid", "курсы - бесплатно")]
    assert all("робот пишет только" in r["detail"] for r in _codes(out))


# --- раунд правок 3 ---------------------------------------------------------------------

def test_false_stem_does_not_inflate_family(monkeypatch):
    # Проба ревью: «скачки на лошадях» — не форма слова «скачать». По широкой семье поток
    # был бы 23 клика, по точной — 3: «≥ 10» держит точная.
    rows = [_row(1, "высшее образование заочно", 100, 10000, 10),
            _row(1, "скачать курсы", 3, 300, 0),
            _row(1, "скачки на лошадях", 20, 2000, 0)]
    FakeDirect(monkeypatch, [_camp(1)], rows, keywords={"1": []})
    off = {"phrase_waste": False, "word_zero_conv": False}
    word = ("query", "has_word", "скачать")
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "builtin": off, "rules": [
        _rule("sk0001", "minus_word", word, ("clicks", "gte", 10))]}))
    assert out.findings == [] and _codes(out) == []
    # Порог по силам точной семье — находка, и в ней только точная семья.
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "builtin": off, "rules": [
        _rule("sk0002", "minus_word", word, ("clicks", "gte", 3))]}))
    f, = out.findings
    d = f.details
    assert (d["clicks"], d["queries_sample"], d["family_queries"]) == (3, ["скачать курсы"], 1)
    assert d["metrics"]["clicks"] == 3
    assert d["broad"] == {"queries": 2, "leads": 0, "clicks": 23}


def test_phrase_covered_by_word_finding_same_run(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _base(), keywords={"1": []})
    # Встроенное слово «бесплатно» гасит фразу правила «курсы бесплатно»: она не находка.
    out = qm.run(_ctx({"rules": [_rule("cour01", "minus_phrase",
                                       ("query", "has_word", "курсы"), CLICKS3)]}))
    assert ("word_zero_conv", "бесплатно") in {(f.rule, f.object_id) for f in out.findings}
    assert "курсы бесплатно" not in _objects(out)
    assert _codes(out) == [{"code": "covered_by_minus", "object": "курсы бесплатно",
                            "campaign_id": 1, "rule": "u:cour01", "login": "login1",
                            "word": "бесплатно", "by": "word_zero_conv"}]
    # Слово правила гасит встроенную фразу «купить диплом» той же кампании.
    out = qm.run(_ctx({"rules": [_rule("dipl01", "minus_word",
                                       ("query", "has_word", "диплом"), CLICKS3)]}))
    assert ("u:dipl01", "диплом") in {(f.rule, f.object_id) for f in out.findings}
    assert "купить диплом" not in _objects(out)
    # Слово-предложение фразу не гасит: человек может его отклонить.
    out = qm.run(_ctx({"rules": [_rule("dipl02", "propose_word",
                                       ("query", "has_word", "диплом"), CLICKS3)]}))
    assert {"диплом", "купить диплом"} <= _objects(out)


def test_word_covers_phrase_only_in_its_campaign(monkeypatch):
    rows = _small(1) + _small(2)
    FakeDirect(monkeypatch, [_camp(1), _camp(2)], rows, keywords={"1": [], "2": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
        _rule("free01", "minus_word", FREE, CLICKS3, campaigns={"only": [1]}),
        _rule("cour01", "minus_phrase", ("query", "has_word", "курсы"), CLICKS3)]}))
    # Слово стоит только в кампании 1: там фраза лишняя, в кампании 2 — нет.
    assert {(f.rule, f.campaign_id, f.object_id) for f in out.findings} == {
        ("u:free01", 1, "бесплатно"), ("u:cour01", 2, "курсы бесплатно")}
    assert [(r["code"], r["campaign_id"]) for r in _codes(out)] == [("covered_by_minus", 1)]


# --- полный улов правила для превью (final-review I-2 п.3) ---------------------------

def test_rule_below_counts_phrase_taken_by_rule_above(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _small(), keywords={"1": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
        _rule("first1", "propose_word", FREE, ("clicks", "gte", 3)),
        _rule("secnd1", "minus_word", FREE, ("clicks", "gte", 2))]}))
    assert [f.rule for f in out.findings] == ["u:first1"]
    assert out.summary["intercepted"] == {"u:secnd1": {"taken": 1, "protected": 0}}


def test_protect_counts_for_every_rule_that_caught_the_phrase(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _small(), keywords={"1": []})
    out = qm.run(_ctx({"lead_goal_ids": [GOAL], "rules": [
        _rule("first1", "minus_word", FREE, ("clicks", "gte", 3)),
        _rule("secnd1", "minus_word", FREE, ("clicks", "gte", 2)),
        _rule("prot01", "protect", ("query", "has_word", "курсы"))]}))
    assert out.findings == []
    assert out.summary["intercepted"] == {"u:first1": {"taken": 0, "protected": 1},
                                          "u:secnd1": {"taken": 0, "protected": 1}}


def test_rule_counts_phrase_taken_by_builtin(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], _base(), keywords={"1": []})
    out = qm.run(_ctx({"rules": [_rule("free01", "minus_word", FREE, CLICKS3)]}))
    assert out.summary["intercepted"]["u:free01"] == {"taken": 1, "protected": 0}


# --- применение одобренного перепроверяет защиту пользователя (Minor 4) -------------

def _approved(phrase, **details):
    f = _stored(1, phrase, rule="u:free01")
    return dataclasses.replace(f, details=details)


def test_apply_skips_phrase_protected_after_approval(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1)], keywords={"1": []})
    prot = _rule("prot01", "protect", ("query", "has_word", "курсы"))
    res = qm.apply(_ctx({"rules": [prot]}),
                   [_approved("бесплатно", query="курсы бесплатно",
                              queries_sample=["курсы бесплатно", "бесплатно онлайн"])])
    assert not res[1].ok
    assert res[1].response["code"] == "user_protect" and res[1].response["by"] == "u:prot01"
    assert "«Правило prot01»" in res[1].error
    assert fake.writes == []
    # защита по слову самой фразы: всё, что фраза отрежет, это слово содержит
    word = _rule("prot02", "protect", ("query", "has_word", "бесплатно"))
    res = qm.apply(_ctx({"rules": [word]}), [_approved("бесплатно")])
    assert res[1].response["by"] == "u:prot02" and fake.writes == []
    # чужая защита не мешает
    other = _rule("prot03", "protect", ("query", "has_word", "диплом"))
    res = qm.apply(_ctx({"rules": [other]}), [_approved("бесплатно", query="курсы бесплатно")])
    assert res[1].ok and fake.writes == [(1, ["бесплатно"])]


def test_apply_never_minus_carries_code(monkeypatch):
    FakeDirect(monkeypatch, [_camp(1)], keywords={"1": []})
    res = qm.apply(_ctx({"never_minus": ["бесплатно"]}), [_stored(1, "бесплатно")])
    assert res[1].response["code"] == "in_never_minus"


def _v501_error(as_result):
    """Два вида отказа ЕПК в v5: ошибка запроса и ошибка в UpdateResults."""
    def update(token, login, cid, field, items):
        if as_result:
            return {"Id": cid, "Errors": [{"Code": 5005, "Message": "Используйте версию API v501"}]}
        raise DirectError("campaigns [5005] Поле задано неверно: Используйте версию API v501")
    return update


@pytest.mark.parametrize("as_result", [True, False])
def test_apply_epk_5005_fails_with_code_not_crash(monkeypatch, as_result):
    """ЕПК, которую v5 не даёт менять (5005 «используйте v501»): находка failed с кодом
    epk_needs_v501, соседняя кампания применяется, прогон не падает."""
    fake = FakeDirect(monkeypatch, [_camp(1, ctype="UNIFIED_CAMPAIGN"), _camp(2)])
    real = fake.update_campaign_list

    def update(token, login, cid, field, items):
        if cid == 1:
            return _v501_error(as_result)(token, login, cid, field, items)
        return real(token, login, cid, field, items)

    monkeypatch.setattr(qm.direct_api, "update_campaign_list", update)
    res = qm.apply(_ctx(), [_stored(1, "бесплатно"), _stored(2, "мусор", cid=2)])
    assert not res[1].ok and res[1].error == "epk_needs_v501"
    assert res[1].response.get("direct") and "5005" in res[1].response["direct"]
    assert res[2].ok


def test_rollback_epk_5005_fails_with_code(monkeypatch):
    fake = FakeDirect(monkeypatch, [_camp(1, ctype="UNIFIED_CAMPAIGN", negatives=["бесплатно"])])
    monkeypatch.setattr(qm.direct_api, "update_campaign_list", _v501_error(False))
    res = qm.rollback(_ctx(), [_stored(1, "бесплатно")])
    assert not res[1].ok and res[1].error == "epk_needs_v501"
    assert fake.writes == []


def test_5005_elsewhere_in_group_is_coded(monkeypatch):
    """5005 на чтении ключей (keywords.get по ЕПК) — тот же код, а не сырой текст."""
    FakeDirect(monkeypatch, [_camp(1, ctype="UNIFIED_CAMPAIGN")])

    def kw(*a, **k):
        raise DirectError("keywords [5005] Используйте версию API v501")

    monkeypatch.setattr(qm.direct_api, "keywords_by_campaign", kw)
    res = qm.apply(_ctx(), [_stored(1, "бесплатно")])
    assert res[1].error == "epk_needs_v501"
