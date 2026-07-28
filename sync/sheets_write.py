# -*- coding: utf-8 -*-
"""Google Sheets клиент С ПРАВОМ ЗАПИСИ — отдельно от sync/sheets.py.

sheets.py держит scope spreadsheets.readonly и сервис-аккаунт EDU (чтение CRM).
Смешивать нельзя: расширить его scope до записи — открыть право писать во все
EDU-таблицы. Поэтому здесь свой клиент, свой сервис-аккаунт (LIME reports) и
write-scope. Идентичности не пересекаются.

ENV (в порядке приоритета):
  LIME_REPORTS_SA_JSON             — JSON приватного ключа сервис-аккаунта строкой
  LIME_REPORTS_SA_FILE / GOOGLE_APPLICATION_CREDENTIALS — путь к JSON-файлу ключа
"""
from __future__ import annotations

import json
import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_write_service():
    """Sheets v4 клиент под сервис-аккаунтом LIME reports с правом записи."""
    sa_json = os.environ.get("LIME_REPORTS_SA_JSON")
    if sa_json:
        creds = Credentials.from_service_account_info(json.loads(sa_json), scopes=SCOPES)
    else:
        path = os.environ.get("LIME_REPORTS_SA_FILE") or os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
        creds = Credentials.from_service_account_file(path, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_values(service, spreadsheet_id: str, a1_range: str, *, render: str | None = None) -> list[list]:
    """Прочитать диапазон. render: FORMATTED_VALUE | UNFORMATTED_VALUE | FORMULA."""
    kwargs: dict = {"spreadsheetId": spreadsheet_id, "range": a1_range}
    if render:
        kwargs["valueRenderOption"] = render
    return service.spreadsheets().values().get(**kwargs).execute().get("values", [])


def list_tabs(service, spreadsheet_id: str) -> list[str]:
    """Названия всех вкладок книги."""
    meta = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
        .execute()
    )
    return [s["properties"]["title"] for s in meta.get("sheets", [])]


def add_tab(service, spreadsheet_id: str, title: str) -> None:
    """Создать вкладку с заданным именем."""
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
    ).execute()


def write_block(service, spreadsheet_id: str, a1_start: str, values2d: list[list]) -> None:
    """Записать двумерный блок начиная с a1_start (например 'Fact Traffic!A1'). RAW."""
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=a1_start,
        valueInputOption="RAW",
        body={"values": values2d},
    ).execute()


def batch_write(service, spreadsheet_id: str, data: list[tuple[str, list[list]]]) -> int:
    """Пакетно записать [(a1_range, values2d), ...] одним запросом. RAW."""
    if not data:
        return 0
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "valueInputOption": "RAW",
            "data": [{"range": rng, "values": vals} for rng, vals in data],
        },
    ).execute()
    return len(data)


def update_cell(service, spreadsheet_id: str, a1_cell: str, value) -> None:
    """Записать одно значение (RAW: число остаётся числом, формула не выполняется)."""
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=a1_cell,
        valueInputOption="RAW",
        body={"values": [[value]]},
    ).execute()


def batch_update(service, spreadsheet_id: str, updates: list[tuple[str, object]]) -> int:
    """Пакетно записать [(a1_cell, value), ...] одним запросом. RAW."""
    if not updates:
        return 0
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "valueInputOption": "RAW",
            "data": [{"range": cell, "values": [[val]]} for cell, val in updates],
        },
    ).execute()
    return len(updates)
