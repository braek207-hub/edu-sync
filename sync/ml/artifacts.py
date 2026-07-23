"""Сериализация артефактов модели в bytes (для хранения bytea в edu_ml_artifacts)."""

import os
import pickle
import tempfile


def serialize_pickle(obj) -> bytes:
    return pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)


def deserialize_pickle(blob) -> object:
    return pickle.loads(bytes(blob))


def serialize_catboost(model) -> bytes:
    # CatBoost.save_model принимает только путь (str/Path), не файловый объект.
    fd, path = tempfile.mkstemp(suffix=".cbm")
    os.close(fd)
    try:
        model.save_model(path, format="cbm")
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        os.remove(path)


def deserialize_catboost(blob):
    from catboost import CatBoostClassifier
    fd, path = tempfile.mkstemp(suffix=".cbm")
    os.close(fd)
    try:
        with open(path, "wb") as fh:
            fh.write(bytes(blob))
        m = CatBoostClassifier()
        m.load_model(path, format="cbm")
        return m
    finally:
        os.remove(path)
