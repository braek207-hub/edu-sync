# -*- coding: utf-8 -*-
"""
sync/agent_e1_watchdog.py — сторож красных линий: наблюдение и автооткат.

Третий слой защиты движка записи. Первые два (рельсы guardrails.py и
риск-бюджет risk.py) не пускают заведомо недопустимое; этот слой отвечает за
то, чего заранее не знает никто, — за результат. Весь дизайн автономного
агента построен на замене человеческого апрува автооткатом: не «человек
предотвращает ошибку заранее», а «система исправляет её за час». До появления
этого прогона механика отката (rollback.py) существовала только для тестов и
не вызывалась из рабочего кода ни разу — то есть третьего слоя в системе не
было, хотя всё вокруг написано так, будто он есть.

Цикл прогона:
    открытые действия журнала → окно наблюдения каждого → факты за окно →
    пробита ли красная линия → рельсы для запроса на возврат → откат → отчёт

Окно наблюдения — не «всё время с момента применения»:
  * день применения не считается (OBSERVATION_LAG_DAYS): изменение работало
    неполные сутки, и этот день смешивает «до» и «после»;
  * верхняя граница — вчера (OBSERVATION_TAIL_DAYS): факты за сегодня неполны,
    синк приносит завершённый день;
  * длина ограничена горизонтом (OBSERVATION_HORIZON_DAYS): красная линия
    судит эффект изменения, а не общий дрейф кампании за квартал. Без
    горизонта наблюдение месячной давности сравнивало бы базовый CPA с
    накопленным средним, где влияния самого изменения почти не осталось.

По умолчанию ПЕСОЧНИЦА и DRY-RUN. Откат — это тоже запись в кабинет, поэтому
он требует тех же двух явных флагов, что и прямое применение.

Запуск:
    python -m sync.agent_e1_watchdog                 # песочница, репетиция
    python -m sync.agent_e1_watchdog --prod          # боевой кабинет, репетиция
    python -m sync.agent_e1_watchdog --prod --apply  # боевой откат
ENV: DATABASE_URL, DIRECT_TOKEN
"""

import json
import sys
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sync.agent import db as agent_db
from sync.agent.writer import db as writer_db
from sync.agent.writer.apply import _element_errors
from sync.agent.writer.client import WriteClient
from sync.agent.writer.guardrails import check_action
from sync.agent.writer.rollback import rollback_payload, is_breached
from sync.agent.writer.units import api_to_delta

OBSERVATION_LAG_DAYS = 1        # день применения в наблюдение не входит
OBSERVATION_TAIL_DAYS = 1       # верхняя граница наблюдения — вчера
OBSERVATION_HORIZON_DAYS = 14   # длина окна наблюдения, дней

PREVIEW_SAMPLE_LIMIT = 5

# Причины, по которым откат невозможен. Повтор их не исправит: Id корректировки
# не появится сам, рельсы отклонят тот же запрос так же. Такие действия
# помечаются в журнале неоткатываемыми (writer_db.mark_rollback_failed с
# permanent=True) и снимаются с наблюдения — но не исчезают: изменение
# остаётся живым, риск-бюджет продолжает его оплачивать, счётчик
# failed_rollbacks_count показывает его в каждом отчёте.
NO_ID_REASON = (
    "неизвестен Id корректировки: откат вслепую невозможен "
    "(Id приходит только в ответе bidmodifier.add)"
)
INCOMPLETE_REASON = "тело запроса на возврат неполное: нет Id или коэффициента"
HOLDOUT_REASON = (
    "кампания в заповеднике: сторож её не трогает — заповедник и есть база "
    "сравнения для всех замеров"
)
NO_RED_LINE_REASON = (
    "у действия нет красной линии — судить не по чему, нужна ручная сверка"
)

# Состояния наблюдения. Разделены, потому что за ними стоят разные решения:
#   waiting    — окно ещё не открылось, данных быть не может;
#   collecting — окно открыто, наблюдений меньше минимума: молчим намеренно,
#                иначе шум примут за провал и откатят здоровое изменение;
#   expired    — горизонт закрыт, а минимум наблюдений так и не набран:
#                вердикта не будет никогда, изменение живёт непроверенным;
#   watched    — наблюдений достаточно, линия не пробита;
#   breached   — линия пробита, действие откатывается.
STATE_WAITING = "waiting"
STATE_COLLECTING = "collecting"
STATE_EXPIRED = "expired"
STATE_WATCHED = "watched"
STATE_BREACHED = "breached"
STATE_NO_RED_LINE = "no_red_line"

