"""Sheets-клиент должен переживать большие, хорошо сжимаемые ответы Sheets API.

22.09.2026 лист CRM «Лиды» (131k строк, ~18k пустых) стал отдаваться gzip-ом
с коэффициентом 133×; httplib2 0.32 принял это за декомпрессионную бомбу
(лимит 100× после 10 МиБ) и уронил crm_leads/crm_payments в EDU Daily Sync.
"""

import gzip
import json

import httplib2

from sync.sheets import _sheets_http


def _big_sparse_sheet_gzip() -> bytes:
    # 20k строк × 12 пустых ячеек — как хвост листа «Лиды»: >10 МиБ и ratio ≫ 100
    rows = [[""] * 12 for _ in range(20_000)] + [["x" * 600] * 12 for _ in range(1_500)]
    raw = json.dumps({"values": rows}).encode()
    assert len(raw) > 10 << 20
    return gzip.compress(raw, compresslevel=9)


def test_sheets_http_decodes_highly_compressible_body():
    http = _sheets_http()
    body = _big_sparse_sheet_gzip()
    response = httplib2.Response({"status": 200, "content-encoding": "gzip"})
    content = httplib2._decompressContent(response, body, http.limit_kwargs)
    assert json.loads(content)["values"][0] == [""] * 12


def test_default_httplib2_rejects_same_body():
    # Контроль: без нашей настройки тот же ответ падает — тест ловит именно её
    body = _big_sparse_sheet_gzip()
    response = httplib2.Response({"status": 200, "content-encoding": "gzip"})
    try:
        httplib2._decompressContent(response, body, httplib2.Http().limit_kwargs)
    except httplib2.decode.DecodeRatioError:
        return
    raise AssertionError("httplib2 больше не режет такой ответ — настройка в sheets.py лишняя")
