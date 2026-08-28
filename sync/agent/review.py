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

from sync.agent import autonomy
from sync.agent import rejects

WALL = "repeated_wall"
CONFLICT = "recurring_conflict"
HAND_ROLLBACK = "hand_rollback"
SILENT_STAGE = "silent_stage"
UNVERIFIED = "unverified_kind"
BLIND_WRITE = "blackbox_write_failed"
TACT_HARM = "tact_harmful"
TACT_BLIND = "tact_unmeasured"
SHADOW_READY = "shadow_ready"
SHADOW_IDLE = "shadow_idle"

# Сколько РАЗНЫХ прогонов подряд должно упереться в одну стену, чтобы это
# перестало быть случайностью дня. Три — минимум, при котором совпадение уже
# требует объяснения; меньше даёт шум из обычной работы бюджета.
WALL_MIN_RUNS = 3

# Причины отказа, повторение которых НЕ является находкой: работающие
# ограничители обязаны срабатывать каждый день, и жаловаться на них значит
# жаловаться на замысел.
#
# Коды берутся константами из rejects, а не литералами: литерал не падает от
# опечатки, он молча превращает ограничитель в жалобу — то есть даёт ровно
# тот шум, ради отсутствия которого перечень и заведён.
#
#   budget      — недельный риск-бюджет прогона, общий на все кабинеты;
#   lane_limit  — лимит полосы; после снятия лимита действий полоса отказывает
#                 сотням кандидатов каждый прогон, и это её работа, а не стена;
#   proposal    — предложение не применяется НИКОГДА и ни при какой ступени:
#                 у него нет рычага записи (writer/lanes.LANE_PROPOSAL);
#   run_cap     — снятая рельса «лимит действий на прогон». Строки за
#                 июль–август 2026 ещё попадают в семидневное окно разбора,
#                 и жалоба на ограничитель, которого больше нет, бесполезна
#                 вдвойне (rejects.HISTORICAL_REASONS);
#   shadow      — полоса стоит на ступени 0, и это режим приёмки рычага, а не
#                 сбой: намерение записано в журнал и ждёт сверки с фактом.
#                 Каждый теневой рычаг отказывает по своему объекту каждый
#                 прогон — на третий такт разбор состоял бы из одних стен.
#                 Молчащая тень при этом видна отдельной находкой (shadow_idle).
EXPECTED_REASONS = frozenset({rejects.BUDGET, rejects.LANE_LIMIT,
                              rejects.PROPOSAL, rejects.RUN_CAP,
                              rejects.SHADOW})

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _finding(code: str, severity: str, subject: str, detail: str,
             evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"code": code, "severity": severity, "subject": subject,
            "detail": detail, "evidence": evidence or {}}


def _key(row: Dict[str, Any]) -> str:
    return "/".join([str(row.get("account") or ""), str(row.get("object_id") or ""),
                     str(row.get("kind") or ""), str(row.get("key") or "")])


def walls(reject_rows: Iterable[Dict[str, Any]],
          min_runs: int = WALL_MIN_RUNS) -> List[Dict[str, Any]]:
    """Одно намерение, отказанное по одной причине в min_runs разных прогонах.

    Считаются именно ПРОГОНЫ, а не строки: одно действие, отклонённое трижды
    внутри одного прогона, — это особенность сборки плана, а не история.

    Аргумент назван не rejects, чтобы не заслонять собой одноимённый модуль:
    коды причин теперь приходят оттуда, и тень над импортом означала бы, что
    первое же обращение к rejects.* внутри функции упадёт на списке строк.
    """
    groups: Dict[str, Dict[str, Any]] = {}
    for row in reject_rows or ():
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


