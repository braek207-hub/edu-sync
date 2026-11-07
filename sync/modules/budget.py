# -*- coding: utf-8 -*-
"""Модуль budget: применяет одобренные правки стратегии кампании — недельный лимит и цель CPA.

Находки создаёт Panda-BI (расчёт распределения по кнопке), поэтому скана нет: run пустой,
а раннер модуль по расписанию не запускает (runner.is_due). Здесь только запись одобренного
и откат.

Действие находки (рубли целые; недельный лимит без НДС, цель CPA как в стратегии Директа):
    {"op": "set_strategy", "campaign_id": int, "login": str,
     "expect": {"weekly_budget": int|None, "target_cpa": int|None},
     "set": {"weekly_budget"?: int, "target_cpa"?: int}}

expect Panda берёт из strategy_snapshots, поэтому свежее чтение считает рычаги тем же кодом,
что снимок (_snapshot_view), а пишет только в то поле, которое снимок читает (_State).

Порядок на кампанию — как у placements._apply_group: свежее чтение, проверка, что в Директе
стоит то же, что видел человек (expect, ±1 ₽), запись блока BiddingStrategy целиком, чтение
обратно — исход решает оно. Запись лимита берём у агента EDU (writer/budget.py): тот же блок
он пишет в прод.
"""

import copy
import re
from typing import Any, Dict, List, Optional, Tuple

from sync import edu_direct_settings as _eds
from sync.agent.writer.budget import read_weekly_limit, strategy_with_limit
from sync.modules.defaults import settings_for
from sync.modules.types import ApplyResult, Context, RunOutput, StoredFinding
from sync.placements import direct
from sync.strategy_snapshots import serving_channel, snapshot_row

KEY = "budget"
OP = "set_strategy"
LEVERS = ("weekly_budget", "target_cpa")
MICROS = 1_000_000
# Директ хранит микрорубли, человек видел рубли: расхождение до рубля — округление, не правка.
TOLERANCE_RUB = 1.0
# Предел шага правки — те же настройки модуля, по которым её считала Panda (lib/budget/model.ts:
# limitUp/limitDown, cpaUp/cpaDown), в процентах от значения «до».
STEP_SETTING = {"weekly_budget": "stepLim", "target_cpa": "stepCpa"}
# Кампании не в зоне модуля — тот же текст, что EXCLUDED_NAME_SQL в Panda
# (lib/budget/facts.sql.ts): rsv — чужие РК в своих кабинетах, Конкурент и Наш бренд — брендовые.
EXCLUDED_NAME_RE = re.compile("rsv|Конкурент|Наш бренд", re.IGNORECASE)
RUB = "RUB"

# Коды ошибок, по которым Panda пишет свою строку статуса (текст — на её стороне).
CHANGED = "changed_in_direct"
UNSUPPORTED = "unsupported"
BAD_ACTION = "bad_action"
PROTECTED = "protected"
NOT_APPLIED = "Директ принял, но не применил"


def run(ctx: Context) -> RunOutput:
    # scanned_logins=[]: «ничего не прочитано» — сверка не закрывает ни одной находки модуля.
    return RunOutput(findings=[], scanned_logins=[])


def _rub(micros: Optional[int]):
    if micros is None:
        return None
    return micros // MICROS if micros % MICROS == 0 else micros / MICROS


def _whole(v):
    return int(v) if isinstance(v, float) and v.is_integer() else v


def _same(fresh, want) -> bool:
    if fresh is None or want is None:
        return fresh is None and want is None
    return abs(float(fresh) - float(want)) <= TOLERANCE_RUB


