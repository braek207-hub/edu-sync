import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.classify import normalize_plan_direction, normalize_plan_project


def test_normalize_plan_project():
    assert normalize_plan_project("ВсеКолледжи") == "vse"
    assert normalize_plan_project("бренды") == "brand"
    assert normalize_plan_project("vuz") == "vuz"


def test_normalize_plan_direction():
    assert normalize_plan_direction("СПО") == "spo"
    assert normalize_plan_direction("дистанс") == "dist"
    assert normalize_plan_direction("остальное") == "other"
