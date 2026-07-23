"""Сериализация артефактов модели в bytes (для хранения bytea в edu_ml_artifacts)."""

import io
import pickle


def serialize_pickle(obj) -> bytes:
    return pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)


def deserialize_pickle(blob) -> object:
    return pickle.loads(bytes(blob))


def serialize_catboost(model) -> bytes:
    buf = io.BytesIO()
    model.save_model(buf, format="cbm")
    return buf.getvalue()


def deserialize_catboost(blob):
    from catboost import CatBoostClassifier
    m = CatBoostClassifier()
    m.load_model(io.BytesIO(bytes(blob)), format="cbm")
    return m
