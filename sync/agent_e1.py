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

2. Одна запись bidmodifiers.get → максимум одна нормализованная actual-запись.
   Запись DemographicsAdjustment может нести Gender И Age ОДНОВРЕМЕННО (ставка
   на пересечение сегментов). Это ОДИН объект Директа с одним Id и одним
   коэффициентом, и он не эквивалентен паре одномерных: «мужчины 25–34» — не
   «мужчины». Такая запись сворачивается в одну actual-запись с составным
   ключом, который заведомо не сойдётся с одномерным планом (подробности — в
   _normalize_actual). Прежняя раскладка на две записи с одним Id выпускала из
   diff два изменения на один физический объект.
"""

import json
import os
import sys
from datetime import date, timedelta
from typing import Any, Dict, List, Set, Tuple

from sync.agent import db as agent_db
from sync.agent.writer import db as writer_db
from sync.agent.writer.apply import apply_actions
from sync.agent.writer.client import WriteClient
from sync.agent.writer.diff import diff_modifiers
from sync.agent.writer.guardrails import (
    MAX_ACTIONS_PER_RUN,
    cap_actions,
    check_action,
    check_holdout,
)
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

# Настройки читаются строго по кабинету (agent_db.load_latest_computed_settings).
# Пустой ответ — не «нечего делать»: он значит либо что Э0 по этому кабинету не
# проходил, либо что в таблице лежат строки СТАРОГО формата, записанные под общим
# идентификатором на все кабинеты сразу. Старые строки читать нельзя — в них
# выжили числа одного кабинета, перетершие остальные; прогон честно ничего не
# применит, но это обязано быть видно в отчёте, а не выглядеть как тишина.
NO_COMPUTED_REASON = (
    "нет вычисленных настроек кабинета {login} (object_level='account', "
    "object_id='{login}'): либо расчёт Э0 по этому кабинету не проходил, либо в "
    "таблице лежат строки старого формата под общим object_id — они не читаются "
    "намеренно, потому что схлопывали кабинеты в один набор"
)

# Сколько минут строка может простоять в статусе planned, прежде чем считаться
# зависшей. Прогон живёт минуты; всё, что старше, — след обрыва прошлого прогона
# ПОСЛЕ отправки запроса (см. writer/db.py::STALE_PLANNED_SQL).
STALE_PLANNED_MINUTES = 60

# Сколько действий показать поимённо в отчёте. Остальное — агрегатом по видам
# настроек: полный список из полусотни строк превращает отчёт в стену текста,
# а он читается глазами перед решением включать боевую запись.
PREVIEW_SAMPLE_LIMIT = 5

# Поле ответа bidmodifiers.get → (тип корректировки, ключ в форме плана).
# Устройство у Директа — три РАЗНЫХ типа корректировки, а не один мобильный;
# ключи в верхнем регистре, как в plan.DEVICE_TYPE_MAP, иначе diff не сойдётся.
_DEVICE_ADJUSTMENTS = (
    ("MobileAdjustment", "MOBILE_ADJUSTMENT", "MOBILE"),
    ("DesktopAdjustment", "DESKTOP_ADJUSTMENT", "DESKTOP"),
    ("TabletAdjustment", "TABLET_ADJUSTMENT", "TABLET"),
)

# Корректировка, несущая сразу несколько измерений (пол И возраст), — это НЕ
# сумма одномерных: «мужчины 25–34» и «мужчины» в Директе разные объекты с
# разными ставками. Такой факт нормализуется составным ключом, который заведомо
# не совпадает ни с одним ключом плана (в плане ключ всегда одномерный), и
# разнотипной меткой — чтобы план не сопоставился с ним по случайности.
COMPOSITE_KEY_SEPARATOR = "+"
COMPOSITE_TYPE = "COMPOSITE_ADJUSTMENT"


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
    """Одна запись bidmodifiers.get → 0 или 1 нормализованная actual-запись.

    Три обязанности, все — про совпадение факта с планом:

    1. Единицы. API отдаёт 100-базный коэффициент (100 = нейтраль), план
       живёт в дельтах — здесь стоит обратная граница конверсии
       (units.api_to_delta), парная той, что в apply.to_api_call. Без неё
       diff сравнивал бы 130 с 30 и переписывал корректировку на каждом
       прогоне.
    2. Ключи. Форма ключа обязана совпадать с планом (plan.DEVICE_TYPE_MAP,
       верхний регистр): устройство — это ТРИ разных типа корректировки, и
       ключ "mobile" строчными не сойдётся с "MOBILE" из плана никогда,
       из-за чего diff вечно предлагал бы add там, где нужен set.
    3. Один объект — одна запись. Запись bidmodifiers.get это ОДИН физический
       объект в Директе с одним Id и одним коэффициентом. Раскладка её на
       несколько normalized-записей с ОДНИМ И ТЕМ ЖЕ Id выпускала из diff два
       изменения на один объект: второе затирало первое, оба списывали
       риск-бюджет, оба сохраняли прошлое состояние, снятое ДО первого, —
       откат вернул бы объект не туда, откуда агент его вывел.

    Многомерная корректировка («мужчины 25–34» — Gender И Age одновременно)
    сворачивается в ОДНУ запись с составным ключом (GENDER_MALE+AGE_25_34).
    Составной ключ не совпадает ни с одним одномерным ключом плана — и это
    правильно: коэффициент, посчитанный для всего мужского сегмента, к
    пересечению «мужчины 25–34» не относится, ставить его туда значит править
    не тот объект. Diff увидит, что одномерной корректировки в кабинете нет, и
    предложит добавить её отдельным объектом, не трогая многомерную —
    в Директе это разные объекты, и они сосуществуют штатно.
    """
    dimensions: List[Tuple[str, str, int]] = []  # (тип, ключ, дельта)

    for api_field, direct_type, key in _DEVICE_ADJUSTMENTS:
        adjustment = item.get(api_field) or {}
        if adjustment:
            dimensions.append(
                (direct_type, key, api_to_delta(adjustment.get("BidModifier") or 0)))

    demo = item.get("DemographicsAdjustment") or {}
    if demo:
        percent = api_to_delta(demo.get("BidModifier") or 0)
        for value in (demo.get("Gender"), demo.get("Age")):
            if value:
                dimensions.append(("DEMOGRAPHICS_ADJUSTMENT", str(value), percent))

    regional = item.get("RegionalAdjustment") or {}
    if regional:
        dimensions.append(("REGIONAL_ADJUSTMENT", str(regional.get("RegionId") or ""),
                           api_to_delta(regional.get("BidModifier") or 0)))

    if not dimensions:
        return []
    if len(dimensions) == 1:
        direct_type, key, percent = dimensions[0]
        return [{"Id": item["Id"], "Type": direct_type, "key": key, "percent": percent}]

    types = {t for t, _, _ in dimensions}
    return [{
        "Id": item["Id"],
        # Измерения одного типа (пол+возраст) сохраняют свой тип; разнотипная
        # комбинация типу не принадлежит вообще — своя метка, чтобы она не
        # сошлась с планом по чистой случайности.
        "Type": dimensions[0][0] if len(types) == 1 else COMPOSITE_TYPE,
        "key": COMPOSITE_KEY_SEPARATOR.join(k for _, k, _ in dimensions),
        "percent": dimensions[0][2],
        "composite": True,
    }]


def _unsupported_report(unsupported: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Неприменимая часть плана для отчёта прогона: сколько и почему.

    Без этого блока «регион временно не применяется» выглядело бы как
    «региона в данных нет» — и пауза жила бы незамеченной месяцами.
    """
    by_reason: Dict[str, int] = {}
    for row in unsupported:
        by_reason[row["reason"]] = by_reason.get(row["reason"], 0) + 1
    return {"count": len(unsupported), "by_reason": by_reason}


