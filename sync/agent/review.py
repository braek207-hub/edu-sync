# -*- coding: utf-8 -*-
"""
sync/agent/review.py — недельный разбор беты: находки, а не счётчики.

Чёрный ящик (blackbox.py), журнал отказов (rejects.py), детектор конфликтов
(conflicts.py) и сверка с кабинетом (drift.py) наполняют базу фактами.
Фактов много, и в этом их слабость: человек, открывающий прогон, видит
сегодняшний день и не видит закономерности. А дефект подхода — это именно
закономерность:

  • одно и то же намерение, упирающееся в одну и ту же стену прогон за
    прогоном. Один отказ — случайность дня (бюджет кончился, завтра будет).
    Тридцать одинаковых — неверная модель: агент каждый день тратит расчёт
    на действие, которое система не пропустит никогда;
  • два рычага, спорящих об одном объекте неделю подряд, — не конфликт
    прогона, а несогласованность самих правил;
  • объект, чьи изменения человек возвращает раз за разом, — не дрейф, а
    несогласие с моделью, о котором агент узнаёт последним;
  • стадия, не оставившая ни одного прогона за неделю, — это молчащий крон,
    и молчит он ровно так же, как «всё хорошо».

Модуль чистый: получает уже прочитанные строки и возвращает находки.
Читает базу sync/agent_review.py.

Находка — не текст для человека, а запись с кодом, весом и уликами: по коду
её группируют между разборами, по уликам проверяют, не выдумана ли она.
"""

from typing import Any, Dict, Iterable, List, Optional

WALL = "repeated_wall"
CONFLICT = "recurring_conflict"
HAND_ROLLBACK = "hand_rollback"
SILENT_STAGE = "silent_stage"
UNVERIFIED = "unverified_kind"
BLIND_WRITE = "blackbox_write_failed"

# Сколько РАЗНЫХ прогонов подряд должно упереться в одну стену, чтобы это
# перестало быть случайностью дня. Три — минимум, при котором совпадение уже
# требует объяснения; меньше даёт шум из обычной работы бюджета.
WALL_MIN_RUNS = 3

# Причины отказа, повторение которых НЕ является находкой. Бюджет и лимит
# прогона — это работающие ограничители: они обязаны срабатывать каждый
# день, и жаловаться на них значит жаловаться на замысел.
EXPECTED_REASONS = frozenset({"budget", "run_cap"})

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _finding(code: str, severity: str, subject: str, detail: str,
             evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"code": code, "severity": severity, "subject": subject,
            "detail": detail, "evidence": evidence or {}}


def _key(row: Dict[str, Any]) -> str:
    return "/".join([str(row.get("account") or ""), str(row.get("object_id") or ""),
                     str(row.get("kind") or ""), str(row.get("key") or "")])


def walls(rejects: Iterable[Dict[str, Any]],
          min_runs: int = WALL_MIN_RUNS) -> List[Dict[str, Any]]:
    """Одно намерение, отказанное по одной причине в min_runs разных прогонах.

    Считаются именно ПРОГОНЫ, а не строки: одно действие, отклонённое трижды
    внутри одного прогона, — это особенность сборки плана, а не история.
    """
    groups: Dict[str, Dict[str, Any]] = {}
    for row in rejects or ():
        reason = str(row.get("reason") or "")
        if reason in EXPECTED_REASONS:
            continue
        bucket = groups.setdefault(f"{_key(row)}|{reason}", {
            "runs": set(), "reason": reason, "subject": _key(row),
            "cost_rub": 0.0, "kind": row.get("kind"), "object_id": row.get("object_id"),
        })
        bucket["runs"].add(str(row.get("run_id") or ""))
        bucket["cost_rub"] = max(bucket["cost_rub"], float(row.get("cost_rub") or 0.0))

    out: List[Dict[str, Any]] = []
    for bucket in groups.values():
        runs = len(bucket["runs"])
        if runs < min_runs:
            continue
        out.append(_finding(
            WALL,
            # Дорогой объект впереди дешёвого: стена перед кампанией с
            # расходом в сотни рублей в день стоит внимания раньше, чем перед
            # спящей. Порог — тысяча рублей в день, ниже него это заметка.
            "high" if bucket["cost_rub"] >= 1000 else "medium",
            bucket["subject"],
            f"{runs} прогонов подряд отказ по причине «{bucket['reason']}»",
            {"runs": runs, "reason": bucket["reason"],
             "cost_rub": round(bucket["cost_rub"], 2),
             "object_id": bucket["object_id"], "kind": bucket["kind"]}))
    return out


def _reports(runs: Iterable[Dict[str, Any]], stage: str) -> List[Dict[str, Any]]:
    return [r.get("report") or {} for r in runs or ()
            if str(r.get("stage") or "") == stage]


