# Проба Ф6 (раунд 5): чем на самом деле заполнены параметры ЕПК LIME —
# приоритетные цели (единицы Value), стратегия (бюджет/CPA/ДРР/цель) и полный
# enum настроек кампании. Нужно, чтобы показать это человеку, а не сырые enum'ы.
# Read-only (только get).
import json
import os

import requests

TOKEN = os.environ["DIRECT_TOKEN"]
LOGIN = os.environ["DIRECT_CLIENT_LOGIN"]
V501 = "https://api.direct.yandex.com/json/v501"


def call(service: str, params: dict, method: str = "get") -> dict:
    resp = requests.post(
        f"{V501}/{service}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Client-Login": LOGIN,
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        },
        data=json.dumps({"method": method, "params": params}, ensure_ascii=False).encode("utf-8"),
        timeout=120,
    )
    try:
        return resp.json()
    except Exception:
        return {"error": {"error_code": resp.status_code, "error_string": "не JSON", "error_detail": resp.text[:300]}}


def err(body: dict) -> str | None:
    e = body.get("error")
    if not e:
        return None
    return f"ERROR {e.get('error_code')} {e.get('error_string')}: {e.get('error_detail')}"


print("=== 1. Полный enum UnifiedCampaignFieldNames (bogus) ===")
print("  ", err(call("campaigns", {"SelectionCriteria": {}, "FieldNames": ["Id"], "UnifiedCampaignFieldNames": ["Bogus"], "Page": {"Limit": 1}})))

print("\n=== 2. Параметры активных ЕПК: цели, стратегия, настройки ===")
res = call(
    "campaigns",
    {
        "SelectionCriteria": {"Types": ["UNIFIED_CAMPAIGN"], "States": ["ON"]},
        "FieldNames": ["Id", "Name", "Type", "Status", "State", "DailyBudget", "Funds", "Currency"],
        "UnifiedCampaignFieldNames": [
            "BiddingStrategy",
            "PackageBiddingStrategy",
            "PriorityGoals",
            "Settings",
            "CounterIds",
            "AttributionModel",
            "WeeklyBudgetRollover",
        ],
        "Page": {"Limit": 8},
    },
)
if err(res):
    print("  ", err(res))
    raise SystemExit(1)

settings_seen: dict[str, set] = {}
for c in res["result"]["Campaigns"]:
    uc = c.get("UnifiedCampaign") or {}
    print(f"\n  --- {c['Id']} {c.get('Name')} ---")
    print("   DailyBudget:", json.dumps(c.get("DailyBudget"), ensure_ascii=False))
    print("   PriorityGoals:", json.dumps(uc.get("PriorityGoals"), ensure_ascii=False)[:600])
    print("   BiddingStrategy:", json.dumps(uc.get("BiddingStrategy"), ensure_ascii=False)[:800])
    print("   PackageBiddingStrategy:", json.dumps(uc.get("PackageBiddingStrategy"), ensure_ascii=False)[:300])
    print("   Settings:", json.dumps(uc.get("Settings"), ensure_ascii=False)[:700])
    for s in uc.get("Settings") or []:
        settings_seen.setdefault(s.get("Option", "?"), set()).add(s.get("Value"))

print("\n=== 3. Какие опции настроек встречаются и с какими значениями ===")
for opt in sorted(settings_seen):
    print(f"  {opt}: {sorted(settings_seen[opt])}")

print("\n=== 4. Пакетные стратегии аккаунта целиком ===")
st = call(
    "strategies",
    {
        "SelectionCriteria": {},
        "FieldNames": ["Id", "Name", "Type"],
        "StrategyMaximumClicksFieldNames": ["WeeklySpendLimit"],
        "StrategyMaximumConversionRateFieldNames": ["WeeklySpendLimit", "GoalId"],
        "StrategyAverageCpcFieldNames": ["AverageCpc", "WeeklySpendLimit"],
        "StrategyAverageCpaFieldNames": ["AverageCpa", "GoalId", "WeeklySpendLimit"],
        "StrategyAverageCrrFieldNames": ["Crr", "GoalId", "WeeklySpendLimit"],
        "StrategyPayForConversionFieldNames": ["Cpa", "GoalId", "WeeklySpendLimit"],
        "StrategyPayForConversionCrrFieldNames": ["Crr", "GoalId", "WeeklySpendLimit"],
        "Page": {"Limit": 6},
    },
)
print("  ", err(st) or json.dumps(st["result"], ensure_ascii=False)[:1500])
