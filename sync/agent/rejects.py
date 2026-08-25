# -*- coding: utf-8 -*-
"""
sync/agent/rejects.py — отказы прогона: чего агент хотел и не смог.

Журнал edu_agent_actions знает только применённое. Всё остальное — не влезло
в бюджет, срезано лимитом действий, попало в кулдаун, осталось без красной
линии, исчерпало попытки — жило счётчиками в отчёте прогона и исчезало
вместе с логом.

Между тем именно здесь видны дефекты подхода, а не случайности дня. Один
отказ ничего не значит: бюджет кончился, завтра будет. Один и тот же объект,
упирающийся в одну и ту же стену тридцать прогонов подряд, — это уже не
бюджет, это неверная модель: агент каждый день тратит расчёт на действие,
которое система не пропустит никогда. Такое различимо только на истории,
поэтому отказы кладутся строками, а не счётчиками.

Модуль чистый: он ничего не пишет и не читает, только превращает списки
действий прогона в строки для sync/agent/blackbox.py. Причина здесь —
машинный код, а не текст для человека: по нему группируют.
"""

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sync.agent import conflicts

# Причины. Список закрытый: отказ без известной причины — это дефект
# врезки, а не новое состояние, и он обязан выглядеть иначе, чем известные.
BUDGET = "budget"                     # не влез в остаток риска (недели или дня)
RUN_CAP = "run_cap"                   # исчерпан лимит действий на прогон
NO_RED_LINE = "no_red_line"           # не с чем сравнивать исход — не применяем
COOLDOWN = "cooldown"                 # свежий откат по этому сегменту
ATTEMPTS_EXHAUSTED = "attempts_exhausted"   # исчерпаны попытки отправки
HOLDOUT = "holdout"                   # объект в заповеднике — агент его не трогает
LEARNING_COOLDOWN = "learning_cooldown"     # обучение стратегии ещё не остыло
CLOSED_KEY = "closed_key"             # ключ уже закрыт финальным статусом
NO_GROWTH_ADDRESS = "no_growth_address"     # сокращение без адресата роста
UNKNOWN = "unknown"

# Причины конфликтов берутся из sync/agent/conflicts.py, а не переписываются
# сюда строками: один код причины в двух местах рано или поздно разъедется,
# и группировка беты молча потеряет половину отказов.
CONFLICT_REASONS = frozenset({conflicts.SUSPENDED_OBJECT,
                              conflicts.OPPOSING_LEVERS,
                              conflicts.DUPLICATE_SEGMENT})

KNOWN_REASONS = frozenset({BUDGET, RUN_CAP, NO_RED_LINE, COOLDOWN,
                           ATTEMPTS_EXHAUSTED, HOLDOUT, LEARNING_COOLDOWN,
                           CLOSED_KEY, NO_GROWTH_ADDRESS,
                           UNKNOWN}) | CONFLICT_REASONS

# Сколько знаков ключа сегмента едет в строку. Ключ бывает длинным (список
# минус-фраз), а группировка беты идёт по объекту и причине, не по ключу.
KEY_LIMIT = 300


def _text(value: Any, limit: int = 200) -> str:
    return str(value if value is not None else "")[:limit]


def row(action: Dict[str, Any], reason: str, account: str = "",
        stage: str = "", risk_rub: float = 0.0) -> Dict[str, Any]:
    """Одно действие + причина отказа → строка журнала отказов.

    Неизвестная причина не отбрасывается и не подменяется похожей: она
    записывается как UNKNOWN, а исходное значение уезжает в detail. Отказ,
    потерянный из-за опечатки в коде причины, — ровно та слепота, ради
    устранения которой журнал и заводится.
    """
    known = reason if reason in KNOWN_REASONS else UNKNOWN
    detail: Dict[str, Any] = {}
    if known == UNKNOWN and reason:
        detail["reason_given"] = _text(reason)
    for field in ("action_kind", "exposure", "baseline_daily_rub", "risk_basis",
                  "learning_impact"):
        if action.get(field) is not None:
            detail[field] = action[field]
    return {
        "stage": stage,
        "account": _text(account, 120),
        "object_id": _text(action.get("object_id"), 60),
        "kind": _text(action.get("direct_type"), 60),
        "key": _text(action.get("key"), KEY_LIMIT),
        "reason": known,
        # Расход объекта в день — то, что стоит на кону за этим отказом.
        # Ноль означает «неизвестно», и это видно как ноль, а не как «дёшево».
        "cost_rub": float(action.get("baseline_daily_rub") or 0.0),
        "risk_rub": float(risk_rub or 0.0),
        "detail": detail,
    }


def from_groups(groups: Sequence[Tuple[str, Iterable[Dict[str, Any]]]],
                account: str = "", stage: str = "",
                risks: Optional[Dict[str, float]] = None) -> List[Dict[str, Any]]:
    """Пары (причина, действия) → строки журнала отказов.

    Форма «пары» выбрана ради врезки: вызывающий прогон перечисляет свои
    списки одним выражением, и добавление нового вида отказа — одна строка
    в этом перечислении, а не новая ветка разбора здесь.

    risks — цена действия, посчитанная прогоном (writer/risk.py). Отдельным
    словарём, потому что у неприменённого действия своего risk_rub нет: он
    проставляется только тем, что прошло в бюджет.
    """
    prices = risks or {}
    out: List[Dict[str, Any]] = []
    for reason, actions in groups:
        for action in actions or ():
            if not isinstance(action, dict):
                continue
            price = prices.get(str(action.get("idempotency_key") or ""), 0.0)
            out.append(row(action, reason, account=account, stage=stage,
                           risk_rub=price))
    return out


def by_reason(rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Счётчик отказов по причинам — для отчёта прогона.

    Строки уезжают в базу, но прогон обязан оставаться читаемым без базы:
    человек, открывший лог, должен видеть тот же расклад, что и запрос.
    """
    out: Dict[str, int] = {}
    for item in rows or ():
        reason = str(item.get("reason") or UNKNOWN)
        out[reason] = out.get(reason, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
