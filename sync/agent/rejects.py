# -*- coding: utf-8 -*-
"""
sync/agent/rejects.py — отказы прогона: чего агент хотел и не смог.

Журнал edu_agent_actions знает только применённое. Всё остальное — не влезло
в бюджет, не прошло лимит своей полосы, попало в кулдаун, осталось без красной
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

# Причины. Разбор перечней — ниже, у KNOWN_REASONS.
BUDGET = "budget"                     # не влез в остаток риска (недели или дня)
LANE_LIMIT = "lane_limit"             # не прошёл лимит своей полосы
PROPOSAL = "proposal"                 # рычага записи нет — это рекомендация человеку
# Снятая рельса: лимит действий на прогон (guardrails.cap_actions) удалён
# вместе с причиной — отбор идёт полосами. Константа остаётся, потому что
# строки с этим кодом лежат в edu_agent_rejects за июль–август 2026 и их
# читают недельный разбор (review.EXPECTED_REASONS) и подписи панели. См.
# HISTORICAL_REASONS ниже: писать нельзя, читать обязательно.
RUN_CAP = "run_cap"
NO_RED_LINE = "no_red_line"           # не с чем сравнивать исход — не применяем
COOLDOWN = "cooldown"                 # свежий откат по этому сегменту
ATTEMPTS_EXHAUSTED = "attempts_exhausted"   # исчерпаны попытки отправки
HOLDOUT = "holdout"                   # объект в заповеднике — агент его не трогает
LEARNING_COOLDOWN = "learning_cooldown"     # обучение стратегии ещё не остыло
CLOSED_KEY = "closed_key"             # ключ уже закрыт финальным статусом
NO_GROWTH_ADDRESS = "no_growth_address"     # сокращение без адресата роста
# Баллы кабинета кончились: остаток ниже резерва (writer/apply.UNITS_RESERVE_SHARE),
# отправлять нечем. Причина кабинетная, а не действия: то же действие завтра
# уедет без единой правки — но пока баллов нет, хвост такта не применяется, и
# он обязан быть виден строками. Раньше он жил счётчиком units_low в отчёте
# прогона и исчезал вместе с логом; при лимите в 50 действий порог не срабатывал
# ни разу, при трёхстах срабатывает первым же тактом.
UNITS_LOW = "units_low"
# Идея реестра уже применена, идёт замер: горизонт её проверки не вышел
# (registry.STATUS_RUNNING). Своя причина, а не одна из соседних, потому что
# лечится она третьим способом: closed_key говорит «ключ закрыт финальным
# статусом» и снимается следующим окном, proposal — «рычага записи нет вовсе»
# и не снимается ничем, а здесь рычаг есть и УЖЕ уехал в кабинет. Слей их — и
# на разборе «идея честно проверяется» выглядело бы как стена, в которую
# агент бьётся каждый прогон.
IDEA_RUNNING = "idea_running"
UNKNOWN = "unknown"

# Причины конфликтов берутся из sync/agent/conflicts.py, а не переписываются
# сюда строками: один код причины в двух местах рано или поздно разъедется,
# и группировка беты молча потеряет половину отказов.
CONFLICT_REASONS = frozenset({conflicts.SUSPENDED_OBJECT,
                              conflicts.OPPOSING_LEVERS,
                              conflicts.DUPLICATE_SEGMENT})

# Перечень РАЗДВОЕН, потому что у него два потребителя с разными интересами.
#
# KNOWN_REASONS — что прогон имеет право записать сегодня. Список закрытый:
# отказ по причине не отсюда — дефект врезки, а не новое состояние, и он обязан
# выглядеть иначе, чем известные (row() схлопывает такое в UNKNOWN).
#
# HISTORICAL_REASONS — коды снятых рельс. Новых строк с ними не появляется, но
# старые лежат в edu_agent_rejects и читаются наравне с остальными. Соблазн
# «убрать лишнее из перечня» здесь стоит дорого: удали код — и полугодовая
# история либо станет безымянной, либо расползётся голыми литералами по
# читающим модулям, где разъедется от первой опечатки.
#
# READABLE_REASONS — то, что разбор беты и подписи панели обязаны понимать.
KNOWN_REASONS = frozenset({BUDGET, LANE_LIMIT, PROPOSAL, NO_RED_LINE,
                           COOLDOWN, ATTEMPTS_EXHAUSTED, HOLDOUT,
                           LEARNING_COOLDOWN, CLOSED_KEY, NO_GROWTH_ADDRESS,
                           UNITS_LOW, IDEA_RUNNING, UNKNOWN}) | CONFLICT_REASONS

HISTORICAL_REASONS = frozenset({RUN_CAP})

READABLE_REASONS = KNOWN_REASONS | HISTORICAL_REASONS

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