def _rub_value(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _err(err: BaseException) -> str:
    return "%s: %s" % (type(err).__name__, err)


def _parse(action: Any) -> Optional[Tuple[int, dict, dict]]:
    """(кампания, expect, set) или None — действие не той формы. В expect оба рычага: без
    второго проверка «в Директе стоит то, что видел человек» шла бы наполовину."""
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
    if set(expect) != set(LEVERS) or any(k not in LEVERS for k in to):
        return None
    if not all(_rub_value(v) for v in to.values()):
        return None
    if not all(v is None or _rub_value(v) for v in expect.values()):
        return None
    return cid, expect, to


def _within_step(expect: dict, to: dict, rules: dict) -> bool:
    """Правка не шире шага, с которым её считала Panda. Panda округляет в сторону меньшей
    правки, так что предел не превышается; допуск в рубль — на округление expect до целых."""
    for k, v in to.items():
        base = expect.get(k)
        if base is None:
            return False  # предел считается от значения «до»; без него правка ничем не ограничена
        step = rules[STEP_SETTING[k]] / 100.0
        if not base * (1 - step) - TOLERANCE_RUB <= v <= base * (1 + step) + TOLERANCE_RUB:
            return False
    return True


def _snapshot_view(tc: dict, name: str) -> Tuple[dict, Optional[str]]:
    """Рычаги так, как их пишет strategy_snapshots (из него Panda берёт expect), и канал,
    который снимок читает. Разбор канала, цен целей и ДРР — функциями витрины настроек EDU
    (edu_direct_settings._parse_campaign), строка — snapshot_row: своя копия разъехалась бы."""
    bs = tc.get("BiddingStrategy") or {}
    search = _eds._enrich_channel_crr(_eds._extract_strategy_channel_full(bs.get("Search")),
                                      bs.get("Search"))
    network = _eds._enrich_channel_crr(_eds._extract_strategy_channel_full(bs.get("Network")),
                                       bs.get("Network"))
    kind = _eds._infer_bid_kind(network.get("biddingStrategyType")
                                or search.get("biddingStrategyType"))
    strategy = {"search": search or None, "network": network or None,
                "priorityGoalsDetails": _eds._merge_priority_goals_details(
                    [tc], is_crr_strategy=kind == "drr")}
    row = snapshot_row("", name, {"meta": {"state": "ON"}, "strategy": strategy})
    shown = serving_channel(strategy)
    if strategy["search"] is not None and shown is strategy["search"]:
        channel = "Search"
    elif strategy["network"] is not None and shown is strategy["network"]:
        channel = "Network"
    else:
        channel = None
    return {k: _whole(row[k]) for k in LEVERS}, channel


def _cpa_holder(block: Any) -> Optional[Tuple[str, str]]:
    """(подблок, поле) цены цели в канале — как читает _extract_strategy_channel_full:
    AverageCpa, а без неё Cpa (PAY_FOR_CONVERSION). Носителей больше одного — неясно, какой
    взял снимок."""
    found = []
    for k, v in (block if isinstance(block, dict) else {}).items():
        if k in ("BiddingStrategyType", "PlacementTypes") or not isinstance(v, dict):
            continue
        field = "AverageCpa" if v.get("AverageCpa") else "Cpa" if v.get("Cpa") else None
        if field:
            found.append((k, field))
    return found[0] if len(found) == 1 else None


def _micros_rub(v):
    try:
        return _rub(int(v))
    except (TypeError, ValueError):
        return None


class _State:
    """Свежее состояние кампании: имя, блок стратегии, рычаги — как в снимке — и куда каждый
    писать. Носитель есть, только если это то самое поле, которое читает снимок: иначе запись
    ушла бы мимо того, что видит человек, а чтение обратно её не нашло бы."""

    def __init__(self, item: dict):
        tc = item.get("TextCampaign") or {}
        self.name = str(item.get("Name") or "")
        self.strategy = tc["BiddingStrategy"]
        self.values, serving = _snapshot_view(tc, self.name)
        # Лимит в обоих каналах или нечитаемый — канал None, как у writer.diff_budget:
        # неприменим только лимит, правка одной цены цели идёт.
        channel, limit, _why = read_weekly_limit(self.strategy)
        self.limit_channel = channel if (
            channel is not None and channel == serving
            and _same(_rub(limit), self.values["weekly_budget"])) else None
        # У «нескольких целей» цена — в PriorityGoals кампании, не в блоке канала: носителя нет.
        self.cpa_holder: Optional[Tuple[str, str, str]] = None
        if serving is not None and self.values["target_cpa"] is not None:
            holder = _cpa_holder(self.strategy.get(serving))
            if holder is not None:
                sub, field = holder
                if _same(_micros_rub(self.strategy[serving][sub][field]),
                         self.values["target_cpa"]):
                    self.cpa_holder = (serving, sub, field)


def _read(acc, cid: int) -> Optional[_State]:
    """None — кампанию писать нельзя: не видна API (мастер кампаний), не текстовая, пакетная
    стратегия или стратегии нет — те же отказы, что у writer.diff_budget. Своё чтение, а не
    writer.fetch_budget_state: снимку нужны цены целей (PriorityGoals), сторожу — имя."""
    res = direct.call(acc.token, acc.login, "campaigns", {"method": "get", "params": {
        "SelectionCriteria": {"Ids": [int(cid)]},
        "FieldNames": ["Id", "Name", "Type"],
        "TextCampaignFieldNames": ["BiddingStrategy", "PackageBiddingStrategy", "PriorityGoals"],
    }})
    item = next((c for c in res.get("Campaigns") or [] if str(c.get("Id")) == str(cid)), None)
    if item is None or item.get("Type") not in (None, "TEXT_CAMPAIGN"):
        return None
    tc = item.get("TextCampaign") or {}
    if (tc.get("PackageBiddingStrategy") or {}).get("StrategyId"):
        return None
    if not isinstance(tc.get("BiddingStrategy"), dict):
        return None
    return _State(item)


def _with(state: _State, to: dict) -> Optional[dict]:
    """Блок BiddingStrategy с новыми значениями; None — у рычага нет носителя, который
    читает снимок."""
    out = state.strategy
    if "weekly_budget" in to:
        if state.limit_channel is None:
            return None
        out = strategy_with_limit(out, state.limit_channel,
                                  int(round(to["weekly_budget"] * MICROS)))
    if "target_cpa" in to:
        if state.cpa_holder is None:
            return None
        channel, sub, field = state.cpa_holder
        out = copy.deepcopy(out)
        out[channel][sub][field] = int(round(to["target_cpa"] * MICROS))
    return out


def _write(acc, cid: int, strategy: dict) -> list:
    """Запись блока; возвращает Warnings Директа — запись прошла, но человеку их стоит видеть."""
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
    return list(results[0].get("Warnings") or [])


def _reached(values: dict, expect: dict, to: dict) -> bool:
    """В Директе уже стоит set, а остальные рычаги — как в expect: это наша же запись, итог
    которой не лёг (процесс убит между записью и итогом), а не чужая правка."""
    return (all(_same(values[k], v) for k, v in to.items())
            and all(_same(values[k], v) for k, v in expect.items() if k not in to))


def _set(acc, cid: int, expect: dict, to: dict, guard: bool) -> ApplyResult:
    """Общий шаг применения и отката: проверить expect, записать to, прочитать обратно.
    guard — сторож имён; у отката его нет: откат снимает нашу же запись."""
    state = _read(acc, cid)
    if state is None:
        return ApplyResult(False, {}, UNSUPPORTED)
    if guard and EXCLUDED_NAME_RE.search(state.name):
        return ApplyResult(False, {"name": state.name}, PROTECTED)
    before = dict(state.values)
    if not all(_same(state.values[k], v) for k, v in expect.items()):
        if _reached(state.values, expect, to):
            return ApplyResult(True, {"before": dict(expect), "after": before, "recovered": True})
        return ApplyResult(False, {"actual": before}, CHANGED)
    strategy = _with(state, to)
    if strategy is None:
        return ApplyResult(False, {"actual": before}, UNSUPPORTED)
    # Таймаут мог прийти после того, как Директ запись принял: чтение идёт при любом исходе,
    # а before лежит в ответе при любом исходе — по нему откатывают и восстанавливают.
    werr, warnings = None, []
    try:
        warnings = _write(acc, cid, strategy)
    except Exception as err:
        werr = err
    try:
        back = _read(acc, cid)
    except Exception as rerr:
        return ApplyResult(False, {"before": before, "unknown": True}, _err(werr or rerr))
    if back is None:
        return ApplyResult(False, {"before": before}, "кампания не читается после записи")
    after = dict(back.values)
    resp = {"before": before, "after": after}
    if warnings:
        resp["warnings"] = warnings
    if all(_same(after[k], v) for k, v in to.items()):
        return ApplyResult(True, resp)
    if werr is not None:
        return ApplyResult(False, resp, _err(werr))
    return ApplyResult(False, resp, NOT_APPLIED)


def _each(ctx: Context, items: List[StoredFinding], step) -> Dict[int, ApplyResult]:
    by_login = {a.login: a for a in ctx.accounts}
    out: Dict[int, ApplyResult] = {}
    # По одной находке со своим свежим чтением: вторая правка той же кампании сверяется
    # с тем, что оставила первая, а не со снимком до неё.
    for f in items:
        parsed = _parse(f.action)
        if parsed is None or str(parsed[0]) != str(f.campaign_id):
            out[f.id] = ApplyResult(False, {}, BAD_ACTION)
            continue
        login = (f.action or {}).get("login") or f.login
        acc = by_login.get(login)
        if acc is None:
            out[f.id] = ApplyResult(False, {}, "кабинет %s недоступен" % login)
            continue
        # Рубли правки — только у рублёвого кабинета: снимок тенге LIME KZ пересчитывает
        # в рубли, а запись ушла бы в Директ тенге.
        currency = (acc.currency or RUB).upper()
        if currency != RUB:
            out[f.id] = ApplyResult(False, {"currency": currency}, UNSUPPORTED)
            continue
        try:
            out[f.id] = step(acc, f, *parsed)
        except Exception as err:  # одна кампания не роняет остальные
            out[f.id] = ApplyResult(False, {}, _err(err))
    return out


def apply(ctx: Context, items: List[StoredFinding]) -> Dict[int, ApplyResult]:
    rules = settings_for(KEY, ctx.settings)

    def one(acc, f, cid, expect, to) -> ApplyResult:
        if not _within_step(expect, to, rules):
            return ApplyResult(False, {}, BAD_ACTION)
        return _set(acc, cid, expect, to, guard=True)
    return _each(ctx, items, one)


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
    return _set(acc, cid, {k: after.get(k) for k in to}, back, guard=False)


def rollback(ctx: Context, items: List[StoredFinding]) -> Dict[int, ApplyResult]:
    return _each(ctx, items, _rollback_one)