# Сервис API → префикс вида действия для рельс. Запрос на возврат обязан
# пройти те же рельсы, что обычное действие: иначе путь отката — единственный
# путь в кабинет, не проверенный ничем. Вид собирается из СЕРВИСА И МЕТОДА
# фактического запроса, а не берётся из действия: если rollback_payload
# когда-нибудь вернёт delete, allow-лист обязан это увидеть и отклонить.
_SERVICE_KIND_PREFIX = {"bidmodifiers": "bidmodifier"}


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value[:19].replace("Z", "")).date()
    return None


def observation_window(
    action: Dict[str, Any], today: date
) -> Optional[Tuple[date, date, bool]]:
    """Окно наблюдения действия: (первый день, последний день, закрыто ли).

    Отсчёт — от МОМЕНТА ПРИМЕНЕНИЯ этого действия, а не от общего окна прогона:
    красная линия ставилась вместе с действием, и судить о ней надо по тому,
    что случилось после него. Общее окно на всех смешало бы в наблюдение дни,
    когда изменения ещё не было.

    Для зависшей строки ('stale') applied_at равен моменту отправки запроса
    (writer/db.py::MARK_STALE_SQL), но подстраховка на created_at оставлена:
    строка без обеих отметок времени не наблюдаема вообще, и это не повод
    считать её здоровой.

    None — окно ещё не открылось: ни одного полного дня после применения не
    прошло. «Закрыто» значит, что горизонт исчерпан и новых данных в окно уже
    не добавится.
    """
    applied = _as_date(action.get("applied_at")) or _as_date(action.get("created_at"))
    if applied is None:
        return None
    start = applied + timedelta(days=OBSERVATION_LAG_DAYS)
    horizon_end = start + timedelta(days=OBSERVATION_HORIZON_DAYS - 1)
    end = min(horizon_end, today - timedelta(days=OBSERVATION_TAIL_DAYS))
    if start > end:
        return None
    return start, end, end >= horizon_end


def observed_metrics(rows: Iterable[Dict[str, Any]], window: Tuple[date, date, bool]) -> Dict[str, Any]:
    """Наблюдаемые метрики объекта за окно: расход, эффективные лиды, CPA.

    Знаменатель — eff_leads, тот же, что у базового CPA (agent/db.py::
    load_baseline_cpa): красная линия сравнивает наблюдаемое с базовым, и
    разные знаменатели сделали бы сравнение бессмысленным.

    leads=0 → cpa=0: is_breached всё равно не выносит вердикт до минимума
    наблюдений, а деление на ноль дало бы бесконечность, которая пробила бы
    любой порог на первом же дне.
    """
    start, end, _ = window
    cost = 0.0
    leads = 0
    days = 0
    for row in rows:
        fact_date = _as_date(row.get("fact_date"))
        if fact_date is None or fact_date < start or fact_date > end:
            continue
        cost += float(row.get("cost") or 0.0)
        leads += int(row.get("eff_leads") or 0)
        days += 1
    return {
        "cost": round(cost, 2),
        "leads": leads,
        "cpa": round(cost / leads, 2) if leads > 0 else 0.0,
        "days": days,
    }


def judge(action: Dict[str, Any], facts_by_campaign: Dict[str, List[Dict[str, Any]]],
          today: date) -> Dict[str, Any]:
    """Вердикт по одному действию: состояние наблюдения и наблюдаемые метрики."""
    red_line = action.get("red_line") or {}
    if not red_line:
        return {"state": STATE_NO_RED_LINE, "reason": NO_RED_LINE_REASON}

    window = observation_window(action, today)
    if window is None:
        return {"state": STATE_WAITING, "reason": "окно наблюдения ещё не открылось"}

    rows = facts_by_campaign.get(str(action.get("object_id"))) or []
    observed = observed_metrics(rows, window)
    breached, reason = is_breached(red_line, observed)
    start, end, closed = window
    verdict = {
        "observed": observed,
        "window": {"from": start.isoformat(), "to": end.isoformat(), "closed": closed},
    }
    if breached:
        return {**verdict, "state": STATE_BREACHED, "reason": reason}

    min_leads = int(red_line.get("min_leads") or 0)
    if observed["leads"] < min_leads:
        # Горизонт закрыт, а минимума так и нет — вердикта не будет никогда.
        # Это не «всё хорошо»: изменение живёт непроверенным, и в отчёте оно
        # обязано стоять отдельной строкой, а не растворяться в «под наблюдением».
        state = STATE_EXPIRED if closed else STATE_COLLECTING
        return {**verdict, "state": state,
                "reason": f"наблюдений {observed['leads']} из {min_leads}"}
    return {**verdict, "state": STATE_WATCHED, "reason": reason or ""}


