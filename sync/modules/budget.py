# -*- coding: utf-8 -*-
"""Модуль budget: применяет одобренные правки стратегии кампании — недельный лимит и цель CPA.

Находки создаёт Panda-BI (расчёт распределения по кнопке), поэтому скана нет: run пустой,
а раннер модуль по расписанию не запускает (runner.is_due). Здесь только запись одобренного
и откат.

Действие находки (рубли целые; недельный лимит без НДС, цель CPA как в стратегии Директа):
    {"op": "set_strategy", "campaign_id": int, "login": str,
     "expect": {"weekly_budget": int|None, "target_cpa": int|None},
     "set": {"weekly_budget"?: int, "target_cpa"?: int}}

Порядок на кампанию — как у placements._apply_group: свежее чтение, проверка, что в Директе
стоит то же, что видел человек (expect, ±1 ₽), запись блока BiddingStrategy целиком, чтение
обратно — исход решает оно. Хирургию блока не повторяем, а берём у агента EDU
(writer/budget.py, writer/tcpa.py): тот же блок он пишет в прод.
"""

from typing import Any, Dict, List, Optional, Tuple

from sync.agent.writer.budget import (NO_LIMIT_REASON, fetch_budget_state, read_weekly_limit,
                                      strategy_with_limit)
from sync.agent.writer.tcpa import read_target_cpa, strategy_with_target
from sync.modules.types import ApplyResult, Context, RunOutput, StoredFinding
from sync.placements import direct

KEY = "budget"
OP = "set_strategy"
LEVERS = ("weekly_budget", "target_cpa")
MICROS = 1_000_000
# Директ хранит микрорубли, человек видел рубли: расхождение до рубля — округление, не правка.
TOLERANCE_RUB = 1.0

# Коды ошибок, по которым Panda пишет свою строку статуса (текст — на её стороне).
CHANGED = "changed_in_direct"
UNSUPPORTED = "unsupported"
BAD_ACTION = "bad_action"
NOT_APPLIED = "Директ принял, но не применил"


def run(ctx: Context) -> RunOutput:
    # scanned_logins=[]: «ничего не прочитано» — сверка не закрывает ни одной находки модуля.
    return RunOutput(findings=[], scanned_logins=[])


class _Client:
    """Клиент формы writer.fetch_budget_state (client.get(service, params))."""

    def __init__(self, acc):
        self.acc = acc

    def get(self, service: str, params: dict) -> dict:
        return direct.call(self.acc.token, self.acc.login, service,
                           {"method": "get", "params": params})


def _rub(micros: Optional[int]):
    if micros is None:
        return None
    return micros // MICROS if micros % MICROS == 0 else micros / MICROS


def _same(fresh, want) -> bool:
    if fresh is None or want is None:
        return fresh is None and want is None
    return abs(float(fresh) - float(want)) <= TOLERANCE_RUB


