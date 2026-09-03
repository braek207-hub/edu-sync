# -*- coding: utf-8 -*-
"""
sync/agent/analyst.py — надзорный слой: модель читает решения кода.

Агент детерминирован: формулы отбирают кандидатов, гейты решают судьбу,
writer исполняет. Сильная сторона формул — воспроизводимость, слабая —
они не видят смысла. Действие может быть корректным по правилу и глупым
по сути; правило может систематически ошибаться; возможность может не
пройти гейт, оставаясь очевидной. Ровно эти три слепых пятна закрывает
модель, читающая ВЕСЬ выхлоп прогона разом.

ГЛАВНОЕ ПРАВИЛО СЛОЯ (этап 1): модель READ-ONLY. Ни один её вердикт не
меняет кабинет, не блокирует действия и не двигает панель. Продукт слоя —
разбор дня человеку и журнал суждений в чёрный ящик: по нему через
неделю-две решается, давать ли модели право вето (этап 2) и полосу
предложений сверх гейтов (этап 3).

Второе правило — то же, что у смыслового слоя (semantic.py): отказ слоя
не трогает агента. Нет ключа, сеть легла, ответ не разобрался — прогон
аналитика помечается SKIPPED, и всё работает ровно как до его появления.

Модуль чистый: собирает контекст из уже прочитанных строк, строит промпт,
разбирает ответ, форматирует сообщение. Базу и API читает sync/agent_analyst.py.
"""

import json
from typing import Any, Dict, List, Optional

# Явный идентификатор, не алиас: алиасы провайдер переназначает молча.
# Opus — сознательно: аналитик читает весь день агента одним запросом в
# сутки, и качество суждения здесь дороже цены вызова.
MODEL = "claude-opus-5"
MAX_TOKENS = 16000

# Бюджеты сжатия контекста в символах. Отчёт Э0 несёт сотни кандидатов и
# в промпт целиком не нужен: решает не полнота, а то, что модель видит
# план, применение и отказы ОДНОГО дня рядом друг с другом.
RUN_REPORT_CHARS = {"e0": 9000, "e1": 9000}
RUN_REPORT_CHARS_DEFAULT = 3000
MAX_ACTIONS = 120
MAX_REJECT_GROUPS = 40
MAX_IDEAS = 60

# Телеграм режет на 4096; свой лимит ниже, чтобы счётчики вердиктов в
# хвосте сообщения не отрезались вместе с текстом разбора.
TELEGRAM_LIMIT = 3500

VERDICT_OK = "OK"
VERDICT_SKIPPED = "SKIPPED"

SYSTEM_PROMPT = """Ты — старший директолог-ревизор над автономным агентом Яндекс Директа образовательного проекта EDUNETWORK (высшее и среднее профессиональное образование, дистанционное обучение).

Агент — детерминированный код. Каждый день он: считает кандидатов действий (корректировки ставок, минус-фразы, запрет площадок, tCPA, бюджеты), прогоняет их через гейты (риск-бюджет недели, лимиты полос, окупаемость λ), применяет прошедшее и пишет журнал отказов. Кампании создаёт на паузе, включает человек.

Твоя роль — надзор, НЕ управление. Твои вердикты сейчас никуда не применяются автоматически: они идут владельцу (Павел, маркетолог) и в журнал, по которому решат, давать ли тебе право вето. Суди строго, но честно: «не вижу проблемы» — законный вердикт, натянутые находки обесценят журнал.

Четыре задачи:
1. digest — разбор дня для владельца. Коротко, по делу, в рублях. Что агент сделал и зачем, где буксует, что требует решения человека. Без пересказа таблиц — только выводы. Plain text без markdown: символы * и _ в именах кампаний ломают разметку.
2. would_veto — действия из плана/применённых, которые корректны по правилам, но нелогичны по сути. Указывай action_id из данных и причину. Пустой список — норма.
3. beyond_gates — возможности, которые гейты отсекли (см. журнал отказов), а по смыслу делать стоит; или которых код вообще не видит. С оценкой эффекта в рублях, где возможно.
4. rule_issues — правила/пороги, систематически дающие странные решения (по повторам в отказах и действиях). Предлагай конкретную правку.

Отвечай СТРОГО одним JSON-объектом без пояснений вокруг:
{"digest": "...", "would_veto": [{"action_id": "...", "reason": "..."}], "beyond_gates": [{"proposal": "...", "why": "...", "expected_rub": 0}], "rule_issues": [{"rule": "...", "evidence": "...", "change": "..."}]}"""