def guard_form(action: Dict[str, Any], service: str, method: str,
               params: Dict[str, Any]) -> Dict[str, Any]:
    """Запрос на возврат в той форме, которую понимают рельсы (guardrails).

    Коэффициент переводится обратно в дельту (api_to_delta): рельса
    MODIFIER_CAP откалибрована по дельтам, и 100-базный коэффициент из тела
    запроса она прочитала бы как «+100 %» — нейтраль отката (100) отклонялась
    бы как выход за потолок, а настоящий выход за потолок (например 170)
    проходил бы под видом «+70» только по случайности совпадения шкал.
    """
    prefix = _SERVICE_KIND_PREFIX.get(str(service), str(service))
    item = ((params.get("BidModifiers") or [{}])[0]) or {}
    percent = item.get("BidModifier")
    return {
        "action_kind": f"{prefix}.{method}",
        "object_level": action.get("object_level"),
        "object_id": action.get("object_id"),
        "payload": {
            "Id": item.get("Id"),
            "BidModifier": None if percent is None else api_to_delta(percent),
        },
    }


def _fail(db_module, action: Dict[str, Any], reason: str, permanent: bool,
          write_allowed: bool) -> Dict[str, Any]:
    """Неудачный откат: пометка в журнале и строка для отчёта.

    В репетиции журнал не трогается — отчёт всё равно показывает, что откат
    неисполним, но состояние базы репетиция не меняет.
    """
    marked = None
    if write_allowed:
        marked = db_module.mark_rollback_failed(action["action_id"], reason,
                                                permanent=permanent)
    return {
        "result": "rollback_failed",
        "action_id": action.get("action_id"),
        "object_id": action.get("object_id"),
        "reason": reason,
        "permanent": bool(permanent),
        "attempts": (marked or {}).get("rollback_attempts"),
        "retries_stopped": bool((marked or {}).get("rollback_failed_at")),
    }


def rollback_one(client, action: Dict[str, Any], db_module,
                 holdout_ids: set) -> Dict[str, Any]:
    """Возврат одного объекта в прошлое состояние.

    Ничего не удаляет ни при каких обстоятельствах: rollback_payload строит
    только set нейтрального или прежнего коэффициента, а allow-лист рельс
    (check_action) не пропустил бы delete, даже если бы он там появился.
    """
    write_allowed = client.is_write_allowed()

    if str(action.get("object_id")) in holdout_ids:
        # Действия в заповедник не попадают (agent_e1 отсекает их check_holdout
        # до применения), но кампанию могли внести в заповедник ПОСЛЕ
        # применения — тогда в журнале живёт применённое действие по её
        # объекту. Трогать её нельзя: заповедник неприкосновенен. Пометки
        # неоткатываемости здесь нет намеренно — состав заповедника меняется,
        # и на следующем прогоне откат может стать возможным.
        return {"result": "blocked_holdout", "action_id": action.get("action_id"),
                "object_id": action.get("object_id"), "reason": HOLDOUT_REASON}

    try:
        request = rollback_payload(action)
    except Exception as exc:
        # delta_to_api роняет вызов на коэффициенте вне диапазона Директа:
        # прошлое состояние испорчено, и повтор его не починит.
        return _fail(db_module, action, f"запрос на возврат не строится: {exc}"[:300],
                     True, write_allowed)

    if request is None:
        return _fail(db_module, action, NO_ID_REASON, True, write_allowed)

    service, method, params = request
    item = ((params.get("BidModifiers") or [{}])[0]) or {}
    if item.get("Id") is None or item.get("BidModifier") is None:
        return _fail(db_module, action, INCOMPLETE_REASON, True, write_allowed)

    ok, reason = check_action(guard_form(action, service, method, params))
    if not ok:
        return _fail(db_module, action, f"рельсы отклонили запрос на возврат: {reason}",
                     True, write_allowed)

    if not write_allowed:
        return {"result": "dry_run", "action_id": action.get("action_id"),
                "object_id": action.get("object_id"),
                "request": {"service": service, "method": method, "params": params}}

    try:
        response = client.mutate(service, method, params)
        errors = _element_errors(method, response)
    except Exception as exc:
        # Сбой отправки бывает разовым — попытка засчитывается, но строка не
        # снимается с наблюдения сразу: её снимет счётчик попыток.
        return _fail(db_module, action, f"{type(exc).__name__}: {exc}"[:300],
                     False, write_allowed)

    if errors:
        return _fail(db_module, action, f"API отклонил возврат: {json.dumps(errors, ensure_ascii=False)}"[:300],
                     False, write_allowed)

    db_module.mark_rolled_back(action["action_id"])
    return {"result": "rolled_back", "action_id": action.get("action_id"),
            "object_id": action.get("object_id"), "response": response}


