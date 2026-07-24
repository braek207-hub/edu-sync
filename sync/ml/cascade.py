"""Сборка матрицы фич под точку решения (после Ф2.1 каскад-композиция удалена —
прод-модель single-stage логистика, `build_stage_matrix` остаётся общей витриной фич)."""

from sync.ml.registry import REGISTRY, select_features, feature_key


def build_stage_matrix(feature_dicts, point):
    """Сборка матрицы фич под точку решения.

    Фичи берутся из REGISTRY, доступные к точке решения `point`.
    Категориальные фичи: None → "__na__", иначе str.
    Числовые фичи: None → 0.0, иначе float.

    Args:
        feature_dicts: Список JSONB-словарей с ключами вида f__<name>.
        point: Точка решения ("pre_lead", "at_creation", "post_connection").

    Returns:
        tuple[list[dict], list[str], list[str]]: (rows, feature_names, cat_names)
            - rows: Список словарей с распарсенными фичами.
            - feature_names: Имена фич (без префикса f__).
            - cat_names: Имена категориальных фич.
    """
    names = select_features(point)
    spec = {f.name: f for f in REGISTRY}
    cat_names = [n for n in names if spec[n].dtype == "cat"]

    rows = []
    for fd in feature_dicts:
        row = {}
        for n in names:
            v = fd.get(feature_key(n))
            if spec[n].dtype == "cat":
                row[n] = "__na__" if v is None else str(v)
            else:
                row[n] = 0.0 if v is None else float(v)
        rows.append(row)

    return rows, names, cat_names
