"""Tests for feature matrix building (Ф2.1: каскад-композиция удалена)."""

from sync.ml.cascade import build_stage_matrix


def test_build_matrix_types_and_na():
    """Проверяет сборку матрицы: типы, NA-обработка, исключение post_connection фич."""
    feats = [
        {
            "f__city_ip_segment": "msk_mo",
            "f__beh_visits": 3,
            "f__beh_device": None,
            "f__audience": "parent",
        }
    ]
    rows, names, cats = build_stage_matrix(feats, "at_creation")

    # post_connection фичи исключены (audience — анкета звонка, утечка на at_creation)
    assert "time_to_connection_days" not in names
    assert "audience" not in names

    # Категориальные значения
    assert rows[0]["city_ip_segment"] == "msk_mo"
    assert rows[0]["beh_device"] == "__na__"  # cat None → __na__

    # Числовые значения
    assert rows[0]["beh_visits"] == 3.0  # num as float

    # Разделение на категориальные и числовые
    assert "city_ip_segment" in cats and "beh_visits" not in cats
