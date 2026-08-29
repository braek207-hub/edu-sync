# -*- coding: utf-8 -*-
"""
sync/agent/balance.py — баланс такта: рост и эффективность, а не эффективность вместо роста.

Механизм, у которого единственный рычаг — резать неокупающееся, монотонно
улучшает средние и монотонно уменьшает объём: через полгода кабинет
эффективен и вдвое меньше. Продукту нужен рост ПРИ эффективности, поэтому:

  • освободившиеся деньги обязаны иметь адресата (солвер портфеля это уже
    делает переливом, выключение кампании и срез лимита — нет);
  • такт, который в сумме уменьшает ожидаемые лиды или оставляет деньги
    никому не назначенными, не применяется целиком: слабейшие сокращения
    снимаются, пока баланс не сойдётся.

Считаем в двух единицах сразу, а не в одной. Ожидаемые ЛИДЫ отвечают на
вопрос «стало ли хуже по объёму», но у выключения кампании ожидания лидов нет
вовсе — его никто не считал, и ноль здесь означает «не мерили», а не «ничего
не изменится». Поэтому вторая единица — РУБЛИ:
освобождённые деньги без адресата это отказ от роста независимо от того,
посчитал ли кто-нибудь лиды.

И различаем ДВА разных факта, а не один:

  • ОТНЯЛИ расход у кабинета — выключение кампании, срез недельного или
    дневного лимита. Деньги вышли из кампании, и адресат у них обязателен;
  • ПЕРЕСТАВИЛИ расход внутри объекта — минус-фраза, запрет площадки, сужение
    гео при неизменном лимите. Лимит кампании не двинулся, стратегия разложит
    те же деньги по оставшимся запросам в тот же день. В freed_rub это не
    входит и адресата не требует.

Признак — не вид действия, а движение ЛИМИТА кампании: ноль означает
перестановку. Перестановка в балансе не участвует ни рублями, ни лидами —
разбор в REARRANGING_KINDS ниже, — и гейт роста к ней не применяется вовсе:
гигиена деньги под удар не ставит по построению.
"""

from typing import Any, Dict, List, Tuple

from sync.agent.writer.budget import BUDGET_DAILY_KIND, BUDGET_KIND
from sync.agent.writer.negatives import NEGATIVE_KIND
from sync.agent.writer.placements import PLACEMENT_KIND
from sync.agent.writer.switch import SWITCH_KIND

# Доля освободившихся денег, которая обязана быть назначена адресатам.
# Решение Павла 25.08.2026 («рост и эффективность вместе»). Не 100 %:
# кап записи (writer/budget.MAX_WRITE_STEP) и округление лимита до целых
# рублей оставляют хвост в единицы процентов, и придираться к нему значит
# блокировать такт из-за копеек.
MIN_ASSIGNED_SHARE = 0.9

# Аварийное сокращение — не оптимизация, а тормоз. Требовать под откат
# «куда переливаем» значит держать заведомо убыточное изменение живым до тех
# пор, пока солвер не найдёт адресата. Такие действия гейт пропускает всегда,
# и в баланс такта они входят только справочно.
#
# Множество ПУСТОЕ, и это результат сверки, а не забывчивость: на 2026-08-25
# ни один аварийный путь не проходит через конвейер планирования Э1. Откат
# (agent_e1_watchdog.rollback_one), вердикт обвала расхода
# (writer/rollback.py: SPEND_COLLAPSE_SHARE) и запрет вредного сегмента
# (mark_harmful_verdict → writer_db.harmful_segments) живут в стороже и
# уходят в кабинет своим вызовом, минуя этот гейт. Вида, которого нет в
# writer/apply.to_api_call, здесь быть не должно — он выглядел бы работающим
# исключением, ничего не исключая. Второй путь, флаг `emergency` на действии,
# работает и на пустом множестве: сторож, которому однажды понадобится
# отправить тормоз через Э1, пометит им строку.
EMERGENCY_KINDS: frozenset = frozenset()

