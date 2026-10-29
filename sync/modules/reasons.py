# -*- coding: utf-8 -*-
"""Коды отказов и ошибок модулей.

Python пишет только код, текст и совет рисует Panda (lib/modules/reasons.ts).
Список и порядок закреплены тестами в обоих репо: новый код без пары на той
стороне показал бы человеку «неизвестную причину».
"""

from typing import Any, Dict, Iterable, List, Optional, Tuple

REASON_CODES: Tuple[str, ...] = (
    # Защиты и границы записи.
    "in_never_list", "system_allowlist", "user_protect", "already_excluded_not_robot",
    "list_full", "campaign_type_unsupported", "campaign_stopped", "over_cap",
    # Защиты минус-фраз.
    "in_never_minus", "conflicts_keyword", "covered_by_minus", "family_pays_off",
    # Данные и доступ.
    "no_lead_goals", "goal_not_visible", "non_rub_money", "report_failed", "no_access",
    "direct_rejected", "too_many_goals", "rule_invalid",
    # Добавлены после этапа 2: порядок сохраняет прежние позиции.
    "phrase_invalid", "report_goals_over_cap",
    # После выкладки: ЕПК, которую v5 не даёт менять (ошибка Директа 5005).
    "epk_needs_v501",
)


def refusal(code: str, rule: Optional[str], *, login: Optional[str] = None,
            campaign_id: Optional[int] = None, obj: Optional[str] = None, **extra) -> dict:
    """Отказ с кодом из REASON_CODES: текст и совет к нему рисует Panda.

    Одна форма у всех модулей: раннер и экраны Panda читают отказы площадок и
    запросов одним кодом."""
    out = {"code": code, "object": obj, "campaign_id": campaign_id, "rule": rule,
           "login": login}
    out.update(extra)
    return out


INTERCEPT_KINDS: Tuple[str, ...] = ("taken", "protected")


def intercept(acc: Dict[str, Dict[str, int]], keys: Iterable[str], kind: str) -> None:
    """Правило поймало строку, но действие не его: taken — раньше сработало правило выше
    или встроенное, protected — строку держит защита. Превью одного правила показывает
    полный улов: без этого счёта «делать само» подтверждали бы по заниженным цифрам."""
    for key in keys:
        acc.setdefault(key, {k: 0 for k in INTERCEPT_KINDS})[kind] += 1


def merge_intercepted(dst: Dict[str, Dict[str, int]], src: Dict[str, Dict[str, int]]) -> None:
    for key, counts in src.items():
        slot = dst.setdefault(key, {k: 0 for k in INTERCEPT_KINDS})
        for kind in INTERCEPT_KINDS:
            slot[kind] += int(counts.get(kind) or 0)


def by_rule(user_keys: Iterable[str], findings: Iterable[Any], refused: List[Any],
            over: Dict[str, int], lines: Optional[Dict[str, int]] = None
            ) -> Dict[str, Dict[str, int]]:
    """{правило: {found, refused, over_cap}} для summary.by_rule.

    Правила пользователя (user_keys, уже с префиксом u:) — всегда, даже с нулями:
    человек видит, что правило проверялось. Встроенные — когда есть что считать.
    У protect found — сколько раз он защитил объект. lines — отказы-строки
    встроенных по правилу: кода у них нет, а счёт нужен."""
    out: Dict[str, Dict[str, int]] = {k: {"found": 0, "refused": 0, "over_cap": 0}
                                      for k in user_keys}

    def bump(key, what, n=1):
        out.setdefault(key, {"found": 0, "refused": 0, "over_cap": 0})[what] += n

    for f in findings:
        bump(f.rule, "found")
    for item in refused:
        if not isinstance(item, dict):
            continue
        if item.get("rule"):
            bump(item["rule"], "refused")
        if item.get("code") == "user_protect" and item.get("by"):
            bump(item["by"], "found")
    for key, n in (lines or {}).items():
        bump(key, "refused", n)
    for key, n in over.items():
        bump(key, "over_cap", n)
    return out
