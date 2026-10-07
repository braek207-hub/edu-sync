"""Диагностика листов и БД после синка (без зависимости от BJ)."""

from __future__ import annotations

import os

from sync.classify import DIRECT_SHEETS
from sync.sheets import get_sheets_service, read_sheet
from sync.utils import pick_index_loose


def _log_direct_sheets(service, spreadsheet_id: str) -> None:
    print("--- Validate: Direct листы ---")
    for proj, sheet_name in DIRECT_SHEETS.items():
        try:
            values = read_sheet(service, spreadsheet_id, sheet_name)
        except Exception as e:
            print(f"  [{proj}] {sheet_name}: ошибка чтения — {e}")
            continue
        if len(values) < 2:
            print(f"  [{proj}] {sheet_name}: пусто")
            continue
        headers = [str(x or "").strip() for x in values[0]]
        hlow = [h.lower() for h in headers]
        metric_cols = sum(
            1
            for v in (
                "avgimpressionposition",
                "avgclickposition",
                "avgeffectivebid",
                "avgtrafficvolume",
            )
            if any(v in hh for hh in hlow)
        )
        print(
            f"  [{proj}] {sheet_name}: {len(headers)} колонок, "
            f"метрики трафика (найдено имён): {metric_cols}/4, строк: {len(values) - 1}"
        )


def _log_crm_sheet(service, spreadsheet_id: str, name: str) -> None:
    try:
        values = read_sheet(service, spreadsheet_id, name)
    except Exception as e:
        print(f"  [{name}]: ошибка — {e}")
        return
    if len(values) < 2:
        print(f"  [{name}]: пусто")
        return
    headers = [str(x or "").strip().lower() for x in values[0]]
    picks = {
        "city": pick_index_loose(headers, ["город (ip)", "город(ip)", "город ip"]),
        "grad": pick_index_loose(headers, ["б24 год выпуска", "год выпуска"]),
        "edu": pick_index_loose(
            headers, ["б24 уровень образования", "уровень образования", "класс/курс"]
        ),
        "connect": pick_index_loose(
            headers,
            ["б24 дата соединения", "дата соединения", "date connect", "connect date"],
        ),
    }
    print(
        f"  [{name}]: строк {len(values) - 1}, "
        f"city={picks['city']}, grad={picks['grad']}, edu={picks['edu']}, connect={picks['connect']}"
    )


def _log_db_direct_wavg() -> None:
    print("--- Validate: direct_stats w_avg (БД) ---")
    try:
        from sync.db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM direct_stats")
                total = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(*) FROM direct_stats WHERE w_avg_impr_pos > 0"
                )
                with_pos = cur.fetchone()[0]
        pct = (100.0 * with_pos / total) if total else 0.0
        print(f"  строк: {total}, w_avg_impr_pos > 0: {with_pos} ({pct:.1f}%)")
    except Exception as e:
        print(f"  БД: {e}")


def run_validation() -> None:
    spreadsheet_id = os.environ.get("GOOGLE_SHEETS_ID", "")
    if not spreadsheet_id:
        print("Validate: GOOGLE_SHEETS_ID не задан — пропуск листов")
        _log_db_direct_wavg()
        return
    try:
        service = get_sheets_service()
        _log_direct_sheets(service, spreadsheet_id)
        from sync.crm import crm_leads_sheets, crm_payments_sheets

        for sheet in crm_leads_sheets():
            _log_crm_sheet(service, spreadsheet_id, sheet)
        for sheet in crm_payments_sheets():
            _log_crm_sheet(service, spreadsheet_id, sheet)
    except Exception as e:
        print(f"Validate sheets: {e}")
    _log_db_direct_wavg()
    print("--- Validate: Direct API incremental 7d / full с DIRECT_DATE_FROM ---")