def _rub_value(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _parse(action: Any) -> Optional[Tuple[int, dict, dict]]:
    """(кампания, expect, set) или None — действие не той формы."""
    if not isinstance(action, dict) or action.get("op") != OP:
        return None
    cid = action.get("campaign_id")
    if isinstance(cid, str) and cid.strip().isdigit():
        cid = int(cid.strip())
    if not isinstance(cid, int) or isinstance(cid, bool) or cid <= 0:
        return None
    expect, to = action.get("expect"), action.get("set")
    if not isinstance(expect, dict) or not isinstance(to, dict) or not to:
        return None
    if any(k not in LEVERS for k in list(expect) + list(to)):
        return None
    if not all(_rub_value(v) for v in to.values()):
        return None
    if not all(v is None or _rub_value(v) for v in expect.values()):
        return None
    return cid, expect, to


class _State:
    """Свежее состояние стратегии кампании: блок, канал лимита и значения рычагов в рублях."""

    def __init__(self, strategy: dict):
        self.strategy = strategy
        self.channel, limit, why = read_weekly_limit(strategy)
        # Лимит в обоих каналах (или нечитаемый) — рычаг неприменим, как у diff_budget. Иначе
        # None читался бы «лимит сняли», и Panda вечно предлагала бы «пересчитать».
        self.unsupported = bool(why) and why != NO_LIMIT_REASON
        self.values = {"weekly_budget": _rub(limit), "target_cpa": _rub(read_target_cpa(strategy))}


def _read(acc, cid: int) -> Optional[_State]:
    """None — кампанию писать нельзя: не видна API (мастер кампаний), не текстовая, пакетная
    стратегия или стратегии нет — те же отказы, что у writer.diff_budget."""
    st = fetch_budget_state(_Client(acc), [str(cid)]).get(str(cid))
    if st is None or st.get("package_id"):
        return None
    if st.get("campaign_type") not in (None, "TEXT_CAMPAIGN"):
        return None
    strategy = st.get("strategy")
    return _State(strategy) if isinstance(strategy, dict) else None


def _with(state: _State, to: dict) -> Optional[dict]:
    """Блок BiddingStrategy с новыми значениями; None — у стратегии нет носителя рычага."""
    out = state.strategy
    if "weekly_budget" in to:
        if state.channel is None:
            return None
        out = strategy_with_limit(out, state.channel, int(round(to["weekly_budget"] * MICROS)))
    if "target_cpa" in to:
        if state.values["target_cpa"] is None:
            return None
        out = strategy_with_target(out, int(round(to["target_cpa"] * MICROS)))
    return out


def _write(acc, cid: int, strategy: dict) -> None:
    res = direct.call(acc.token, acc.login, "campaigns", {
        "method": "update",
        "params": {"Campaigns": [{"Id": int(cid), "TextCampaign": {"BiddingStrategy": strategy}}]},
    })
    results = res.get("UpdateResults") or []
    if not results:
        raise direct.DirectError("campaigns.update: Директ не вернул UpdateResults")
    errors = results[0].get("Errors") or []
    if errors:
        raise direct.DirectError("; ".join(
            "%s %s %s" % (e.get("Code"), e.get("Message"), e.get("Details") or "") for e in errors))


def _set(acc, cid: int, expect: dict, to: dict) -> ApplyResult:
    """Общий шаг применения и отката: проверить expect, записать to, прочитать обратно."""
    state = _read(acc, cid)
    if state is None or state.unsupported:
        return ApplyResult(False, {}, UNSUPPORTED)
    before = {k: state.values[k] for k in LEVERS}
    if not all(_same(state.values[k], v) for k, v in expect.items()):
        return ApplyResult(False, {"actual": before}, CHANGED)
    strategy = _with(state, to)
    if strategy is None:
        return ApplyResult(False, {"actual": before}, UNSUPPORTED)
    # Таймаут мог прийти после того, как Директ запись принял: чтение идёт при любом исходе.
    werr = None
    try:
        _write(acc, cid, strategy)
    except Exception as err:
        werr = err
    try:
        back = _read(acc, cid)
    except Exception as rerr:
        raise werr or rerr
    if back is None:
        return ApplyResult(False, {"before": before}, "кампания не читается после записи")
    after = {k: back.values[k] for k in LEVERS}
    resp = {"before": before, "after": after}
    if all(_same(after[k], v) for k, v in to.items()):
        return ApplyResult(True, resp)
    if werr is not None:
        return ApplyResult(False, resp, "%s: %s" % (type(werr).__name__, werr))
    return ApplyResult(False, resp, NOT_APPLIED)


def _each(ctx: Context, items: List[StoredFinding], step) -> Dict[int, ApplyResult]:
    by_login = {a.login: a for a in ctx.accounts}
    out: Dict[int, ApplyResult] = {}
    # По одной находке со своим свежим чтением: вторая правка той же кампании сверяется
    # с тем, что оставила первая, а не со снимком до неё.
    for f in items:
        parsed = _parse(f.action)
        if parsed is None:
            out[f.id] = ApplyResult(False, {}, BAD_ACTION)
            continue
        login = (f.action or {}).get("login") or f.login
        acc = by_login.get(login)
        if acc is None:
            out[f.id] = ApplyResult(False, {}, "кабинет %s недоступен" % login)
            continue
        try:
            out[f.id] = step(acc, f, *parsed)
        except Exception as err:  # одна кампания не роняет остальные
            out[f.id] = ApplyResult(False, {}, "%s: %s" % (type(err).__name__, err))
    return out


def _apply_one(acc, f, cid, expect, to) -> ApplyResult:
    return _set(acc, cid, expect, to)


def _rollback_one(acc, f, cid, _expect, to) -> ApplyResult:
    """Вернуть рычаги, которые меняла находка, к response.before; в Директе обязано стоять
    response.after — иначе их после нас правил человек, и откат затёр бы его правку."""
    resp = f.response if isinstance(f.response, dict) else {}
    before, after = resp.get("before"), resp.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ApplyResult(False, {}, "нет снимка до применения — откатывать нечем")
    back = {k: before.get(k) for k in to}
    if not all(_rub_value(v) for v in back.values()):
        return ApplyResult(False, {}, UNSUPPORTED)
    return _set(acc, cid, {k: after.get(k) for k in to}, back)


def apply(ctx: Context, items: List[StoredFinding]) -> Dict[int, ApplyResult]:
    return _each(ctx, items, _apply_one)


def rollback(ctx: Context, items: List[StoredFinding]) -> Dict[int, ApplyResult]:
    return _each(ctx, items, _rollback_one)
