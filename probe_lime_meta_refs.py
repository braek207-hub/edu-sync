# Проба форм ответов справочных сервисов для meta-обогащения Campaign Launcher.
# Read-only. Печатает СЫРЫЕ первые записи — важны точные имена полей.
import json
import os
import time

import requests

TOKEN = os.environ["DIRECT_TOKEN"]
LOGIN = os.environ["DIRECT_CLIENT_LOGIN"]


def call(version: str, service: str, params: dict) -> tuple[float, int, dict]:
    t0 = time.time()
    resp = requests.post(
        f"https://api.direct.yandex.com/json/{version}/{service}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Client-Login": LOGIN,
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        },
        data=json.dumps({"method": "get", "params": params}, ensure_ascii=False).encode("utf-8"),
        timeout=120,
    )
    return time.time() - t0, len(resp.content), resp.json()


def show(label: str, version: str, service: str, params: dict, list_key: str) -> None:
    try:
        sec, size, body = call(version, service, params)
        if "error" in body:
            e = body["error"]
            print(f"{label} [{version}]: ERROR {e.get('error_code')} {e.get('error_string')}: {e.get('error_detail')}")
            return
        items = (body.get("result") or {}).get(list_key)
        n = len(items) if isinstance(items, list) else None
        print(f"{label} [{version}]: {sec:.1f}s {size/1048576:.2f}MB items={n}")
        print("  result keys:", sorted((body.get("result") or {}).keys()))
        for it in (items or [])[:3]:
            print("  ", json.dumps(it, ensure_ascii=False)[:400])
    except Exception as exc:  # noqa: BLE001
        print(f"{label} [{version}]: THROW {exc}")


for v in ("v501", "v5"):
    show("GeoRegions", v, "dictionaries", {"DictionaryNames": ["GeoRegions"]}, "GeoRegions")

for v in ("v501", "v5"):
    show(
        "RetargetingLists",
        v,
        "retargetinglists",
        {"SelectionCriteria": {}, "FieldNames": ["Id", "Name", "Type"]},
        "RetargetingLists",
    )

STRATEGY_PARAMS = {
    "SelectionCriteria": {},
    "FieldNames": ["Id", "Name", "Type"],
    "StrategyMaximumClicksFieldNames": ["WeeklySpendLimit"],
    "StrategyMaximumConversionRateFieldNames": ["WeeklySpendLimit", "GoalId"],
    "StrategyAverageCpcFieldNames": ["AverageCpc", "WeeklySpendLimit"],
    "StrategyAverageCpaFieldNames": ["AverageCpa", "GoalId", "WeeklySpendLimit"],
    "StrategyAverageCrrFieldNames": ["Crr", "GoalId", "WeeklySpendLimit"],
    "StrategyPayForConversionFieldNames": ["Cpa", "GoalId", "WeeklySpendLimit"],
    "StrategyPayForConversionCrrFieldNames": ["Crr", "GoalId", "WeeklySpendLimit"],
}
for v in ("v501", "v5"):
    show("Strategies", v, "strategies", STRATEGY_PARAMS, "Strategies")

# Bogus-значение вскрывает допустимые FieldNames strategies, если форма выше кривая.
try:
    _, _, body = call("v501", "strategies", {"SelectionCriteria": {}, "FieldNames": ["Bogus"]})
    print("Strategies bogus-FieldNames:", json.dumps(body.get("error", {}), ensure_ascii=False)[:600])
except Exception as exc:  # noqa: BLE001
    print("Strategies bogus: THROW", exc)
