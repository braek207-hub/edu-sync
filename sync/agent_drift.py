# -*- coding: utf-8 -*-
"""
sync/agent_drift.py — прогон сверки журнала с кабинетом.

Отвечает на вопрос, который агент не может задать сам себе: то, что он
записал в журнал как применённое, ДО СИХ ПОР ли стоит в кабинете. Правило
«проверять у получателя»: успешный ответ API доказывает отправку, а не
состояние. Между прогоном применения и вердиктом наблюдения проходят дни, и
за эти дни настройку возвращают руками, не применяет пакетная стратегия,
кампанию архивируют — а сторож всё это время судит эффект изменения,
которого в кабинете нет.

Прогон только ЧИТАЕТ: ни одного вызова изменения, ни одной отметки в
журнале. Поэтому у него нет боевого режима — есть только выбор кабинета,
и по умолчанию это песочница, как у остальных тактов.

Запуск:
    python -m sync.agent_drift                # песочница
    python -m sync.agent_drift --prod         # боевой кабинет
    python -m sync.agent_drift --prod --days=14
    python -m sync.agent_drift --prod --fail-on-alarm   # расхождение = красный ран
ENV: DATABASE_URL, DIRECT_TOKEN
"""

import json
import sys
from typing import Any, Dict, List

from sync.agent import blackbox, drift
from sync.agent.writer.client import WriteClient
from sync.agent.writer.db import LIVE_STATUSES
from sync.db import get_connection

# Глубина окна сверки. Смысл границы — горизонт наблюдения сторожа: строки
# старше него уже отсужены, и расхождение по ним — история, а не сигнал.
DEFAULT_DAYS = 21

# Строки журнала, за которыми стоит ЖИВОЕ изменение в кабинете. Статусы
# берутся из writer/db.py, а не переписываются сюда: определение «живого»
# одно на всю систему, и его расхождение между сторожем и сверкой означало
# бы, что две проверки смотрят на разные множества.
APPLIED_ACTIONS_SQL = """
    SELECT action_id, account, object_level, object_id, action_kind,
           direct_type, setting_key, payload, previous_state,
           applied_at, status
      FROM edu_agent_actions
     WHERE status = ANY(%(statuses)s)
       AND applied_at IS NOT NULL
       AND applied_at >= now() - make_interval(days => %(days)s)
     ORDER BY applied_at DESC
"""


def applied_actions(days: int = DEFAULT_DAYS) -> List[Dict[str, Any]]:
    import psycopg2.extras

    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(APPLIED_ACTIONS_SQL,
                        {"statuses": list(LIVE_STATUSES), "days": int(days)})
            return [dict(row) for row in cur.fetchall()]


