# -*- coding: utf-8 -*-
"""
sync/agent/approval.py — апрув человека для крупных рычагов (Telegram).

Решение Павла 03.09.2026: крупные действия (пауза кампании, сдвиг бюджета,
целевая цена) едут в кабинет только после «да» в Telegram. Это НЕ тень:
тень (shadow) пишет намерение и не применяет никогда, апрув — очередь на
применение, дверь которой открывает человек. Полоса при этом проходит ВСЕ
гейты и рельсы риска как раньше: человеку показывается то, что агент
действительно собрался сделать, с уже посчитанной ценой.

Контур из двух половин:
  • Э1 (agent_e1.run_account): действия полос approval_lanes после отбора
    и рельс риска не отправляются — ложатся в журнал статусом
    pending_approval, сводка с кодами уходит в Telegram одним сообщением
    в конце прогона.
  • Воркер (sync/agent_approver.py, крон): читает ответы человека
    (getUpdates), одобренные применяет, отклонённые закрывает; молчание
    дольше TTL закрывает само действие — расчёт двухдневной давности
    применять нельзя (тот же довод, что у lanes.select про отложенные).

Формат ответа человека — обычное сообщение боту:
    да 3fa2b1            — применить действие с этим кодом
    нет 3fa2b1           — отклонить
    да все / нет все     — разом всю очередь
    да 3fa2b1 8c41d0     — несколько кодов в одной строке
Код — первые CODE_LEN символов action_id из сводки.

Модуль чистый: ничего не читает и никуда не пишет. Журнал — writer/db,
таблица решений человека — sync/agent/approval_db.py.
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Статус журнала: действие прошло отбор и ждёт слова человека. Не входит ни
# в FINAL_STATUSES (строку можно перевести в applied/rejected), ни в
# LIVE_STATUSES (экспозиции ещё не было) — писать его сюда, а не в writer/db,
# нельзя было бы, но статус НОВЫЙ и ни один существующий SQL его не трогает:
# mark_stale_planned закрывает только 'planned', сторож смотрит LIVE.
PENDING_STATUS = "pending_approval"

# Полосы, чьи действия по умолчанию ждут апрува. Ключ approval_lanes панели
# перебивает список; пустое значение панели означает «дефолт кода», как у
# остальных nullable-ключей.
DEFAULT_APPROVAL_LANES = ("suspend", "allocation")

# Дольше этого действие не ждёт: план посчитан на данных своего дня, и
# «да» через трое суток одобряло бы применение вчерашнего расчёта к
# сегодняшнему кабинету. Следующий прогон пересчитает и спросит заново.
PENDING_TTL_HOURS = 36

CODE_LEN = 6

_YES_WORDS = frozenset({"да", "ок", "ok", "yes", "давай", "применяй", "+"})
_NO_WORDS = frozenset({"нет", "no", "отмена", "стоп", "-"})
_ALL_WORDS = frozenset({"все", "всё", "all"})

_CODE_RE = re.compile(r"^[0-9a-f]{4,32}$")


def approval_lanes(config: Optional[Dict[str, Any]]) -> frozenset:
    """Полосы под апрувом: ключ панели или дефолт кода."""
    raw = (config or {}).get("approval_lanes")
    if not raw:
        return frozenset(DEFAULT_APPROVAL_LANES)
    return frozenset(str(lane) for lane in raw)


def split_for_approval(
    actions: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
    vetoed_keys: Optional[Iterable[str]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(едет сразу, ждёт апрува). Зовётся ПОСЛЕ отбора и рельс риска.

    vetoed_keys — ключи идемпотентности, по которым человек уже сказал
    «нет» (approval_db.vetoed_keys). Такое действие не встаёт в очередь
    заново: план пересчитывается каждый такт, ключ у него тот же, и без
    фильтра вчерашнее вето стиралось бы сегодняшней вставкой строки.
    """
    lanes_set = approval_lanes(config)
    vetoed = {str(k) for k in (vetoed_keys or ())}
    now: List[Dict[str, Any]] = []
    hold: List[Dict[str, Any]] = []
    for action in actions:
        if str(action.get("lane") or "") not in lanes_set:
            now.append(action)
        elif str(action.get("idempotency_key")) in vetoed:
            # Вето живо — действие не едет и в очередь не встаёт; в отчёт
            # прогона оно попадает отказом human_veto у вызывающего.
            hold.append({**action, "_vetoed": True})
        else:
            hold.append(action)
    return now, hold


def short_code(action_id: str) -> str:
    return str(action_id)[:CODE_LEN]


