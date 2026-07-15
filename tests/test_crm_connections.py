import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# googleapiclient optional locally
sys.modules.setdefault("googleapiclient", MagicMock())
sys.modules.setdefault("googleapiclient.discovery", MagicMock())

from sync.crm import _sync_leads_raw


def test_payments_from_leads_column():
    headers = ["ID", "Дата создания", "Ленд", "UTM Campaign", "Оплата"]
    values = [
        headers,
        ["1", "01.02.2026", "vuz", "12345", "1"],
        ["2", "01.02.2026", "vuz", "12345", "0"],
        ["3", "02.02.2026", "vuz", "99999", "да"],
    ]
    agg, _dims, _details = _sync_leads_raw(headers, values)
    # audience defaults to "unknown" when no «Родитель» column → 6-part key
    k1 = "2026-02-01|12345|rf|unknown|unknown|unknown"
    k2 = "2026-02-02|99999|rf|unknown|unknown|unknown"
    assert agg[k1]["payments_from_leads"] == 1
    assert agg[k2]["payments_from_leads"] == 1


def test_connections_from_b24_date_column():
    headers = ["ID", "Дата создания", "Ленд", "UTM Campaign", "Б24 дата соединения на ОП"]
    values = [
        headers,
        ["1", "01.02.2026", "vuz", "12345", "02.02.2026"],
        ["2", "01.02.2026", "vuz", "12345", ""],
    ]
    agg, _dims, _details = _sync_leads_raw(headers, values)
    key = "2026-02-01|12345|rf|unknown|unknown|unknown"
    assert agg[key]["leads"] == 2
    assert agg[key]["connections"] == 1
