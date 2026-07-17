import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync import lime


def test_delete_sql_excludes_gcc():
    """RU/KZ-синк не должен трогать строки region='gcc'."""
    sql = lime.DELETE_SQL
    assert "region" in sql.lower()
    assert "gcc" in sql.lower()
