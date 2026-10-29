"""Probe: брендовый спрос Polina Repik по Wordstat (Search API), помесячно.
Только чтение. Результат — JSON в скретчпад сессии."""
import json, os, sys, datetime as dt
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sync.wordstat import _post_dynamics

PHRASES = ["polina repik", "полина репик", "polinarepik", "полинарепик"]
# 1 = Москва и область (справочник GeoRegions Директа)
SETS = {"ru": None, "msk": ["1"]}
FROM, TO = "2024-09-01", "2026-08-31"

def monthly(phrase, regions):
    body = {"phrase": phrase, "period": "PERIOD_MONTHLY",
            "fromDate": f"{FROM}T00:00:00Z", "toDate": f"{TO}T23:59:59Z"}
    if regions: body["regions"] = regions
    return _post_dynamics(body)

out = {}
for key, regions in SETS.items():
    agg = {}
    per_phrase = {}
    for ph in PHRASES:
        try:
            r = monthly(ph, regions)
        except Exception as e:
            per_phrase[ph] = "ERR: %s" % e
            continue
        pts = {p["date"][:7]: int(p.get("count", 0) or 0) for p in r.get("results", [])}
        per_phrase[ph] = pts
        for m, c in pts.items():
            agg[m] = agg.get(m, 0) + c
    out[key] = {"total_by_month": dict(sorted(agg.items())), "by_phrase": per_phrase}

dest = r"C:\Users\Admin\AppData\Local\Temp\claude\d--vscode-EDU-v2\f59387db-871c-4492-becb-24b5444a7ba6\scratchpad\polina_wordstat.json"
with open(dest, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("saved", dest)
for k, v in out.items():
    print(k, json.dumps(v["total_by_month"], ensure_ascii=False))