def action_label(action: Dict[str, Any]) -> str:
    """Короткая подпись действия: что и на сколько правится."""
    payload = action.get("payload") or {}
    percent = int(payload.get("BidModifier") or 0)
    kind = str(action.get("action_kind") or "").split(".")[-1]
    return f"{action.get('direct_type')}:{action.get('key')} {percent:+d}% ({kind})"


def actions_preview(
    actions: List[Dict[str, Any]], limit: int = PREVIEW_SAMPLE_LIMIT
) -> Dict[str, Any]:
    """Состав действий для отчёта прогона: сколько и какие именно.

    Без этого блока репетиция (--prod без --apply) показывала ровные нули и не
    содержала ни числа готовых действий, ни их состава — то есть главный
    артефакт для решения «включать ли боевую запись» не показывал, что именно
    было бы записано.

    Форма компактная и не растёт со числом действий: агрегат по видам настроек
    (их единицы) плюс несколько примеров с кампаниями. Полный список — в
    журнале действий, отчёт его не дублирует.
    """
    by_setting: Dict[str, int] = {}
    for action in actions:
        label = action_label(action)
        by_setting[label] = by_setting.get(label, 0) + 1
    return {
        "count": len(actions),
        "by_setting": dict(sorted(by_setting.items())),
        "sample": [f"кампания {a.get('object_id')}: {action_label(a)}"
                   for a in actions[:limit]],
        "sample_truncated": len(actions) > limit,
    }


