# -*- coding: utf-8 -*-
"""Паритет классов запросов с Panda: те же векторы, что lib/queries/vectors.json в Panda-BI."""

import json
from pathlib import Path

import pytest

from sync.modules import query_class as qc

V = json.loads((Path(__file__).parent / "data" / "query_vectors.json").read_text("utf-8"))


@pytest.mark.parametrize("query,cls,hit", V["classify"])
def test_classify(query, cls, hit):
    assert qc.classify(query, V["dict"]) == (cls, hit)


@pytest.mark.parametrize("case", V["judge"], ids=lambda c: c["row"]["query"])
def test_judge(case):
    s = {"id": "s", "on": True, "clean": case["clean"], "campaigns": []}
    _cls, _hit, clean, minus = qc.judge(case["row"], s, V["dict"], V["thresholds"])
    assert [clean, minus] == case["expect"]


@pytest.mark.parametrize("case", V["judgeNoMaxCpa"], ids=lambda c: str(c["row"]["cost"]))
def test_judge_without_max_cpa(case):
    s = {"id": "s", "on": True, "clean": case["clean"], "campaigns": []}
    th = dict(V["thresholds"], maxCpa=None)
    assert list(qc.judge(case["row"], s, V["dict"], th)[2:]) == case["expect"]


@pytest.mark.parametrize("camp,set_id,kind", V["sets"]["cases"])
def test_set_for(camp, set_id, kind):
    s, why = qc.set_for(camp, V["sets"]["sets"], V["sets"]["manual"])
    assert [s["id"] if s else None, why] == [set_id, kind]


def test_off_set_and_no_set_give_nothing():
    row = {"query": "lime", "cost": 1, "clicks": 1, "conversions": 0}
    assert qc.judge(row, {"on": False, "clean": ["brand"]}, V["dict"], V["thresholds"])[3] is None
    assert qc.judge(row, None, V["dict"], V["thresholds"])[3] is None
