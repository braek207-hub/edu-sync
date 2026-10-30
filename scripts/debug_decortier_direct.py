#!/usr/bin/env python3
"""Debug Decortier Direct report — print raw TSV head."""
import io
import csv
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
for name in (".env.local", ".env"):
    p = ROOT.parent / "EDU v2" / name
    if not p.exists():
        p = ROOT / name
    if not p.exists():
        continue
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        t = line.strip()
        if not t or t.startswith("#") or "=" not in t:
            continue
        k, v = t.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

token = os.environ.get("DECORTIER_DIRECT_TOKEN", "").strip()
login = (os.environ.get("DECORTIER_DIRECT_CLIENT_LOGIN", "") or "walpapperdecor").strip()

if not token:
    print("ERROR: no token", file=sys.stderr)
    sys.exit(1)

date_from = sys.argv[1] if len(sys.argv) > 1 else "2026-05-22"
date_to = sys.argv[2] if len(sys.argv) > 2 else "2026-06-25"

body = {
    "params": {
        "SelectionCriteria": {"DateFrom": date_from, "DateTo": date_to},
        "FieldNames": ["Date", "CampaignId", "CampaignName", "Impressions", "Clicks", "Cost"],
        "ReportName": f"decortier_debug_{date_from}_{date_to}",
        "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
        "DateRangeType": "CUSTOM_DATE",
        "Format": "TSV",
        "IncludeVAT": "YES",
        "IncludeDiscount": "NO",
    }
}
headers = {
    "Authorization": f"Bearer {token}",
    "Accept-Language": "ru",
    "processingMode": "auto",
    "returnMoneyInMicros": "false",
    "Content-Type": "application/json",
    "Client-Login": login,
}

for i in range(8):
    r = requests.post(
        "https://api.direct.yandex.com/json/v5/reports",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    r.encoding = "utf-8"
    print(f"attempt {i+1}: status={r.status_code}")
    if r.status_code == 200:
        break
    if r.status_code in (201, 202):
        time.sleep(int(r.headers.get("retryIn", 30)))
        continue
    print(r.text[:500])
    sys.exit(1)

text = r.text
lines = text.splitlines()
print(f"login={login} range={date_from}..{date_to}")
print(f"response lines={len(lines)} bytes={len(text)}")
print("--- first 15 lines ---")
for line in lines[:15]:
    print(line)

reader = csv.DictReader(io.StringIO(text), delimiter="\t")
print("--- fieldnames ---", reader.fieldnames)
rows = list(reader)
print(f"parsed rows={len(rows)}")
if rows:
    print("sample:", rows[0])