def tact_effects(runs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Что показал замер такта целиком (agent_e1_watchdog.tact_effect_report).

    Частные вердикты отвечают на вопрос «выдержало ли ЭТО изменение», и
    четыреста таких ответов не складываются в ответ о работе системы: они
    сняты на объёмах, где каждый по отдельности — шум. Замер такта отвечает
    одним числом, и разбору нужны из него ровно две вещи.

    ВРЕДНЫЙ ТАКТ — находка высшего веса, и не потому, что «стало дороже»
    (дороже бывает от рынка), а потому, что дороже стало ОТНОСИТЕЛЬНО
    заповедника, весь доверительный интервал по одну сторону нуля. Это
    единственное утверждение о вреде, которое агент умеет доказать.

    СЛЕПОЙ ЗАМЕР — находка о самом наблюдении. Такты идут, а сказать о них
    нечего: нет заповедника, он мал, фактов не хватило. Молчание замера
    выглядит ровно как «всё хорошо», и именно поэтому его печатают отдельной
    строкой. Одиночное «unknown» законно (в такте могло не быть действий) —
    находкой становится период, где НИ ОДИН такт не измерен.
    """
    measured: List[Dict[str, Any]] = []
    for report in _reports(runs, "watchdog"):
        for account in report.get("accounts") or []:
            effect = account.get("tact_effect") or {}
            if effect:
                measured.append({"account": account.get("account"), **effect})

    out: List[Dict[str, Any]] = []
    for effect in measured:
        if str(effect.get("verdict")) != "worsened":
            continue
        out.append(_finding(
            TACT_HARM, "high", str(effect.get("account") or ""),
            f"такт {effect.get('tact_date')} ухудшил цену лида на "
            f"{round(float(effect.get('did') or 0.0) * 100)}% относительно "
            f"заповедника",
            {"tact_date": effect.get("tact_date"), "did": effect.get("did"),
             "ci": effect.get("ci"), "treated_delta": effect.get("treated_delta"),
             "holdout_delta": effect.get("holdout_delta")}))

    verdicts = {str(e.get("verdict")) for e in measured}
    if measured and verdicts == {"unknown"}:
        reasons = sorted({str(e.get("reason")) for e in measured if e.get("reason")})
        out.append(_finding(
            TACT_BLIND, "medium", "tact_effect",
            f"за период ни один такт не измерен ({len(measured)} прогонов): "
            + "; ".join(reasons),
            {"runs": len(measured), "reasons": reasons}))
    return out


# Сколько окончательных вердиктов сверки должно накопиться, чтобы разговор о
# выпуске рычага из тени был разговором о числах. То же, что требует лестница
# от первой ступени: приёмка не может быть дешевле входа, который она заменяет
# собой для рычагов без истории.
MIN_SHADOW_VERDICTS = autonomy.STEPS[1].min_closed

# Исходы сверки, которые СЧИТАЮТСЯ материалом. «unknown» — не материал:
# у намерения не было обещания в заявках, или горизонт ещё открыт.
SHADOW_JUDGED = ("shadow_hit", "shadow_miss")


def shadow_intents(runs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Что происходит с рычагами на приёмке.

    Тень — единственное состояние агента, из которого он не выходит сам: 0 → 1
    делает человек, прочитав совпадения намерений с фактом. У такого устройства
    есть ровно два способа сломаться молча, и обе находки здесь про них.

    ПРИЁМКА НАБРАЛА МАТЕРИАЛ — намерений сверено достаточно, а рычаг всё ещё в
    тени. Это не дефект, это ожидающее решение; но решение, о котором никто не
    напомнил, не принимается никогда, и рычаг стоит в тени вечно при готовых
    числах.

    ПРИЁМКА НЕ ДВИЖЕТСЯ — намерения пишутся, а вердиктов нет. Снаружи это
    выглядит как «рычаг проверяется» и может выглядеть так месяцами: обещания
    без сроков, обещания меньше заявки, пустая витрина по объекту. Разница с
    первой находкой в том, что здесь решать человеку НЕЧЕГО, и чинить надо
    сверку, а не ждать.
    """
    judged: Dict[str, int] = {}
    verdict_runs = 0
    for report in _reports(runs, "watchdog"):
        block = report.get("shadow") or {}
        counts = block.get("verdicts") or {}
        if not counts:
            continue
        verdict_runs += 1
        for verdict, count in counts.items():
            judged[str(verdict)] = judged.get(str(verdict), 0) + int(count or 0)

    intents = 0
    for report in _reports(runs, "e1"):
        for account in report.get("accounts") or []:
            intents += int((account.get("shadow") or {}).get("intents") or 0)

    material = sum(judged.get(v, 0) for v in SHADOW_JUDGED)
    out: List[Dict[str, Any]] = []
    if material >= MIN_SHADOW_VERDICTS:
        out.append(_finding(
            SHADOW_READY, "medium", "shadow",
            f"приёмка набрала {material} сверенных намерений "
            f"(порог {MIN_SHADOW_VERDICTS}): "
            f"совпало {judged.get('shadow_hit', 0)}, не совпало "
            f"{judged.get('shadow_miss', 0)} — решение о выпуске за человеком",
            {"judged": material, "verdicts": dict(sorted(judged.items())),
             "threshold": MIN_SHADOW_VERDICTS}))
    elif intents > 0 and material == 0:
        out.append(_finding(
            SHADOW_IDLE, "low", "shadow",
            f"намерений записано {intents}, вердиктов сверки ноль "
            f"({verdict_runs} прогонов сторожа со сверкой) — приёмка не движется",
            {"intents": intents, "verdict_runs": verdict_runs,
             "verdicts": dict(sorted(judged.items()))}))
    return out


def review(runs: List[Dict[str, Any]], reject_rows: List[Dict[str, Any]],
           expected_stages: Iterable[str]) -> Dict[str, Any]:
    """Все находки периода, отсортированные по весу."""
    findings = (silent_stages(runs, expected_stages) + hand_rollbacks(runs)
                + tact_effects(runs) + shadow_intents(runs)
                + walls(reject_rows) + conflicts_seen(runs) + unverified(runs)
                + blind_writes(runs))
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    by_code: Dict[str, int] = {}
    for finding in findings:
        by_code[finding["code"]] = by_code.get(finding["code"], 0) + 1
    return {
        "runs": len(runs),
        "rejects": len(reject_rows),
        "findings": findings,
        "by_code": by_code,
        "by_severity": {level: sum(1 for f in findings if f["severity"] == level)
                        for level in ("high", "medium", "low")},
    }
