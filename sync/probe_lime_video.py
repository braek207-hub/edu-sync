# -*- coding: utf-8 -*-
"""
sync/probe_lime_video.py — РАЗОВАЯ проверка: отдаёт ли Reports API Директа видео-метрики
(досмотры) для LIME видео-кампаний. Изолирован от основного синка. Без записи в БД.

Проверяем: поля VideoComplete/VideoViews/VideoCompleteRate/AvgVideoCompleteCost принимаются
отдельным отчётом и возвращают НЕНУЛЕВЫЕ досмотры на видео-флайте (Медиаметрика вернула 0 —
здесь ждём реальные числа). Окно — апрельский видео-флайт 2026-04-16..2026-05-10.
"""
import csv
import io
import json
import os
import time

import requests

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"


def _report_headers():
    return {
        "Authorization": f"Bearer {os.environ['LIME_DIRECT_TOKEN'].strip()}",
        "Client-Login": os.environ["LIME_DIRECT_CLIENT_LOGIN"].strip(),
        "Accept-Language": "ru",
        "processingMode": "auto",
        "returnMoneyInMicros": "false",
        "skipReportHeader": "true",
        "skipReportSummary": "true",
        "Content-Type": "application/json; charset=utf-8",
    }


VIDEO_FIELDS = [
    "Date", "CampaignId", "CampaignName", "Impressions", "Clicks",
    "VideoViews", "VideoComplete", "VideoCompleteRate", "AvgVideoCompleteCost", "CPV",
]


REPORT_TYPES = [
    "CAMPAIGN_PERFORMANCE_REPORT",
    "REACH_AND_FREQUENCY_PERFORMANCE_REPORT",
    "CUSTOM_REPORT",
]


def _try(report_type: str, date_from: str, date_to: str) -> None:
    params = {
        "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
        "FieldNames": VIDEO_FIELDS,
        "ReportName": f"lime_vprobe_{report_type}_{date_from}",
        "ReportType": report_type,
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        "IncludeVAT": "YES",
        "IncludeDiscount": "NO",
    }
    payload = json.dumps({"params": params}).encode("utf-8")
    print(f"[vprobe] === {report_type} ===")
    r = None
    for _ in range(10):
        r = requests.post(REPORTS_URL, data=payload, headers=_report_headers())
        r.encoding = "utf-8"
        if r.status_code == 200:
            break
        if r.status_code in (201, 202):
            wait = int(r.headers.get("retryIn", "10"))
            print(f"[vprobe] {report_type}: формируется, ждём {wait}с...")
            time.sleep(wait)
            continue
        print(f"[vprobe] {report_type}: HTTP {r.status_code}: {r.text[:400]}")
        return
    else:
        print(f"[vprobe] {report_type}: таймаут")
        return
    reader = csv.DictReader(io.StringIO(r.text), delimiter="\t")
    print(f"[vprobe] {report_type}: колонки {reader.fieldnames}")
    total_vc, shown = 0, 0
    for row in reader:
        vc = row.get("VideoComplete", "0")
        try:
            vci = int(float(vc)) if vc not in ("--", "", None) else 0
        except ValueError:
            vci = 0
        total_vc += vci
        if vci > 0 and shown < 15:
            shown += 1
            print(f"[vprobe]   {row.get('Date')} {row.get('CampaignId')} '{row.get('CampaignName')}': "
                  f"impr={row.get('Impressions')} views={row.get('VideoViews')} complete={vc} "
                  f"rate={row.get('VideoCompleteRate')} cpv={row.get('CPV')}")
    print(f"[vprobe] {report_type}: ИТОГО VideoComplete = {total_vc}")


def probe(date_from: str, date_to: str) -> None:
    print(f"[vprobe] окно {date_from}..{date_to}, поля: {VIDEO_FIELDS}")
    for rt in REPORT_TYPES:
        try:
            _try(rt, date_from, date_to)
        except Exception as e:
            print(f"[vprobe] {rt}: исключение {e}")


if __name__ == "__main__":
    probe(os.environ.get("VPROBE_FROM", "2026-04-16"), os.environ.get("VPROBE_TO", "2026-05-10"))