def watch(client, actions: List[Dict[str, Any]], db_module, holdout_ids: set,
          facts_by_campaign: Dict[str, List[Dict[str, Any]]], today: date) -> Dict[str, Any]:
    """Наблюдение и откат по всем открытым действиям одного кабинета."""
    states: Dict[str, int] = {}
    breached: List[Dict[str, Any]] = []
    rolled_back = 0
    failures: List[Dict[str, Any]] = []
    blocked_holdout: List[Dict[str, Any]] = []
    would_roll_back: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for action in actions:
        # Одна испорченная строка журнала не должна ослеплять сторожа по всем
        # остальным: непойманное исключение здесь означало бы, что ни одно
        # пробившее линию изменение в этом прогоне не откатится. Строка с
        # ошибкой видна в отчёте и разбирается руками — но не молча, и не
        # ценой всех остальных.
        try:
            verdict = judge(action, facts_by_campaign, today)
        except Exception as exc:
            errors.append({"action_id": action.get("action_id"),
                           "object_id": action.get("object_id"),
                           "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        state = verdict["state"]
        states[state] = states.get(state, 0) + 1
        if state != STATE_BREACHED:
            continue

        breached.append({
            "action_id": action.get("action_id"),
            "object_id": action.get("object_id"),
            "action_kind": action.get("action_kind"),
            "reason": verdict.get("reason"),
            "observed": verdict.get("observed"),
        })
        try:
            outcome = rollback_one(client, action, db_module, holdout_ids)
        except Exception as exc:
            errors.append({"action_id": action.get("action_id"),
                           "object_id": action.get("object_id"),
                           "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        if outcome["result"] == "rolled_back":
            rolled_back += 1
        elif outcome["result"] == "rollback_failed":
            failures.append(outcome)
        elif outcome["result"] == "blocked_holdout":
            blocked_holdout.append(outcome)
        else:
            would_roll_back.append(outcome)

    return {
        "under_watch": len(actions),
        "states": dict(sorted(states.items())),
        "breached": len(breached),
        "breached_sample": breached[:PREVIEW_SAMPLE_LIMIT],
        "rolled_back": rolled_back,
        "would_roll_back": len(would_roll_back),
        "rollback_failed": len(failures),
        "failures": [{k: v for k, v in f.items() if k != "result"}
                     for f in failures[:PREVIEW_SAMPLE_LIMIT]],
        "blocked_holdout": len(blocked_holdout),
        # Строки, которые прогон не смог даже рассудить: испорченные данные
        # журнала. Не «ноль пробитых», а явный отдельный счётчик.
        "errors": len(errors),
        "errors_sample": errors[:PREVIEW_SAMPLE_LIMIT],
    }


def facts_window(actions: List[Dict[str, Any]], today: date) -> Optional[Tuple[date, date]]:
    """Общий отрезок дат, покрывающий окна наблюдения всех действий.

    Один запрос к фактам на прогон вместо запроса на действие; окно каждого
    действия вырезается из этих строк по его собственным границам.
    """
    windows = [observation_window(a, today) for a in actions]
    windows = [w for w in windows if w]
    if not windows:
        return None
    return min(w[0] for w in windows), max(w[1] for w in windows)


def load_facts(actions: List[Dict[str, Any]], today: date) -> Dict[str, List[Dict[str, Any]]]:
    span = facts_window(actions, today)
    if span is None:
        return {}
    rows = agent_db.load_daily_facts(
        [str(a.get("object_id")) for a in actions], span[0].isoformat(), span[1].isoformat())
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row["campaign_id"]), []).append(row)
    return out


def main() -> int:
    sandbox = "--prod" not in sys.argv
    dry_run = "--apply" not in sys.argv
    today = date.today()

    writer_db.ensure_writer_tables()
    actions = writer_db.open_actions()
    holdout_ids = {str(h) for h in agent_db.load_holdout_ids()}
    facts_by_campaign = load_facts(actions, today)

    by_account: Dict[str, List[Dict[str, Any]]] = {}
    for action in actions:
        by_account.setdefault(str(action.get("account") or ""), []).append(action)

    accounts: List[Dict[str, Any]] = []
    for login, account_actions in sorted(by_account.items()):
        client = WriteClient(login, sandbox=sandbox, dry_run=dry_run)
        report = watch(client, account_actions, writer_db, holdout_ids,
                       facts_by_campaign, today)
        accounts.append({"account": login, **report, "units_left": client.units_left})

    print(json.dumps({
        "sandbox": sandbox,
        "dry_run": dry_run,
        "today": today.isoformat(),
        "observation": {
            "lag_days": OBSERVATION_LAG_DAYS,
            "horizon_days": OBSERVATION_HORIZON_DAYS,
            "tail_days": OBSERVATION_TAIL_DAYS,
        },
        "under_watch": len(actions),
        "accounts": accounts,
        # Изменения, живые в кабинете и неоткатываемые автоматически: их
        # разбирает человек. Число накопительное, а не за прогон, — иначе
        # находка прошлого прогона исчезала бы из виду.
        "needs_manual_rollback": writer_db.failed_rollbacks_count(),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