# Виды, которые расход не отнимают, а ПЕРЕКЛАДЫВАЮТ внутри кампании. Лимит
# кампании они не трогают: недельный или дневной потолок после минус-фразы
# тот же, и стратегия в тот же день разложит те же деньги по оставшимся
# запросам. Это и есть «rub_delta кампании равен нулю» — признак перестановки
# из плана пропускной способности (docs/AGENT-THROUGHPUT-PLAN.md, A2).
#
# Перестановка не участвует в балансе такта НИ ОДНОЙ из двух единиц:
#
#   • РУБЛИ. Требовать адресата у денег, которые из кампании не выходили,
#     значит запрещать чистку ради роста, которого чистка не отменяла.
#   • ЛИДЫ. Те же деньги остаются в кампании и покупают конверсии на
#     оставшихся запросах. Вырезаемое отобрано как заведомо худшее:
#     objects.CPA_OVERSHOOT = 2.0 — конверсии кандидата стоят вдвое дороже
#     допустимого, либо их нет вовсе при расходе выше трёх CPA. Записать
#     такому действию потерю объёма, не записав встречную покупку, — это тот
#     же дефект D2 этажом ниже.
#
# Списывать одно и не списывать другое нельзя: заряди лиды, не позволяя
# снять само отсечение, — и гейт начнёт компенсировать чужую гигиену
# снятием доливок и выключений, пока не снимет их все.
#
# Раньше здесь стоял список SHRINKING_KINDS (выключение + минус-фраза +
# запрет площадки), и цена ошибки измерена на проде 29.08.2026: гигиена
# делала КАЖДЫЙ такт сжимающим (28.08, edu_agent_runs: freed_rub 153 528 ₽
# при assigned_share 0.0), require_growth_address снимала самые дешёвые
# действия — то есть ровно её. За всё время работы агента гейт снял по
# no_growth_address 23 запрета площадок и 15 минус-фраз (27 795 ₽/дн
# вырезаемого трафика), а в кабинет не уехало НИ ОДНОЙ минус-фразы и НИ
# ОДНОГО запрета площадки: в edu_agent_actions negative.add нет вовсе,
# placement.exclude — четыре строки, все dry_run.
#
# Что осталось предохранителем вместо гейта: отбор кандидатов (правило трёх
# и двойной перебор цены), доля вырезаемого расхода за такт
# (lanes.HYGIENE_MAX_CUT_SHARE = 5 % недельного расхода кабинета) и красная
# линия с откатом на горизонте замера в 3 дня.
REARRANGING_KINDS = frozenset({NEGATIVE_KIND, PLACEMENT_KIND})

BUDGET_KINDS = frozenset({BUDGET_KIND, BUDGET_DAILY_KIND})

NO_ADDRESS_REASON = (
    "сокращение без адресата роста: такт в сумме уменьшает ожидаемые лиды "
    "({delta:+.1f}) или оставляет {unassigned:.0f} ₽ никому не назначенными, "
    "а компенсировать нечем — усиление не найдено"
)


def _num(value: Any) -> float:
    return float(value or 0.0)


def _is_emergency(entry: Dict[str, Any]) -> bool:
    return (str(entry.get("action_kind") or "") in EMERGENCY_KINDS
            or bool(entry.get("emergency")))


def _is_rearrangement(entry: Dict[str, Any]) -> bool:
    """Перекладывает ли действие расход ВНУТРИ кампании, не двигая её лимит."""
    return str(entry.get("action_kind") or "") in REARRANGING_KINDS


def _leads_delta(entry: Dict[str, Any]) -> float:
    """Ожидание солвера по лидам — с верхнего уровня или из payload.

    У боевого действия оно лежит в payload (writer/budget._expectation_payload):
    туда его кладут, чтобы ожидание уехало в журнал вместе со строкой. Гейт,
    читающий только верхний уровень, считал бы каждую доливку нулевой.

    Предпочитается КАЛИБРОВАННОЕ ожидание, когда оно есть: вопрос «сжимает ли
    такт объём» — про то, что действительно случится, а история собственных
    промахов ровно об этом и знает. Сырое остаётся мерой самой поправки
    (learning_loop.forecast_bias) и здесь работает запасным, когда истории
    ещё нет.
    """
    payload = entry.get("payload") or {}
    for key in ("expected_leads_delta_calibrated", "expected_leads_delta"):
        if entry.get(key) is not None:
            return _num(entry[key])
        if payload.get(key) is not None:
            return _num(payload[key])
    return 0.0