def fetch_state(client, campaign_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Текущее состояние кампаний кабинета — свежим чтением.

    Поля ровно те, по которым написана сверка (sync/agent/drift.py): статус
    кампании, дневной бюджет, почасовое расписание и блок стратегии, внутри
    которого лежат недельный лимит и цель конверсии.
    """
    out: Dict[str, Dict[str, Any]] = {}
    ids = sorted({int(c) for c in campaign_ids if str(c).isdigit()})
    page = 1000
    for start in range(0, len(ids), page):
        result = client.get("campaigns", {
            "SelectionCriteria": {"Ids": ids[start:start + page]},
            "FieldNames": ["Id", "Type", "State", "Status", "DailyBudget",
                           "TimeTargeting"],
            "TextCampaignFieldNames": ["BiddingStrategy"],
        })
        for item in result.get("Campaigns") or []:
            out[str(item.get("Id"))] = {
                "campaign_type": item.get("Type"),
                "state": item.get("State"),
                "status": item.get("Status"),
                "daily_budget": item.get("DailyBudget"),
                "time_targeting": item.get("TimeTargeting"),
                "strategy": (item.get("TextCampaign") or {}).get("BiddingStrategy"),
            }
    return out


# Сколько расхождений уезжает в отчёт строками. Счётчики видны всегда;
# подробности нужны для первого взгляда, а полный список лежит в чёрном
# ящике вместе с отчётом.
SAMPLE_LIMIT = 20


def fetch_modifiers(client, campaign_ids: List[str]) -> Dict[str, Dict[Any, Any]]:
    """Корректировки ставок кампаний → {кампания: {(вид, ключ): дельта}}.

    Чтение и нормализация — те же, что у прогона применения
    (sync/agent_e1._actual_modifiers): сверка обязана видеть ровно то, что
    видел планировщик, иначе она сравнивала бы дельты с 100-базными
    коэффициентами и объявляла дрейфом каждую корректировку.

    Импорт внутри функции: прогон применения тянет за собой весь расчётный
    слой, и чистая сверка не должна поднимать его при импорте модуля.
    """
    from sync.agent_e1 import _actual_modifiers

    out: Dict[str, Dict[Any, Any]] = {}
    for campaign_id in sorted({str(c) for c in campaign_ids if str(c).isdigit()}):
        out[campaign_id] = {
            # Ключ словаря — (вид, ключ сегмента), как в действии. Вид у
            # нормализованной записи лежит в "Type" (имя из API), а не в
            # "direct_type": последнего там нет, и запрос по нему собрал бы
            # словарь из строк "None" — сверка молча объявила бы удалёнными
            # все корректировки разом.
            (str(item.get("Type")), str(item.get("key"))): item.get("percent")
            for item in _actual_modifiers(client, campaign_id)
        }
    return out


def check_account(client, actions: List[Dict[str, Any]]) -> Dict[str, Any]:
    latest = drift.latest_per_segment(actions)
    # По объектам, которых кабинет не вернул, состояния нет — это и есть
    # вердикт OBJECT_GONE, а не повод пропустить строку.
    states = fetch_state(client, [a.get("object_id") for a in latest])
    # Корректировки читаются ПОКАМПАНИЙНО и только для тех кампаний, где они
    # действительно сверяются: запрос на кампанию стоит баллов API, и платить
    # их за объекты, по которым агент корректировок не ставил, незачем.
    modifier_campaigns = [a.get("object_id") for a in latest
                          if drift.is_modifier(a.get("direct_type"))]
    for campaign_id, modifiers in fetch_modifiers(client, modifier_campaigns).items():
        if campaign_id in states:
            states[campaign_id]["modifiers"] = modifiers
    rows = [drift.check(action, states.get(str(action.get("object_id") or "")))
            for action in latest]
    mismatched = [r for r in rows if r["verdict"] in drift.ALARMING]
    return {
        "checked": len(rows),
        "verdicts": drift.summarize(rows),
        "alarms": drift.alarms(rows),
        "mismatched": mismatched[:SAMPLE_LIMIT],
        "_rows": rows,
    }


def main() -> int:
    sandbox = "--prod" not in sys.argv
    days = DEFAULT_DAYS
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days = max(1, int(arg.split("=", 1)[1]))

    actions = applied_actions(days)
    by_account: Dict[str, List[Dict[str, Any]]] = {}
    for action in actions:
        by_account.setdefault(str(action.get("account") or ""), []).append(action)

    accounts: List[Dict[str, Any]] = []
    all_rows: List[Dict[str, Any]] = []
    for login, account_actions in sorted(by_account.items()):
        if not login:
            continue
        # dry_run=True всегда: у сверки нет режима записи вовсе, и запрет
        # стоит на клиенте, а не только в намерении вызывающего.
        client = WriteClient(login, sandbox=sandbox, dry_run=True)
        report = check_account(client, account_actions)
        all_rows += report.pop("_rows")
        accounts.append({"account": login, **report,
                         "units_left": client.units_left})

    alarms = drift.alarms(all_rows)
    out = {
        "verdict": "DRIFT" if alarms else "GREEN",
        "sandbox": sandbox,
        "days": days,
        "actions_in_window": len(actions),
        "checked": len(all_rows),
        "verdicts": drift.summarize(all_rows),
        # Покрытие сверки: без этой строки «расхождений нет» одинаково
        # выглядит и когда всё сошлось, и когда не проверено ничего.
        "unverified_kinds": drift.unverified_kinds(all_rows),
        "alarms": alarms,
        "accounts": accounts,
    }
    # Полные строки — в чёрный ящик, а не в лог: разбор расхождения начинается
    # со сравнения двух сверок между собой, и для этого они должны лежать в
    # базе. В логе остаётся то, что читает человек.
    out["blackbox"] = blackbox.save_run(
        blackbox.new_run_id(), stage="drift",
        mode=blackbox.MODE_SANDBOX if sandbox else blackbox.MODE_COMPUTE,
        report={**out, "rows": all_rows})
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    # Расхождение — не сбой прогона, а новость о кабинете. Красным раном оно
    # становится только по явному флагу: у крона это единственный способ
    # прислать письмо, а у ручного запуска повода падать нет.
    if alarms and "--fail-on-alarm" in sys.argv:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