def _clip_json(value: Any, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    # Обрыв честно помечается: модель не должна принимать усечённый JSON
    # за полный и делать выводы из отсутствия хвоста.
    return text[:limit] + f"…<обрезано, полный размер {len(text)} символов>"


def compact_runs(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Прогоны с отчётами, ужатыми до бюджета своей стадии."""
    out = []
    for run in runs:
        stage = str(run.get("stage") or "")
        limit = RUN_REPORT_CHARS.get(stage, RUN_REPORT_CHARS_DEFAULT)
        out.append({
            "stage": stage,
            "mode": run.get("mode"),
            "started_at": str(run.get("started_at") or ""),
            "verdict": run.get("verdict"),
            "report": _clip_json(run.get("report") or {}, limit),
        })
    return out


def compact_actions(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for a in actions[:MAX_ACTIONS]:
        out.append({
            "action_id": a.get("action_id"),
            "account": a.get("account"),
            "kind": a.get("action_kind"),
            "object": f"{a.get('object_level')}:{a.get('object_id')}",
            "status": a.get("status"),
            "risk_rub": a.get("risk_rub"),
            "payload": _clip_json(a.get("payload") or {}, 600),
            "previous": _clip_json(a.get("previous_state") or {}, 300),
        })
    return out


def compact_rejects(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"stage": g.get("stage"), "kind": g.get("kind"),
             "reason": g.get("reason"), "count": g.get("count"),
             "cost_rub": g.get("cost_rub"),
             "sample_keys": g.get("sample_keys")}
            for g in groups[:MAX_REJECT_GROUPS]]


def compact_ideas(ideas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"source": i.get("source"), "lane": i.get("lane"),
             "tier": i.get("tier"), "status": i.get("status"),
             "subject_key": i.get("subject_key"),
             "expected_rub": i.get("expected_rub"),
             "horizon_days": i.get("horizon_days"),
             "dropped_reason": i.get("dropped_reason")}
            for i in ideas[:MAX_IDEAS]]


def build_user_prompt(context: Dict[str, Any]) -> str:
    """Контекст → текст запроса. Блоки идут от решений к обстановке:
    план и применение — предмет суда, панель и бюджет — его рамки."""
    blocks = [
        ("Прогоны агента за период (стадия, режим, вердикт, отчёт)",
         context.get("runs") or []),
        ("Действия (план и применённые)", context.get("actions") or []),
        ("Журнал отказов, сгруппирован по причине", context.get("rejects") or []),
        ("Идеи генераторов за 7 дней", context.get("ideas") or []),
        ("Панель управления (ключ, значение, кто поставил)",
         context.get("config") or []),
        ("Риск-бюджет недели", context.get("risk_budget") or {}),
    ]
    parts = []
    for title, payload in blocks:
        parts.append(f"### {title}\n{json.dumps(payload, ensure_ascii=False, default=str)}")
    parts.append(
        "Дата разбора: " + str(context.get("as_of") or "") +
        ". Дай вердикт по четырём задачам одним JSON-объектом.")
    return "\n\n".join(parts)


def parse_response(raw: str) -> Optional[Dict[str, Any]]:
    """Ответ модели → вердикты. Терпимый разбор: модель может обернуть
    JSON в текст. Не разобралось — None, и это SKIPPED, а не падение."""
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not str(data.get("digest") or "").strip():
        return None
    return {
        "digest": str(data.get("digest")).strip(),
        "would_veto": _row_list(data.get("would_veto")),
        "beyond_gates": _row_list(data.get("beyond_gates")),
        "rule_issues": _row_list(data.get("rule_issues")),
    }


def merged_prompt(context: Dict[str, Any]) -> str:
    """Оба промпта одним текстом — для запуска через CLI без системного канала."""
    return SYSTEM_PROMPT + "\n\n" + build_user_prompt(context)


def _row_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def format_telegram(result: Dict[str, Any]) -> str:
    """Сообщение владельцу: разбор + счётчики суждений. Plain text —
    причина та же, что в notify.py: имена кампаний несут _ и *."""
    digest = str(result.get("digest") or "").strip()
    veto = result.get("would_veto") or []
    beyond = result.get("beyond_gates") or []
    rules = result.get("rule_issues") or []
    tail_lines = [
        "",
        f"Суждения (read-only): вето-кандидатов {len(veto)}, "
        f"сверх гейтов {len(beyond)}, дефектов правил {len(rules)}.",
    ]
    for row in veto[:3]:
        tail_lines.append(f"— вето: {row.get('action_id')}: {_short(row.get('reason'))}")
    for row in beyond[:3]:
        tail_lines.append(f"— сверх гейтов: {_short(row.get('proposal'))}")
    for row in rules[:3]:
        tail_lines.append(f"— правило: {_short(row.get('rule'))}")
    tail = "\n".join(tail_lines)
    # Хвост со счётчиками важнее хвоста разбора: обрезается digest.
    room = TELEGRAM_LIMIT - len(tail) - 20
    header = "Аналитик агента — разбор дня\n\n"
    body = digest[:max(room - len(header), 200)]
    return header + body + tail


def _short(value: Any, limit: int = 160) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"