def conflicts_seen(runs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Конфликты плана, повторяющиеся из прогона в прогон.

    Разовый конфликт — работа детектора: два рычага не сошлись, оба сняты,
    завтра сойдутся. Тот же конфликт неделю подряд означает, что не сойдутся
    никогда: спорят не прогоны, а правила.
    """
    by_reason: Dict[str, int] = {}
    runs_with: Dict[str, int] = {}
    for report in _reports(runs, "e1"):
        for account in report.get("accounts") or []:
            found = account.get("conflicts") or {}
            for reason, count in found.items():
                by_reason[reason] = by_reason.get(reason, 0) + int(count or 0)
                runs_with[reason] = runs_with.get(reason, 0) + 1
    out: List[Dict[str, Any]] = []
    for reason, total in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        if runs_with.get(reason, 0) < 2:
            continue
        out.append(_finding(
            CONFLICT, "medium", reason,
            f"конфликт «{reason}» снимал действия в {runs_with[reason]} прогонах "
            f"(всего {total})",
            {"runs": runs_with[reason], "actions": total}))
    return out


def hand_rollbacks(runs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Объекты, чьи изменения возвращают руками.

    Самая дорогая находка разбора: агент об этом не узнаёт никогда сам, а
    наблюдение всё это время судит эффект изменения, которого нет.
    """
    hits: Dict[str, Dict[str, Any]] = {}
    for report in _reports(runs, "drift"):
        for row in report.get("rows") or []:
            verdict = str(row.get("verdict") or "")
            if verdict not in ("reverted", "segment_gone", "drifted"):
                continue
            subject = _key({"account": row.get("account"),
                            "object_id": row.get("object_id"),
                            "kind": row.get("direct_type"), "key": row.get("key")})
            bucket = hits.setdefault(subject, {"verdicts": {}, "runs": 0})
            bucket["verdicts"][verdict] = bucket["verdicts"].get(verdict, 0) + 1
            bucket["runs"] += 1
    out: List[Dict[str, Any]] = []
    for subject, bucket in sorted(hits.items(), key=lambda kv: -kv[1]["runs"]):
        out.append(_finding(
            HAND_ROLLBACK, "high", subject,
            "изменение не стоит в кабинете: " +
            ", ".join(f"{v}×{n}" for v, n in sorted(bucket["verdicts"].items())),
            {"runs": bucket["runs"], "verdicts": bucket["verdicts"]}))
    return out


def unverified(runs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Виды действий, которые сверка не умеет проверять.

    Слепое пятно измеряется, а не подразумевается: покрытие сверки — это
    единственное, что отличает «расхождений нет» от «ничего не проверено».
    """
    kinds: Dict[str, int] = {}
    for report in _reports(runs, "drift"):
        for kind, count in (report.get("unverified_kinds") or {}).items():
            kinds[kind] = kinds.get(kind, 0) + int(count or 0)
    return [_finding(UNVERIFIED, "low", kind,
                     f"сверка не умеет проверять этот вид ({count} действий за период)",
                     {"actions": count})
            for kind, count in sorted(kinds.items(), key=lambda kv: -kv[1])]


def silent_stages(runs: Iterable[Dict[str, Any]],
                  expected: Iterable[str]) -> List[Dict[str, Any]]:
    """Стадии, не оставившие за период ни одного прогона.

    Молчащий крон выглядит ровно как «всё хорошо»: ни ошибки, ни строки. Это
    единственная находка, которую нельзя вывести из данных — только из их
    отсутствия, поэтому список ожидаемых стадий задаётся явно.
    """
    seen = {str(r.get("stage") or "") for r in runs or ()}
    return [_finding(SILENT_STAGE, "high", stage,
                     "за период не было ни одного прогона этой стадии", {})
            for stage in expected if stage not in seen]


def blind_writes(runs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Прогоны, у которых не записалась часть чёрного ящика.

    Сбой записи возвращается полем отчёта, а не исключением (blackbox.py), —
    значит он попадает в базу только тогда, когда следующая попытка удалась.
    Уцелевшая отметка об ошибке означает, что где-то рядом лежит прогон,
    которого в базе нет вовсе.
    """
    out: List[Dict[str, Any]] = []
    for run in runs or ():
        error = ((run.get("report") or {}).get("blackbox") or {}).get("error")
        if not error:
            continue
        out.append(_finding(BLIND_WRITE, "medium", str(run.get("stage") or ""),
                            f"запись чёрного ящика не удалась: {error}",
                            {"run_id": run.get("run_id"),
                             "run_url": run.get("run_url")}))
    return out


def review(runs: List[Dict[str, Any]], rejects: List[Dict[str, Any]],
           expected_stages: Iterable[str]) -> Dict[str, Any]:
    """Все находки периода, отсортированные по весу."""
    findings = (silent_stages(runs, expected_stages) + hand_rollbacks(runs)
                + walls(rejects) + conflicts_seen(runs) + unverified(runs)
                + blind_writes(runs))
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    by_code: Dict[str, int] = {}
    for finding in findings:
        by_code[finding["code"]] = by_code.get(finding["code"], 0) + 1
    return {
        "runs": len(runs),
        "rejects": len(rejects),
        "findings": findings,
        "by_code": by_code,
        "by_severity": {level: sum(1 for f in findings if f["severity"] == level)
                        for level in ("high", "medium", "low")},
    }
