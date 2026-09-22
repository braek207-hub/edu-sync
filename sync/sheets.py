"""Общий клиент Google Sheets для edu-sync."""

from __future__ import annotations

import json
import os

import httplib2
from google.oauth2.service_account import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _sheets_http() -> httplib2.Http:
    # httplib2 ≥0.32 считает ответ «декомпрессионной бомбой», если после 10 МиБ
    # он раздулся больше чем в 100× относительно gzip-байтов. Лист CRM «Лиды»
    # (130k+ строк, тысячи пустых) — как раз такой: 22.09.2026 коэффициент стал
    # 133× и синк потерял crm_leads/crm_payments. Ratio выключаем; жёсткий
    # потолок объёма (10 ГиБ) остаётся — от Google это единственный нужный предохранитель.
    return httplib2.Http(decode_limit_ratio=0)


def get_sheets_service():
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        creds = Credentials.from_service_account_file(
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"], scopes=SCOPES
        )
    else:
        sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT"]
        creds = Credentials.from_service_account_info(json.loads(sa_json), scopes=SCOPES)
    http = AuthorizedHttp(creds, http=_sheets_http())
    return build("sheets", "v4", http=http, cache_discovery=False)


def read_sheet(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    *,
    value_render_option: str | None = None,
) -> list[list]:
    kwargs: dict = {
        "spreadsheetId": spreadsheet_id,
        "range": sheet_name,
    }
    if value_render_option:
        kwargs["valueRenderOption"] = value_render_option
    # Sheets API регулярно отдаёт транзиентные 503/429. Встроенный ретрай
    # googleapiclient (экспоненциальный бэкофф на 5xx/429) снимает почти все:
    # без него единичный 503 на листе «Оплаты» 23.08 обнулил is_paid у всех
    # лидов 2026 года (см. sync/crm.py::_load_paid_by_lead_id).
    result = service.spreadsheets().values().get(**kwargs).execute(num_retries=4)
    return result.get("values", [])
