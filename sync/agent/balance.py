# -*- coding: utf-8 -*-
"""
sync/agent/balance.py — баланс такта: рост и эффективность, а не эффективность вместо роста.

Механизм, у которого единственный рычаг — резать неокупающееся, монотонно
улучшает средние и монотонно уменьшает объём: через полгода кабинет
эффективен и вдвое меньше. Продукту нужен рост ПРИ эффективности, поэтому:

  • освободившиеся деньги обязаны иметь адресата (солвер портфеля это уже
    делает переливом, выключение кампаний и минус-фразы — нет);
  • такт, который в сумме уменьшает ожидаемые лиды или оставляет деньги
    никому не назначенными, не применяется целиком: слабейшие сокращения
    снимаются, пока баланс не сойдётся.

Считаем в двух единицах сразу, а не в одной. Ожидаемые ЛИДЫ отвечают на
вопрос «стало ли хуже по объёму», но у выключения кампании и у минус-фразы
ожидания лидов нет вовсе — их никто не считал, и ноль здесь означает «не
мерили», а не «ничего не изменится». Поэтому вторая единица — РУБЛИ:
освобождённые деньги без адресата это отказ от роста независимо от того,
посчитал ли кто-нибудь лиды.
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

# Виды действий, которые сокращают объём ВСЕГДА, даже когда ожидания лидов у
# них нет. Выключение кампании, минус-фраза и запрет площадки существуют
# ровно затем, чтобы отнять расход, — считать их нейтральными из-за пустого
# expected_leads_delta значило бы не заметить единственный вид сжатия,
# который агент сегодня умеет делать массово.
#
# «Даже когда ожидания лидов нет» — здесь ключевое. Правило написано под ноль,
# который означает «не мерили»: у такого действия объём никем не посчитан, и
# единственная защита от сжатия — рубли. Там, где потеря объёма ИЗМЕРЕНА
# (leads_measured, см. ниже), этого основания нет, и плата берётся один раз —
# лидами.
SHRINKING_KINDS = frozenset({SWITCH_KIND, NEGATIVE_KIND, PLACEMENT_KIND})

# Отсечение трафика с ИЗМЕРЕННОЙ потерей конверсий рублей не освобождает.
#
# Два довода, и оба проверяемые. Первый — механика Директа: недельный лимит
# кампании отсечение не трогает, и стратегия перекладывает те же деньги на
# оставшиеся запросы в тот же день. Освобождением это выглядит только в
# арифметике такта, а в кабинете расход кампании не падает. Второй — двойная
# плата: объём, который отсечение отнимает, уже посчитан в expected_leads_delta
# и уже участвует в вердикте; рублёвое требование адресата берёт с того же
# действия вторую плату.
#
# Цена ошибки была измерена 28.08.2026: гигиена делала КАЖДЫЙ такт сжимающим
# (доля адресата 0.34 при требуемых 0.90), require_growth_address снимала
# самые дешёвые действия — то есть ровно её, — и за всё время агента не
# применилось НИ ОДНОЙ минус-фразы и НИ ОДНОГО запрета площадки:
# no_growth_address по EXCLUDED_SITES 20, по NEGATIVE_KEYWORDS 15.
#
# Неизмеренное отсечение (leads_measured=False, кампания в unknown_conversions)
# по-прежнему освобождает деньги и требует адресата: там ноль в лидах — это
# незнание, и рубли остаются единственной защитой.
def _cut_frees_money(entry: Dict[str, Any]) -> bool:
    return not bool(entry.get("leads_measured"))

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
        if _cut_frees_money(cut):
            _free(cut, _num(cut.get("cost_saved")))
        else:
            reallocated += _num(cut.get("cost_saved"))
        if not _is_emergency(cut):
            leads_delta += _leads_delta(cut)

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
        # Переложенное внутри кампаний — отдельным числом. В требование
        # адресата оно не входит, но пропасть из отчёта не имеет права:
        # «гигиена ничего не вырезала» и «гигиена вырезала на 200 тыс ₽,
        # которые остались в тех же кампаниях» — разные новости.
        "reallocated_rub": round(reallocated, 2),
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
        # Выигрыш сокращения — деньги, которые оно возвращает. Отдельного
        # поля у боевых действий нет, и это не пробел: у минус-фразы и у
        # выключения «сколько даёт» и есть вырезанный расход.
        if action.get("expected_gain_rub") is not None:
            return _num(action["expected_gain_rub"])
        return freed_by_key.get(str(action.get("idempotency_key")), 0.0)

    def _is_shrinker(action: Dict[str, Any]) -> bool:
        if _is_emergency(action):
            return False
        if _leads_delta(action) < 0:
            return True
        # Отсечение с измеренным нулём потерь сжатием не является: объём оно
        # не отнимает (это и есть измерение), а деньги оставляет внутри
        # кампании. Снимать его ради баланса значит платить объёмом,
        # которого оно не трогало.
        if (str(action.get("action_kind") or "") in SHRINKING_KINDS
                and _cut_frees_money(action)):
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
                         # Признак приходит с самого действия: его ставит тот,
                         # кто знает, измерялись ли конверсии вырезаемого
                         # (writer/negatives.py, writer/placements.py).
                         # Восстанавливать его здесь по нулю в лидах нельзя —
                         # ноль как раз и двузначен.
                         "leads_measured": bool(action.get("leads_measured")),
                         "expected_leads_delta": _leads_delta(action)})

    # Запертая доливка в moves не попадает — и это не потеря, а требуемое
    # поведение: её рубли остаются в unassigned_rub у сокращений этого такта
    # ровно потому, что назначить их оказалось некому.
    return {"moves": moves, "suspends": suspends, "cuts": cuts}
