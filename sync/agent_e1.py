# -*- coding: utf-8 -*-
"""
sync/agent_e1.py — прогон Э1a: применение вычисленных настроек.

Порядок: гейт данных → план → свежий факт из API → diff → рельсы → заповедник →
риск-бюджет → применение → сторож красных линий.

По умолчанию ПЕСОЧНИЦА и DRY-RUN. Боевая запись требует двух явных флагов:
--prod и --apply. Это не перестраховка: единственный необратимый шаг здесь —
касание живого кабинета, и он должен быть намеренным.

Запуск:
    python -m sync.agent_e1                    # песочница, dry-run
    python -m sync.agent_e1 --prod             # боевой кабинет, dry-run
    python -m sync.agent_e1 --prod --apply     # боевая запись
ENV: DATABASE_URL, DIRECT_TOKEN, DIRECT_CLIENTS_JSON

Два отклонения от исходного плана задачи (см. task-9-report.md):

1. Список кампаний кабинета берётся через campaigns.get ЭТОГО кабинета,
   а не как срез справочника расходов по всем кабинетам сразу. Справочник
   расходов (edu_agent_facts) копит кампании ВСЕХ клиентов в одной таблице;
   без пересечения с собственным списком кампаний агент слал бы
   bidmodifiers.get по чужим Id — гарантированная ошибка "объект не найден"
   и лишние Units на чужой кабинет.

2. Демографические корректировки нормализуются построчно: одна запись
   DemographicsAdjustment в ответе API может нести Gender И Age ОДНОВРЕМЕННО
   (ставка на пересечение сегментов). Diff сопоставляет план с фактом по паре
   (Type, key) — если такую запись свернуть в один ключ, вторая половина
   потеряется и diff предложит add там, где нужен set, создав дубль
   корректировки в кабинете.
"""

import json
import os
import sys
from datetime import date, timedelta
from typing import Any, Dict, List

from sync.agent import db as agent_db
from sync.agent.writer import db as writer_db
from sync.agent.writer.apply import apply_actions
from sync.agent.writer.client import WriteClient
from sync.agent.writer.diff import diff_modifiers
from sync.agent.writer.guardrails import cap_actions, check_action, check_holdout
from sync.agent.writer.plan import plan_bid_modifiers
from sync.agent.writer.risk import action_risk, fit_into_budget, median, week_start
from sync.agent.writer.rollback import red_line_for
from sync.agent.writer.units import api_to_delta

DEFAULT_WEEKLY_RISK_RUB = 50_000.0
CAMPAIGN_PAGE_LIMIT = 1000

# Множитель медианы базового CPA → абсолютный аварийный порог красной линии
# для кампаний без собственной базы (rollback.py::red_line_for, has_baseline=
# False). Медиана — типичный CPA портфеля, а не потолок для конкретной новой
# или непредсказуемой кампании: x2 даёт запас, прежде чем считать результат
# провалом, но не настолько широкий, чтобы автооткат никогда не сработал —
# тот же порядок величины, что и относительный потолок для кампаний с базой
# (RED_LINE_TOLERANCE = +40% в rollback.py — здесь просто нет базы для %).
ABSOLUTE_MAX_CPA_MULTIPLIER = 2.0

# Причина, по которой действие не применяется, когда абсолютный порог
# посчитать не из чего вообще (справочник базовых CPA пуст целиком).
NO_RED_LINE_REASON = "нет базового CPA ни у одной кампании — красная линия недостижима"

# Поле ответа bidmodifiers.get → (тип корректировки, ключ в форме плана).
# Устройство у Директа — три РАЗНЫХ типа корректировки, а не один мобильный;
# ключи в верхнем регистре, как в plan.DEVICE_TYPE_MAP, иначе diff не сойдётся.
_DEVICE_ADJUSTMENTS = (
    ("MobileAdjustment", "MOBILE_ADJUSTMENT", "MOBILE"),
    ("DesktopAdjustment", "DESKTOP_ADJUSTMENT", "DESKTOP"),
    ("TabletAdjustment", "TABLET_ADJUSTMENT", "TABLET"),
)


