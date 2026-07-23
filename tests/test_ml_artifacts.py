import numpy as np
from sync.ml.artifacts import serialize_pickle, deserialize_pickle

def test_pickle_roundtrip():
    obj = {"a": [1, 2, 3], "b": np.array([0.1, 0.2])}
    blob = serialize_pickle(obj)
    assert isinstance(blob, (bytes, bytearray))
    back = deserialize_pickle(blob)
    assert back["a"] == [1, 2, 3]
    assert np.allclose(back["b"], [0.1, 0.2])