def _payload_gist(action: Dict[str, Any]) -> str:
    """Суть действия в одну короткую фразу — лучшее, что видно из payload."""
    kind = str(action.get("action_kind") or "")
    payload = action.get("payload") or {}
    if kind == "campaign.suspend":
        return "пауза кампании"
    if kind == "campaign.resume":
        name = str(payload.get("CampaignName") or "").strip()
        tail = f" «{name}»" if name else ""
        return f"ВКЛЮЧИТЬ новую кампанию{tail} — собрана, стоит на паузе"
    if kind in ("budget.set", "budget.set_daily", "tcpa.set"):
        strategy = payload.get("BiddingStrategy") or {}
        amounts = _strategy_amounts(strategy)
        label = {"budget.set": "недельный лимит",
                 "budget.set_daily": "дневной бюджет",
                 "tcpa.set": "целевая цена"}[kind]
        if amounts:
            return f"{label}: {' / '.join(amounts)}"
        return label
    if kind == "goal.set":
        return "смена цели оптимизации"
    if kind == "strategy.set":
        return "смена стратегии"
    return kind


def _strategy_amounts(strategy: Dict[str, Any]) -> List[str]:
    """Все денежные поля блока стратегии — в рублях, как есть."""
    out: List[str] = []
    for block in strategy.values():
        if not isinstance(block, dict):
            continue
        for field in ("WeeklySpendLimit", "AverageCpa", "AverageCpi", "BidCeiling"):
            value = block.get(field)
            if isinstance(value, (int, float)):
                out.append(f"{field} {round(float(value) / 1_000_000):,} ₽".replace(",", " "))
    return out


def format_request(rows: List[Dict[str, Any]]) -> str:
    """Сводка очереди для Telegram. rows — строки с action_id.

    Plain text без разметки — контракт notify.send. Хвост с инструкцией
    не режется: без него сообщение — новость, а не запрос.
    """
    lines = [f"Агент просит апрув — {len(rows)}:"]
    for row in rows:
        code = short_code(str(row.get("action_id") or ""))
        risk = float(row.get("risk_rub") or 0.0)
        risk_note = f" — риск {round(risk):,} ₽".replace(",", " ") if risk > 0 else ""
        lines.append(f"[{code}] {_payload_gist(row)}: кампания "
                     f"{row.get('object_id')} ({row.get('account')}){risk_note}")
    lines.append(f"Ответь: да <код> / нет <код> / да все / нет все. "
                 f"Молчание {PENDING_TTL_HOURS} ч = нет.")
    return "\n".join(lines)


def request_buttons(rows: List[Dict[str, Any]]) -> List[List[Dict[str, str]]]:
    """Кнопки к сводке: пара «применить / отклонить» на каждое действие.

    Формат callback_data — «ok:<код>» / «no:<код>», ровно тот, что разбирает
    вебхук Panda-BI (lib/telegram/router.ts). Bot API режет callback_data на 64
    байтах, поэтому в ней только код: остальное вебхук достаёт из журнала.

    Кнопки не отменяют текстовый ответ: разбор «да <код>» остаётся на месте, и
    если вебхук выключен, человек отвечает словами, как раньше.
    """
    keyboard: List[List[Dict[str, str]]] = []
    for row in rows:
        code = short_code(str(row.get("action_id") or ""))
        if not code:
            continue
        keyboard.append([
            {"text": f"Применить {code}", "callback_data": f"ok:{code}"},
            {"text": f"Отклонить {code}", "callback_data": f"no:{code}"},
        ])
    return keyboard


def parse_decisions(texts: Iterable[str]) -> List[Tuple[str, bool]]:
    """Тексты человека → [(код | '*', одобрено)]. Порядок сохраняется:
    позднее слово перебивает раннее у того же кода (решает вызывающий,
    применяя список слева направо).

    Всё, что не разобралось, молча пропускается: чат живой, в нём бывают
    и обычные сообщения.
    """
    out: List[Tuple[str, bool]] = []
    for text in texts:
        words = str(text or "").lower().replace(",", " ").split()
        verdict: Optional[bool] = None
        for word in words:
            if word in _YES_WORDS:
                verdict = True
            elif word in _NO_WORDS:
                verdict = False
            elif verdict is not None and word in _ALL_WORDS:
                out.append(("*", verdict))
            elif verdict is not None and _CODE_RE.match(word):
                out.append((word, verdict))
    return out


def resolve_decisions(decisions: List[Tuple[str, bool]],
                      pending_codes: Iterable[str]) -> Dict[str, bool]:
    """Список решений → {полный код: вердикт} по текущей очереди.

    Код человека может быть короче кода очереди (CODE_LEN — минимум 4):
    матчится префиксом, неоднозначный префикс не матчится вовсе — лучше
    переспросить молчанием, чем применить чужое действие.
    """
    codes = [str(c) for c in pending_codes]
    verdicts: Dict[str, bool] = {}
    for token, approved in decisions:
        if token == "*":
            for code in codes:
                verdicts[code] = approved
            continue
        matched = [c for c in codes if c.startswith(token)]
        if len(matched) == 1:
            verdicts[matched[0]] = approved
    return verdicts