def _clients() -> List[Dict[str, Any]]:
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    out: List[Dict[str, Any]] = []
    if raw:
        for item in json.loads(raw):
            if isinstance(item, dict) and str(item.get("login", "")).strip():
                out.append({"login": item["login"]})
    return out


def fetch_campaign_ids(client: WriteClient) -> List[int]:
    """Id всех кампаний ОДНОГО кабинета (форма — sync/agent/segments.py::fetch_campaign_ids).

    Постранично: Page.Limit/Offset, остановка когда страница короче лимита.
    """
    out: List[int] = []
    offset = 0
    while True:
        result = client.get("campaigns", {
            "SelectionCriteria": {},
            "FieldNames": ["Id"],
            "Page": {"Limit": CAMPAIGN_PAGE_LIMIT, "Offset": offset},
        })
        items = result.get("Campaigns") or []
        out += [int(c["Id"]) for c in items]
        if len(items) < CAMPAIGN_PAGE_LIMIT:
            break
        offset += CAMPAIGN_PAGE_LIMIT
    return out


def own_campaign_ids(client: WriteClient, daily_cost_by_campaign: Dict[str, float]) -> List[str]:
    """Кампании ЭТОГО кабинета, пересечённые со справочником расходов.

    daily_cost_by_campaign построен по ВСЕМ кабинетам сразу — без пересечения
    с собственным списком кампаний агент опрашивал бы чужие Id чужим логином.
    """
    own = {str(i) for i in fetch_campaign_ids(client)}
    return sorted(own & set(daily_cost_by_campaign.keys()))


