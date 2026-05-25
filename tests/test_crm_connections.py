import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# googleapiclient optional locally
sys.modules.setdefault("googleapiclient", MagicMock())
sys.modules.setdefault("googleapiclient.discovery", MagicMock())

from sync.crm import _sync_leads_raw


def test_connections_from_b24_date_column():
    headers = ["ID", "Дата создания", "Ленд", "UTM Campaign", "Б24 дата соединения на ОП"]
    values = [
        headers,
        ["1", "01.02.2026", "vuz", "12345", "02.02.2026"],
        ["2", "01.02.2026", "vuz", "12345", ""],
    ]
    agg, _dims = _sync_leads_raw(headers, values)
    key = "2026-02-01|12345|rf|unknown|unknown"
    assert agg[key]["leads"] == 2
    assert agg[key]["connections"] == 1