def tact_balance(moves: List[Dict[str, Any]], suspends: List[Dict[str, Any]],
                 cuts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Сколько такт освобождает, сколько назначает и куда идёт объём."""
    freed = 0.0
    added = 0.0
    leads_delta = 0.0
    emergency_freed = 0.0
    reallocated = 0.0
    reallocated_leads = 0.0
    # Сколько денег вернёт снятие каждого отдельного действия. Без этой карты
    # гейт не может остановиться на первом достаточном снятии и вынужден
    # резать весь список.
    freed_by_key: Dict[str, float] = {}

    def _free(entry: Dict[str, Any], rub: float) -> None:
        nonlocal freed, emergency_freed
        if _is_emergency(entry):
            emergency_freed += rub
            return
        freed += rub
        key = entry.get("idempotency_key")
        if key is not None and rub > 0:
            freed_by_key[str(key)] = freed_by_key.get(str(key), 0.0) + rub

    for move in moves:
        if _is_emergency(move):
            emergency_freed += max(0.0, _num(move.get("cost_28d")) - _num(move.get("target_28d")))
            continue
        delta = _num(move.get("target_28d")) - _num(move.get("cost_28d"))
        if delta < 0:
            _free(move, -delta)
        else:
            added += delta
        leads_delta += _leads_delta(move)
    for suspend in suspends:
        _free(suspend, _num(suspend.get("cost_28d")))
        if not _is_emergency(suspend):
            leads_delta += _leads_delta(suspend)
    for cut in cuts:
        # Отсечение переставляет расход внутри кампании: её лимит не двинулся
        # (REARRANGING_KINDS). Ни рубли, ни лиды в баланс такта не идут — оба
        # числа уезжают отдельными полями отчёта, чтобы вырезанное было
        # видно, но ничего не запирало.
        reallocated += _num(cut.get("cost_saved"))
        reallocated_leads += _leads_delta(cut)

    unassigned = max(0.0, freed - added)
    assigned_share = added / freed if freed > 0 else 1.0
    return {
        "freed_rub": round(freed, 2),
        "added_rub": round(added, 2),
        "unassigned_rub": round(unassigned, 2),
        "assigned_share": round(assigned_share, 4),
        # Освобождённое тормозом — отдельным числом: оно не участвует ни в
        # вердикте, ни в требовании адресата, но исчезать из отчёта не имеет
        # права, иначе «такт ничего не освободил» и «такт освободил аварийно»
        # выглядят одинаково.
        "emergency_freed_rub": round(emergency_freed, 2),
        # Переложенное внутри кампаний — двумя отдельными числами, и это
        # ЕДИНСТВЕННОЕ место, где вырезанное видно. В баланс они не входят
        # (лимит кампании не двигался), но пропасть из отчёта не имеют права:
        # «гигиена ничего не вырезала» и «гигиена вырезала на 200 тыс ₽,
        # которые остались в тех же кампаниях» — разные новости. Второе число —
        # заявленная рычагом потеря конверсий на вырезаемом трафике; встречную
        # покупку на оставшихся запросах никто не считает, поэтому судить по
        # нему такт нельзя, а видеть его надо: недельный разбор смотрит, не
        # растёт ли оно вместе с объёмом чистки.
        "reallocated_rub": round(reallocated, 2),
        "reallocated_leads_delta": round(reallocated_leads, 1),
        "freed_by_key": freed_by_key,
        "expected_leads_delta": round(leads_delta, 1),
        "shrinking": bool(leads_delta < 0
                          or (freed > 0 and assigned_share < MIN_ASSIGNED_SHARE)),
    }


def require_growth_address(
    actions: List[Dict[str, Any]], balance: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Снимает слабейшие сокращения, пока такт остаётся сжимающим.

    Слабейшее — с наименьшим ожидаемым выигрышем в рублях: снимать сначала то,
    что меньше всего даёт. Порядок детерминирован (выигрыш, затем object_id),
    иначе два прогона на одних данных дали бы разные планы.
    """
    if not balance.get("shrinking"):
        return actions, []

    freed_by_key: Dict[str, float] = balance.get("freed_by_key") or {}

    def _gain(action: Dict[str, Any]) -> float:
        # Выигрыш сокращения — деньги, которые оно возвращает кабинету.
        # Отдельного поля у боевых действий нет, и это не пробел: у
        # выключения и у среза лимита «сколько даёт» и есть снятый расход.
        if action.get("expected_gain_rub") is not None:
            return _num(action["expected_gain_rub"])
        return freed_by_key.get(str(action.get("idempotency_key")), 0.0)

    def _is_shrinker(action: Dict[str, Any]) -> bool:
        if _is_emergency(action):
            return False
        # Перестановка внутри кампании гейту не показывается вовсе: она не
        # отнимает у кабинета ни рубля и в балансе такта не участвует, значит
        # снятием её ничего не исправить. До 29.08.2026 всё было наоборот —
        # гигиена стоила дешевле всех и снималась первой: 38 снятых действий
        # и ноль применённых за всё время работы агента.
        if _is_rearrangement(action):
            return False
        if _leads_delta(action) < 0:
            return True
        return freed_by_key.get(str(action.get("idempotency_key")), 0.0) > 0

    delta = _num(balance.get("expected_leads_delta"))
    freed = _num(balance.get("freed_rub"))
    added = _num(balance.get("added_rub"))
    reason = NO_ADDRESS_REASON.format(delta=delta,
                                      unassigned=max(0.0, freed - added))

    shrinkers = sorted((a for a in actions if _is_shrinker(a)),
                       key=lambda a: (_gain(a), str(a.get("object_id"))))

    blocked: List[Dict[str, Any]] = []
    blocked_ids: set = set()
    for action in shrinkers:
        if delta >= 0 and (freed <= 0 or added / freed >= MIN_ASSIGNED_SHARE):
            break
        # Снятие возвращает и объём, и деньги: сокращения больше нет, значит
        # его рубли больше не свободны и адресата не требуют.
        delta -= _leads_delta(action)
        freed = max(0.0, freed - freed_by_key.get(
            str(action.get("idempotency_key")), 0.0))
        blocked.append({**action, "blocked_reason": reason})
        blocked_ids.add(id(action))

    allowed = [a for a in actions if id(a) not in blocked_ids]
    return allowed, blocked


def balance_inputs(
    actions: List[Dict[str, Any]],
    moves_by_campaign: Dict[str, Dict[str, Any]],
    cost_28d_by_campaign: Dict[str, float],
    cut_cost_by_kind: Dict[str, Dict[str, float]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Действия такта → три списка для tact_balance.

    Считается по ДОШЕДШИМ до этой точки действиям, а не по плану целиком.
    Гейты (кулдауны, потолок попыток, закрытые ключи) стоят ПОСЛЕ солвера:
    кампания, которой солвер назначил рост, могла до сюда не дойти — и её
    рубли обязаны остаться неназначенными, а не исчезнуть вместе со строкой.
    """
    moves: List[Dict[str, Any]] = []
    suspends: List[Dict[str, Any]] = []
    cuts: List[Dict[str, Any]] = []

    for action in actions:
        kind = str(action.get("action_kind") or "")
        cid = str(action.get("object_id"))
        common = {"campaign_id": cid,
                  "idempotency_key": action.get("idempotency_key"),
                  "emergency": _is_emergency(action)}
        if kind in BUDGET_KINDS:
            move = moves_by_campaign.get(cid)
            if move is None:
                continue
            moves.append({**common,
                          "cost_28d": _num(move.get("cost_28d")),
                          "target_28d": _num(move.get("target_28d")),
                          "expected_leads_delta": _leads_delta(action)})
        elif kind == SWITCH_KIND:
            suspends.append({**common,
                             "cost_28d": _num(cost_28d_by_campaign.get(cid)),
                             "expected_leads_delta": _leads_delta(action)})
        elif kind in (NEGATIVE_KIND, PLACEMENT_KIND):
            cuts.append({**common, "kind": kind,
                         "cost_saved": _num(
                             (cut_cost_by_kind.get(kind) or {}).get(cid)),
                         "expected_leads_delta": _leads_delta(action)})

    # Запертая доливка в moves не попадает — и это не потеря, а требуемое
    # поведение: её рубли остаются в unassigned_rub у сокращений этого такта
    # ровно потому, что назначить их оказалось некому.
    return {"moves": moves, "suspends": suspends, "cuts": cuts}
