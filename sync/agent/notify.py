# -*- coding: utf-8 -*-
"""Уведомления человеку о боевых тактах.

Сегодня единственный сигнал о прогоне — красный шаг воркфлоу; всё остальное
живёт в чёрном ящике и требует, чтобы человек сам туда зашёл. Молчание —
тоже сигнал, поэтому шлём сообщение и когда «применено 0»: иначе тишина
неотличима от того, что крон не отработал вовсе.

Транспорт — Bot API через urllib (без внешних зависимостей). Отказ сети не
роняет прогон: send() ничего не бросает, а результат возвращается полем и
уходит рядом с чёрным ящиком в тот же отчёт.

Текст — ЧИСТЫЙ plain text, без parse_mode. Имена кампаний в отчётах несут
"_" и "*" как обычные символы; под Markdown-разметкой Telegram они рвут
сообщение или меняют его смысл, а экранировать их дороже, чем не включать
разметку вовсе.
"""
import json
import os
import urllib.request
from typing import Any, Dict

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
# Лимит Bot API на текст сообщения — 4096 символов; отчёт обрезается, а не
# отбрасывается: усечённая сводка лучше отсутствующей.
MAX_TEXT_LEN = 4000


def _post(url: str, data: bytes) -> None:
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def send(text: str, buttons: Any = None) -> Dict[str, Any]:
    """Шлёт text в чат. Никогда не бросает — результат всегда в поле.

    buttons — готовый inline_keyboard (список рядов кнопок) либо None. Кнопки
    имеют смысл только когда ответы человека принимает вебхук Panda-BI: нажатие
    приходит туда как callback_query. При работе через getUpdates они не мешают
    (нажатие просто не будет прочитано), но и не помогают — поэтому решение
    добавить их принимает вызывающий, а не транспорт.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return {"sent": False, "reason": "not_configured"}
    try:
        payload: Dict[str, Any] = {"chat_id": chat, "text": text[:MAX_TEXT_LEN]}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        _post(TELEGRAM_API.format(token=token),
              json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        return {"sent": True, "reason": None}
    except Exception as exc:  # noqa: BLE001 — транспорт не вправе уронить прогон
        # Сообщение исключения может нести URL целиком (например,
        # http.client.InvalidURL) — а в URL зашит токен бота. token здесь
        # всегда непустой (проверено выше), поэтому маскируем его ДО
        # усечения: обрезка по длине не гарантирует, что токен попадёт в
        # отброшенный хвост.
        reason = f"{type(exc).__name__}: {exc}".replace(token, "***")
        return {"sent": False, "reason": reason[:200]}


def e1_summary(report: Dict[str, Any], dry_run: bool) -> str:
    """Сводка такта записи Э1 — для человека, не для отладки.

    Решение Павла 03.09.2026: прежний формат (по кабинету на строку, коды
    причин отказов, счётчики полос) был нечитаем — «дичь какая-то». Полный
    расклад и так живёт в логе прогона и чёрном ящике; сводка отвечает на
    три человеческих вопроса: что сделал сам, просит ли апрув, есть ли
    проблемы. Кабинеты сворачиваются в сумму — поимённо они нужны только
    когда с кабинетом беда.

    Ключи — из report.accounts[i].result: {applied, failed, dry_run,
    rejected, unknown_outcome, ...} — БЕЗ "stale". В репетиции ничего
    фактически не применяется, поэтому счётчик того, что применилось бы, —
    result["dry_run"], а не result["applied"].
    """
    accounts = report.get("accounts") or []

    def _sum(key: str) -> int:
        return sum(int((a.get("result") or {}).get(key) or 0) for a in accounts)

    applied = _sum("dry_run") if dry_run else _sum("applied")
    failed = _sum("failed")
    unknown = _sum("unknown_outcome")
    rejected = _sum("rejected")
    held = sum(sum((a.get("rejects") or {}).values()) for a in accounts)
    pending = len(report.get("pending_approvals") or [])
    trouble = bool(failed or unknown or report.get("failed_accounts"))

    if dry_run:
        lines = ["Агент · репетиция (в кабинет ничего не писал)",
                 f"Применилось бы: {applied} мелких правок."]
    else:
        status = "есть проблемы" if trouble else "всё ок"
        lines = [f"Агент · {status}",
                 f"Сделал сам: {applied} мелких правок "
                 f"(ставки, площадки, минус-фразы) по {len(accounts)} каб."]
    if held:
        lines.append(f"Ещё {held} отложил из-за внутренних лимитов риска — "
                     "норма, вернётся к ним в следующие дни.")
    # Конвейер новых кампаний — раньше он жил молча: наряд билдеру, сборка,
    # кампания на паузе. Решение Павла 03.09.2026: половина работы агента
    # была невидима — теперь состояние конвейера в каждой сводке.
    queue = report.get("launch_queue") or {}
    building = int(queue.get("building") or 0)
    waiting = int(queue.get("built_waiting") or 0)
    if building or waiting:
        parts = []
        if building:
            parts.append(f"{building} в сборке")
        if waiting:
            parts.append(f"{waiting} собрано, жду твоего «да» на включение")
        lines.append("Новые кампании: " + ", ".join(parts) + ".")
    if pending:
        # Цена вопроса рядом с числом: «прошу апрув: 3» не говорит, три это
        # мелочь или треть недельного риск-бюджета. risk_rub кладёт в строки
        # тот же record_pending_approvals, что и печатает запрос апрува.
        risk = sum(float(row.get("risk_rub") or 0.0)
                   for row in (report.get("pending_approvals") or []))
        cost = f" на {_money(risk)} риска" if risk else ""
        lines.append(f"Прошу апрув: {pending}{cost} — жми кнопки под запросом.")
    else:
        lines.append("Крупных действий не предлагаю.")
    proposals = report.get("proposal_open")
    if proposals:
        lines.append(f"Идей-предложений в копилке: {proposals} — это находки "
                     "без рычага у агента, разберём на недельном разборе.")
    if rejected and not dry_run:
        lines.append(f"Кабинет отверг {rejected} — разберёт разбор недели.")
    if trouble:
        parts = []
        if failed:
            parts.append(f"сбоев {failed}")
        if unknown:
            parts.append(f"неясный исход {unknown}")
        if report.get("failed_accounts"):
            parts.append("кабинеты с ошибкой: " + ", ".join(
                a.get("account", "?") for a in report["failed_accounts"]))
        lines.append("ВНИМАНИЕ: " + ", ".join(parts))
    return "\n".join(lines)


def abort_summary(verdict: str, reason: str, dry_run: bool) -> str:
    """Сводка раннего прерывания такта Э1 — до отчёта по кабинетам.

    Пять точек выхода (RUN_LOCKED, RUN_LEASE_LOST, CONFIG_UNAVAILABLE в
    боевой записи, AUTONOMY_OFF, DATA_GATE_RED) возвращаются раньше NOTIFY
    в конце _run_all — без этой сводки они молчат в Telegram, а тишина
    неотличима от того, что крон не отработал вовсе.
    """
    mode = "репетиция" if dry_run else "боевой прогон"
    return (f"Агент не отработал ({mode}): {verdict}\n{(reason or '—')[:500]}")


def watchdog_summary(out: Dict[str, Any]) -> str:
    """Сводка такта отката (сторож) — для человека, тем же решением
    03.09.2026, что и e1_summary: кабинеты в сумму, коды — в слова.

    Верхний уровень отчёта несёт только alarms/under_watch/
    needs_manual_rollback; rolled_back/breached/rollback_failed/
    closed_verdicts/needs_review живут ПО АККАУНТАМ в out["accounts"][i] —
    breached там уже int (счётчик), не список.
    """
    accounts = out.get("accounts") or []

    def _sum(key: str) -> int:
        return sum(int(a.get(key) or 0) for a in accounts)

    rolled = _sum("rolled_back")
    breached = _sum("breached")
    not_rolled = _sum("rollback_failed")
    review = _sum("needs_review")
    manual = int(out.get("needs_manual_rollback") or 0)
    watch = int(out.get("under_watch") or 0)
    trouble = bool(out.get("alarms") or rolled or breached or not_rolled
                   or manual or review)

    if not trouble:
        return (f"Проверка своих правок: вредных не нашёл, откатывать нечего. "
                f"На замере {watch}.")

    lines = ["Проверка своих правок · ЕСТЬ ПРОБЛЕМЫ"]
    for alarm in out.get("alarms") or []:
        lines.append("ТРЕВОГА: " + str(alarm))
    if rolled:
        lines.append(f"Откатил {rolled} правок, которые навредили кампаниям.")
    if breached:
        lines.append(f"Пробита красная линия у {breached} — CPA хуже порога.")
    if not_rolled:
        lines.append(f"НЕ СМОГ откатить {not_rolled} — смотри лог сторожа.")
    if manual:
        lines.append(f"Нужны руки: {manual} не откатываются автоматически.")
    if review:
        lines.append(f"К разбору недели: {review}.")
    lines.append(f"На замере {watch}.")
    return "\n".join(lines)


def _money(rub: float) -> str:
    """Рубли для человека: тысячные разряды узкими пробелами, копейки прочь.

    Копейка в сводке недели — шум: суммы идут десятками тысяч, а лишние два
    знака мешают увидеть порядок величины с одного взгляда.
    """
    return f"{round(float(rub or 0.0)):,}".replace(",", "\u00a0") + "\u00a0₽"


# Веса находок разбора, которые попадают в сводку человеку. low не попадает
# намеренно: его место в логе прогона. Сводка, где перечислено всё, читается
# как «ничего важного» ровно так же, как пустая.
REVIEW_SEVERITIES = ("high", "medium")

# Сколько находок называется поимённо. Остальные сворачиваются в счётчик:
# двадцать строк подряд человек не читает, а список из трёх с хвостом
# «и ещё 17» честно показывает и главное, и объём.
REVIEW_FINDINGS_SHOWN = 3


def review_summary(result: Dict[str, Any],
                   value: Any = None,
                   actions_now: Any = None,
                   actions_prev: Any = None,
                   days: int = 7) -> str:
    """Сводка недельного разбора — то же, что печатает agent_review, словами.

    Три вопроса недели: сколько агент сделал (и больше или меньше прошлой
    недели), во что это встало и что принесло, обо что он бьётся.

    Про деньги здесь действует правило sync/agent/value.py: НЕИЗМЕРЕННОЕ НЕ
    РАВНО НУЛЮ. Поэтому рядом с суммой всегда едет доля неизмеренного, а при
    нуле измеренных наблюдений сумма не печатается вовсе — «0 ₽» человек
    прочтёт как «агент не принёс ничего», а замер такого не утверждал.

    value/actions_* необязательны: отчёт Э0 за неделю может не найтись
    (стадия молчала — это само по себе находка разбора), и сводка обязана
    уйти всё равно.
    """
    findings = [f for f in (result.get("findings") or [])
                if f.get("severity") in REVIEW_SEVERITIES]
    lines = [f"Разбор недели ({days} дн.)"]

    now = dict(actions_now or {})
    prev = dict(actions_prev or {})
    applied_now = int(now.get("applied") or 0)
    if now:
        applied_prev = int(prev.get("applied") or 0)
        delta = applied_now - applied_prev
        trend = ("столько же, сколько неделей раньше" if delta == 0
                 else f"{'+' if delta > 0 else ''}{delta} к прошлой неделе")
        line = f"Применено: {applied_now} действий ({trend})."
        risk = float(now.get("risk_rub") or 0.0)
        if risk:
            line += f" Под риском {_money(risk)}."
        lines.append(line)
        waiting = int(now.get("pending_approval") or 0)
        rejected = int(now.get("rejected") or 0)
        rolled = int(now.get("rolled_back") or 0)
        tail = []
        if waiting:
            tail.append(f"ждут твоего слова {waiting}")
        if rejected:
            tail.append(f"отклонено тобой {rejected}")
        if rolled:
            tail.append(f"откачено сторожем {rolled}")
        if tail:
            lines.append("Из очереди: " + ", ".join(tail) + ".")

    if value:
        measured = (int(value.get("n_tacts_measured") or 0)
                    + int(value.get("n_actions_measured") or 0))
        if measured:
            share = float(value.get("unmeasured_share") or 0.0)
            parts = []
            saved = float(value.get("saved_rub") or 0.0)
            if saved:
                interval = value.get("did_interval_rub")
                span = (f" (от {_money(interval[0])} до {_money(interval[1])})"
                        if interval else "")
                # Интервал, накрывающий ноль, — это «утверждать нечего», а не
                # «сэкономил столько-то с погрешностью»: слово «сэкономил»
                # рядом с таким интервалом присваивает агенту деньги, которых
                # замер ему не отдал (то же правило, что и у вердикта
                # inconclusive в tact_effect).
                crosses_zero = bool(interval) and interval[0] <= 0 <= interval[1]
                parts.append(f"экономия не отличима от нуля: оценка "
                             f"{_money(saved)}{span}" if crosses_zero
                             else f"сэкономил {_money(saved)}{span}")
            earned = float(value.get("earned_rub") or 0.0)
            if earned:
                parts.append(f"принёс {_money(earned)}")
            cut = float(value.get("cut_rub") or 0.0)
            if cut:
                parts.append(f"вырезал впустую {_money(abs(cut))}")
            if parts:
                lines.append("Деньги: " + ", ".join(parts) + ". "
                             f"Измерено {measured} наблюдений, "
                             f"не измерено {round(share * 100)} % — "
                             "это не ноль, а неизвестность.")
        else:
            lines.append("Деньги: за неделю не измерено ни одного наблюдения "
                         "— утверждать нечего.")

    if not findings:
        lines.append("Проблем разбор не нашёл.")
        return "\n".join(lines)

    high = sum(1 for f in findings if f.get("severity") == "high")
    lines.append(f"Проблем: {len(findings)}"
                 + (f", из них срочных {high}." if high else "."))
    for finding in findings[:REVIEW_FINDINGS_SHOWN]:
        mark = "СРОЧНО: " if finding.get("severity") == "high" else ""
        subject = str(finding.get("subject") or "").strip()
        detail = str(finding.get("detail") or "").strip()
        text = f"{subject} — {detail}" if subject and detail else (subject or detail)
        lines.append(f"· {mark}{text[:220]}")
    if len(findings) > REVIEW_FINDINGS_SHOWN:
        lines.append(f"…и ещё {len(findings) - REVIEW_FINDINGS_SHOWN} — "
                     "полный список в логе разбора.")
    return "\n".join(lines)