def _stale_report(rows: List[Dict[str, Any]], limit: int = PREVIEW_SAMPLE_LIMIT) -> Dict[str, Any]:
    """Зависшие planned-строки для отчёта: сколько, какие, с какого времени."""
    return {
        "count": len(rows),
        "older_than_minutes": STALE_PLANNED_MINUTES,
        "sample": [
            {"action_id": r.get("action_id"), "object_id": r.get("object_id"),
             "action_kind": r.get("action_kind"), "created_at": str(r.get("created_at"))}
            for r in rows[:limit]
        ],
    }


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

    # Лимит действий — на ПРОГОН, а не на кабинет: рельса ограничивает объём
    # изменений, которые человек способен проверить и осмысленно откатить, а
    # он не зависит от того, на сколько кабинетов эти изменения разложены.
    # Внутри цикла по четырём кабинетам потолок был вчетверо выше заявленного.
    remaining_cap = MAX_ACTIONS_PER_RUN
    # Объекты, риск которых прогон уже оплатил (см. risk.fit_into_budget).
    charged_objects: Set[str] = set()

    for client_info in clients:
        login = client_info["login"]

        # Настройки — этого кабинета, а не общий набор на всех: числа посчитаны
        # по его аудитории и применимы только к его кампаниям.
        computed = agent_db.load_latest_computed_settings(login)
        plan = plan_bid_modifiers(computed)
        desired = plan["desired"]
        # Значимые настройки, которые агент применить не умеет (нечисловой ключ
        # региона, устройство вне DESKTOP/MOBILE/TABLET). Они не подставляются в
        # чужой тип корректировки и не роняют применение — но и не пропадают
        # молча: причина видна в отчёте прогона.
        unsupported = _unsupported_report(plan["unsupported"])
        if not desired:
            print(json.dumps({
                "account": login,
                "verdict": "NO_COMPUTED_SETTINGS" if not computed else "NOTHING_TO_DO",
                "reason": (NO_COMPUTED_REASON.format(login=login) if not computed
                           else "нет значимых корректировок"),
                "computed_settings": len(computed),
                "unsupported": unsupported,
                "stale_planned": _stale_report(
                    writer_db.stale_planned(STALE_PLANNED_MINUTES, account=login)),
            }, ensure_ascii=False, indent=2))
            continue

        client = WriteClient(login, sandbox=sandbox, dry_run=dry_run)

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
                planned.append({**action, "account": login})

        allowed, in_holdout = check_holdout(planned, holdout_ids)
        blocked += [{**a, "blocked_reason": "заповедник"} for a in in_holdout]

        # Порядок рельс: сначала отсекается всё, что применять нельзя или
        # незачем (лимит прогона, отсутствие красной линии), и только потом
        # считается бюджет. Обратный порядок списывал бы риск за действия,
        # которые дальше отваливаются, — объект помечался бы оплаченным, а
        # изменение по нему так и не уходило бы в кабинет.
        allowed, over_cap = cap_actions(allowed, max_per_run=max(remaining_cap, 0))

        # Красная линия ставится ВМЕСТЕ с действием: у каждого применённого
        # изменения заранее известно, при каком исходе оно считается провалом.
        # build_red_line возвращает None, если её посчитать не из чего — такое
        # действие не применяется, причина уходит в no_red_line, а не в тихий
        # дефолт-плейсхолдер.
        with_red_line: List[Dict[str, Any]] = []
        no_red_line: List[Dict[str, Any]] = []
        for a in allowed:
            red_line = build_red_line(a, baseline_cpa, absolute_max_cpa)
            if red_line is None:
                no_red_line.append({**a, "blocked_reason": NO_RED_LINE_REASON})
                continue
            with_red_line.append({**a, "red_line": red_line})

        risks = {a["idempotency_key"]: action_risk(a, daily_cost) for a in with_red_line}
        # Бюджет читается заново для каждого кабинета: он общий на весь прогон,
        # а не на кабинет, и предыдущий клиент этого же прогона мог его уже
        # частично занять (spent_risk читает applied_at из журнала, куда
        # apply_actions уже успел записать применённые действия).
        remaining = writer_db.risk_limit(wk, DEFAULT_WEEKLY_RISK_RUB) - writer_db.spent_risk(wk)
        prepared, deferred = fit_into_budget(with_red_line, risks, remaining, charged_objects)
        remaining_cap -= len(prepared)

        stale = writer_db.stale_planned(STALE_PLANNED_MINUTES, account=login)
        report = apply_actions(client, prepared, writer_db)

        print(json.dumps({
            "account": login,
            "sandbox": sandbox,
            "dry_run": dry_run,
            "own_campaigns": len(campaign_ids),
            "computed_settings": len(computed),
            "desired": len(desired),
            "unsupported": unsupported,
            "planned": len(planned),
            "blocked": len(blocked),
            "deferred_by_risk": len(deferred),
            "deferred_by_cap": len(over_cap),
            "actions_left_in_run": max(remaining_cap, 0),
            "no_red_line": {
                "count": len(no_red_line),
                "reason": NO_RED_LINE_REASON if no_red_line else None,
            },
            "remaining_risk_rub": round(remaining, 2),
            "risk_charged_rub": round(sum(a["risk_rub"] for a in prepared), 2),
            "absolute_max_cpa": absolute_max_cpa,
            # Состав того, что уходит (или ушло бы) в кабинет. В режиме
            # репетиции это единственное место, где он вообще виден.
            "prepared": actions_preview(prepared),
            "stale_planned": _stale_report(stale),
            "result": {k: v for k, v in report.items() if k != "details"},
            "units_left": client.units_left,
        }, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
