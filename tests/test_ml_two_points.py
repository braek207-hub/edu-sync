"""Task 7: две точки скоринга — чистая логика выбора субпопуляции (без catboost/БД)."""

from sync.ml_train import point_subset


def test_post_connection_subset():
    rows = [{"label_connected": True}, {"label_connected": False}]
    assert len(point_subset(rows, "post_connection")) == 1
    assert len(point_subset(rows, "at_creation")) == 2


def test_at_creation_returns_all_including_none_connected():
    rows = [{"label_connected": True}, {"label_connected": None}, {"label_connected": 0}]
    assert len(point_subset(rows, "at_creation")) == 3


def test_post_connection_filters_falsy_connected():
    rows = [
        {"label_connected": True},
        {"label_connected": None},   # None → отброшен
        {"label_connected": 0},      # 0 → отброшен
        {"label_connected": 1},      # truthy → оставлен
    ]
    got = point_subset(rows, "post_connection")
    assert len(got) == 2
    assert all(r["label_connected"] for r in got)


def test_point_subset_returns_new_list_for_at_creation():
    rows = [{"label_connected": True}]
    out = point_subset(rows, "at_creation")
    assert out is not rows          # копия, не тот же объект
    assert out == rows
