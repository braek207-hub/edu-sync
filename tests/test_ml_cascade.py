"""Tests for cascade composition and feature matrix building."""

from sync.ml.cascade import compose_cascade, build_stage_matrix


def test_compose_product_and_clip():
    """Проверяет композицию вероятностей: поэлементное произведение с клипом [0,1]."""
    out = compose_cascade([0.5, 1.0], [0.5, 1.0], [0.5, 2.0])
    assert abs(out[0] - 0.125) < 1e-9  # 0.5 * 0.5 * 0.5 = 0.125
    assert out[1] == 1.0                 # 1.0 * 1.0 * 2.0 = 2.0, clipped to 1.0


def test_build_matrix_types_and_na():
    """Проверяет сборку матрицы: типы, NA-обработка, исключение post_connection фич."""
    feats = [
        {
            "f__audience": "parent",
            "f__beh_visits": 3,
            "f__beh_device": None,
            "f__b24_grad_year": None,
        }
    ]
    rows, names, cats = build_stage_matrix(feats, "at_creation")

    # post_connection фичи исключены
    assert "time_to_connection_days" not in names

    # Категориальные значения
    assert rows[0]["audience"] == "parent"
    assert rows[0]["beh_device"] == "__na__"  # cat None → __na__

    # Числовые значения
    assert rows[0]["beh_visits"] == 3.0  # num as float

    # Разделение на категориальные и числовые
    assert "audience" in cats and "beh_visits" not in cats