def _normalize_actual(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Одна запись bidmodifiers.get → 0..N нормализованных actual-записей.

    Две обязанности, обе — про совпадение факта с планом:

    1. Единицы. API отдаёт 100-базный коэффициент (100 = нейтраль), план
       живёт в дельтах — здесь стоит обратная граница конверсии
       (units.api_to_delta), парная той, что в apply.to_api_call. Без неё
       diff сравнивал бы 130 с 30 и переписывал корректировку на каждом
       прогоне.
    2. Ключи. Форма ключа обязана совпадать с планом (plan.DEVICE_TYPE_MAP,
       верхний регистр): устройство — это ТРИ разных типа корректировки, и
       ключ "mobile" строчными не сойдётся с "MOBILE" из плана никогда,
       из-за чего diff вечно предлагал бы add там, где нужен set.

    Демографическая корректировка может нести Gender И Age одновременно —
    такая запись раскладывается в ДВЕ отдельные normalized-записи с разными
    ключами (но одним Id: обе указывают на один физический объект в Директе).
    Без этого diff терял бы вторую половину и предлагал add вместо set.
    """
    out: List[Dict[str, Any]] = []
    demo = item.get("DemographicsAdjustment") or {}
    regional = item.get("RegionalAdjustment") or {}

    for api_field, direct_type, key in _DEVICE_ADJUSTMENTS:
        adjustment = item.get(api_field) or {}
        if adjustment:
            out.append({"Id": item["Id"], "Type": direct_type, "key": key,
                        "percent": api_to_delta(adjustment.get("BidModifier") or 0)})
    if demo:
        percent = api_to_delta(demo.get("BidModifier") or 0)
        gender = demo.get("Gender")
        age = demo.get("Age")
        if gender:
            out.append({"Id": item["Id"], "Type": "DEMOGRAPHICS_ADJUSTMENT",
                        "key": gender, "percent": percent})
        if age:
            out.append({"Id": item["Id"], "Type": "DEMOGRAPHICS_ADJUSTMENT",
                        "key": age, "percent": percent})
    if regional:
        out.append({"Id": item["Id"], "Type": "REGIONAL_ADJUSTMENT",
                    "key": str(regional.get("RegionId") or ""),
                    "percent": api_to_delta(regional.get("BidModifier") or 0)})
    return out


def _unsupported_report(unsupported: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Неприменимая часть плана для отчёта прогона: сколько и почему.

    Без этого блока «регион временно не применяется» выглядело бы как
    «региона в данных нет» — и пауза жила бы незамеченной месяцами.
    """
    by_reason: Dict[str, int] = {}
    for row in unsupported:
        by_reason[row["reason"]] = by_reason.get(row["reason"], 0) + 1
    return {"count": len(unsupported), "by_reason": by_reason}


def absolute_max_cpa_from_baseline(baseline_cpa: Dict[str, float]) -> Any:
    """Абсолютный аварийный порог красной линии: медиана известных базовых
    CPA × ABSOLUTE_MAX_CPA_MULTIPLIER. None, если справочник пуст целиком —
    медианы не существует, порог из данных не выводится.
    """
    med = median(baseline_cpa.values())
    if med is None:
        return None
    return round(med * ABSOLUTE_MAX_CPA_MULTIPLIER, 2)


def build_red_line(
    action: Dict[str, Any], baseline_cpa: Dict[str, float], absolute_max_cpa: Any,
) -> Any:
    """Красная линия для действия, или None, если её посчитать не из чего.

    У кампании есть собственный baseline_cpa (>0) — относительный порог,
    absolute_max_cpa не нужен (red_line_for уйдёт в относительную ветку и
    не тронет этот параметр, даже если он None). Нет — нужен absolute_max_cpa
    (медиана по справочнику, см. absolute_max_cpa_from_baseline); если и его
    нет (справочник baseline_cpa пуст целиком), у действия не будет
    работающей красной линии вообще — применять его нельзя, вызывающий код
    обязан исключить его до apply_actions, а не передавать дальше с тихим
    дефолт-плейсхолдером.

    red_line_for после правки по код-ревью не имеет дефолта для
    absolute_max_cpa — вызов без явного порога стал бы TypeError. Ветка
    "своей базы нет и абсолютного порога тоже нет" уже отсечена гардом выше,
    поэтому здесь всегда безопасно передать absolute_max_cpa как есть.
    """
    baseline = {"cpa": baseline_cpa.get(str(action["object_id"]), 0.0)}
    if baseline["cpa"] <= 0 and absolute_max_cpa is None:
        return None
    return red_line_for(action, baseline, absolute_max_cpa)


def _actual_modifiers(client: WriteClient, campaign_id: str) -> List[Dict[str, Any]]:
    """Свежее состояние корректировок кампании: между прогонами кабинет могли
    править руками, поэтому читаем заново на каждом прогоне, а не берём из журнала.

    Levels обязателен и лежит ВНУТРИ SelectionCriteria (probe задачи 1, факт
    подтверждён прогонами 32217815538 и др.) — без него запрос отвергается
    ошибкой 8000 «Отсутствует обязательный параметр Levels».
    """
    result = client.get("bidmodifiers", {
        "SelectionCriteria": {"CampaignIds": [int(campaign_id)], "Levels": ["CAMPAIGN"]},
        "FieldNames": ["Id", "CampaignId", "Type"],
        "MobileAdjustmentFieldNames": ["BidModifier"],
        "DesktopAdjustmentFieldNames": ["BidModifier"],
        "TabletAdjustmentFieldNames": ["BidModifier"],
        "DemographicsAdjustmentFieldNames": ["BidModifier", "Gender", "Age"],
        "RegionalAdjustmentFieldNames": ["BidModifier", "RegionId"],
    })
    out: List[Dict[str, Any]] = []
    for item in result.get("BidModifiers") or []:
        out += _normalize_actual(item)
    return out


def main() -> int:
    sandbox = "--prod" not in sys.argv
    dry_run = "--apply" not in sys.argv
    today = date.today().isoformat()

    writer_db.ensure_writer_tables()

    computed = agent_db.load_latest_computed_settings()
    plan = plan_bid_modifiers(computed)
    desired = plan["desired"]
    # Значимые настройки, которые агент применить не умеет (нечисловой ключ
    # региона, устройство вне DESKTOP/MOBILE/TABLET). Они не подставляются в
    # чужой тип корректировки и не роняют применение — но и не пропадают
    # молча: причина видна в отчёте прогона.
    unsupported = _unsupported_report(plan["unsupported"])
    if not desired:
        print(json.dumps({"verdict": "NOTHING_TO_DO", "reason": "нет значимых корректировок",
                          "unsupported": unsupported},
                         ensure_ascii=False, indent=2))
        return 0

    clients = _clients()
    if not clients:
        print(json.dumps({"verdict": "NOTHING_TO_DO", "reason": "нет кабинетов в DIRECT_CLIENTS_JSON"},
                         ensure_ascii=False, indent=2))
        return 0

    holdout_ids = set(agent_db.load_holdout_ids())
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    daily_cost = agent_db.load_daily_cost_by_campaign(cutoff, today)
    baseline_cpa = agent_db.load_baseline_cpa(cutoff, today)
    wk = week_start(today)

    # Абсолютный аварийный порог красной линии считается один раз на весь
    # прогон из уже загруженного baseline_cpa — те же данные, тот же приём
    # медианы, что и для неизвестного дневного расхода в risk.py. Справочник
    # пуст целиком (None) — абсолютный порог из данных не выводится: такие
    # действия ниже явно исключаются из применения, а не тихо получают
    # дефолт-плейсхолдер, никак не связанный с экономикой кабинета.
    absolute_max_cpa = absolute_max_cpa_from_baseline(baseline_cpa)

    for client_info in clients:
        client = WriteClient(client_info["login"], sandbox=sandbox, dry_run=dry_run)

        # Рулинг 1: кампании только этого кабинета, не всего справочника расходов.
        campaign_ids = own_campaign_ids(client, daily_cost)

        planned: List[Dict[str, Any]] = []
        blocked: List[Dict[str, Any]] = []

        for campaign_id in campaign_ids:
            actual = _actual_modifiers(client, campaign_id)
            for action in diff_modifiers(desired, actual, campaign_id):
                ok, reason = check_action(action)
                if not ok:
                    blocked.append({**action, "blocked_reason": reason})
                    continue
                planned.append({**action, "account": client_info["login"]})

        allowed, in_holdout = check_holdout(planned, holdout_ids)
        blocked += [{**a, "blocked_reason": "заповедник"} for a in in_holdout]

        risks = {a["idempotency_key"]: action_risk(a, daily_cost) for a in allowed}
        # Бюджет читается заново для каждого кабинета: он общий на весь прогон,
        # а не на кабинет, и предыдущий клиент этого же прогона мог его уже
        # частично занять (spent_risk читает applied_at из журнала, куда
        # apply_actions уже успел записать применённые действия).
        remaining = writer_db.risk_limit(wk, DEFAULT_WEEKLY_RISK_RUB) - writer_db.spent_risk(wk)
        fits, deferred = fit_into_budget(allowed, risks, remaining)
        fits, over_cap = cap_actions(fits)

        # Красная линия ставится ВМЕСТЕ с действием: у каждого применённого
        # изменения заранее известно, при каком исходе оно считается провалом.
        # build_red_line возвращает None, если её посчитать не из чего — такое
        # действие не применяется, причина уходит в no_red_line, а не в тихий
        # дефолт-плейсхолдер.
        prepared = []
        no_red_line: List[Dict[str, Any]] = []
        for a in fits:
            red_line = build_red_line(a, baseline_cpa, absolute_max_cpa)
            if red_line is None:
                no_red_line.append({**a, "blocked_reason": NO_RED_LINE_REASON})
                continue
            prepared.append({
                **a,
                "risk_rub": risks[a["idempotency_key"]],
                "red_line": red_line,
            })
        report = apply_actions(client, prepared, writer_db)

        print(json.dumps({
            "account": client_info["login"],
            "sandbox": sandbox,
            "dry_run": dry_run,
            "own_campaigns": len(campaign_ids),
            "desired": len(desired),
            "unsupported": unsupported,
            "planned": len(planned),
            "blocked": len(blocked),
            "deferred_by_risk": len(deferred),
            "deferred_by_cap": len(over_cap),
            "no_red_line": {
                "count": len(no_red_line),
                "reason": NO_RED_LINE_REASON if no_red_line else None,
            },
            "remaining_risk_rub": round(remaining, 2),
            "absolute_max_cpa": absolute_max_cpa,
            "result": {k: v for k, v in report.items() if k != "details"},
            "units_left": client.units_left,
        }, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
