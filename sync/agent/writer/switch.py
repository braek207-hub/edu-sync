# -*- coding: utf-8 -*-
"""
sync/agent/writer/switch.py — рычаг выключения кампаний (Э3.4): строки
campaign_switch Э3.2 → campaigns.suspend.

Кандидата размечает портфель (portfolio._switch_off_candidate): целевой
бюджет кампании стоит на полу капа (×0.5), и предельная окупаемость даже на
полу не дотягивает до порога кабинета λ с запасом SWITCH_OFF_ROI_SHARE.
То есть кампании не помогает ни перенос бюджета, ни урезание — деньги в ней
работают хуже, чем в любой соседней.

Почему класс уверенности самый строгий (campaign_state, 0.97): пауза — не
«ставка чуть ниже». Остановленная автостратегия теряет накопленное обучение,
и включение обратно не возвращает кампанию в ту же точку — эффект не
отматывается откатом полностью, откат лишь возобновляет показы.

Потолок MAX_SUSPENDS_PER_RUN = 1 — своя рельса поверх общего лимита действий:
выключения меняют состав портфеля, на котором считались λ и целевые бюджеты
всех остальных кампаний. Два выключения за прогон означали бы, что второе
принято по экономике, которой после первого уже нет.

Ключ идемпотентности несёт calc_date расчёта: человек мог включить кампанию
обратно, и следующий расчёт, снова назвавший её кандидатом, — это НОВОЕ
решение по новым данным, а не повтор старого. Вечный ключ без даты закрыл бы
повторное выключение навсегда после первого же применения.
"""

import hashlib
from typing import Any, Dict, List, Tuple

from sync.agent.confidence import assess
from sync.agent.writer import exposure

SWITCH_KIND = "campaign.suspend"
CAMPAIGN_SWITCH_SETTING = "campaign_switch"

# Не больше одного выключения за прогон — см. докстринг модуля.
MAX_SUSPENDS_PER_RUN = 1

LOW_CONFIDENCE_REASON = (
    "уверенность в вердикте ниже порога класса campaign_state"
)
# У бюджета строка без rel_error — допуск со счётчиком (сдвиг обратим).
# Здесь наоборот: выключение без ошибки решения не планируется вовсе —
# самый строгий класс не принимает решений по числам без меры их точности.
UNKNOWN_CONFIDENCE_REASON = (
    "ошибка решения неизвестна (rel_error пуст): для выключения кампании "
    "вердикт без меры точности не принимается"
)
NOT_ON_REASON = (
    "кампания не в показах (State={state}): выключать нечего, а трогать "
    "состояние, выставленное человеком или системой, движку нельзя"
)


def _idempotency_key(campaign_id: str, calc_date: str) -> str:
    raw = f"switch:{campaign_id}:suspend:{calc_date}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def plan_switch_offs(
    computed_by_campaign: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Строки campaign_switch → желаемые выключения по кампаниям.

    {"desired": {cid: {"roi_share", "roi_at_floor", "calc_date", "p_sign"}},
     "low_confidence": [...], "confidence_unknown": N}.

    Вердикт пересчитывается здесь из тех же чисел, что записал портфель
    (value = доля предельной окупаемости на полу от λ), — по тому же правилу,
    что у бюджета: источник правды — строка расчёта, а не поле «уверен»,
    сохранённое кем-то раньше.
    """
    desired: Dict[str, Dict[str, Any]] = {}
    low_confidence: List[Dict[str, Any]] = []
    confidence_unknown = 0

    for cid, rows in computed_by_campaign.items():
        row = next((r for r in rows
                    if str(r.get("setting_kind")) == CAMPAIGN_SWITCH_SETTING),
                   None)
        if row is None:
            continue
        roi_share = float(row.get("value") or 0.0)
        if roi_share <= 0:
            continue
        verdict = assess(roi_share, row.get("rel_error"), "campaign_state")
        if verdict["confident"] is None:
            confidence_unknown += 1
            continue
        if not verdict["confident"]:
            low_confidence.append({
                "campaign_id": str(cid), "roi_share": round(roi_share, 3),
                "p_sign": verdict["p_sign"], "min_p_sign": verdict["min_p_sign"],
                "reason": LOW_CONFIDENCE_REASON,
            })
            continue
        desired[str(cid)] = {
            "roi_share": roi_share,
            "roi_at_floor": float(row.get("raw_value") or 0.0),
            "calc_date": str(row.get("calc_date") or ""),
            "p_sign": verdict["p_sign"],
        }

    return {"desired": desired, "low_confidence": low_confidence,
            "confidence_unknown": confidence_unknown}


def fetch_campaign_states(client, campaign_ids: List[str]) -> Dict[str, str]:
    """Текущий State кампаний — свежим чтением, не из витрины.

    State, а не Status: Status описывает модерацию, State — показы
    (ON / SUSPENDED / OFF / ENDED / ARCHIVED). Выключать можно только ON.
    """
    out: Dict[str, str] = {}
    ids = [int(c) for c in campaign_ids]
    if not ids:
        return out
    page = 1000
    for start in range(0, len(ids), page):
        chunk = ids[start:start + page]
        result = client.get("campaigns", {
            "SelectionCriteria": {"Ids": chunk},
            "FieldNames": ["Id", "State"],
        })
        for item in result.get("Campaigns") or []:
            out[str(item.get("Id"))] = str(item.get("State") or "")
    return out


def diff_switch(
    desired: Dict[str, Dict[str, Any]],
    state_by_campaign: Dict[str, str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Желаемые выключения × прочитанное состояние → (действия, отказы).

    Действия отсортированы по roi_share ПО ВОЗРАСТАНИЮ: худшая экономика —
    первой. Порядок значим, потому что потолок cap_suspends пропускает одно
    действие, и это обязано быть самое обоснованное, а не случайное.

    Кампании без записи в state_by_campaign не порождают ни действия, ни
    отказа — их не оказалось в кабинете; это состояние видит вызывающий по
    своему счётчику not_found (тем же правилом, что diff_budget).
    """
    actions: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []

    ordered = sorted(desired, key=lambda c: desired[c]["roi_share"])
    for cid in ordered:
        move = desired[cid]
        state = state_by_campaign.get(str(cid))
        if state is None:
            continue
        if state != "ON":
            refused.append({"campaign_id": str(cid),
                            "reason": NOT_ON_REASON.format(state=state or "—")})
            continue
        actions.append({
            "action_kind": SWITCH_KIND,
            "object_level": "campaign",
            "object_id": str(cid),
            # Единственный рычаг, который и в дельта-модели стоит целую
            # кампанию: выключение ставит под удар весь её расход — дельта
            # здесь и есть объект.
            "exposure": exposure.whole_object_exposure(
                "выключение кампании — под ударом весь её расход"),
            "direct_type": "CAMPAIGN_STATE",
            "key": "suspend",
            "payload": {"CampaignId": int(cid)},
            # previous_state — прочитанный ON: откат (campaigns.resume)
            # возвращает ровно в него, и рельса пути возврата требует его
            # явного присутствия, а не выводит «раз выключали, был включён».
            "previous_state": {"State": "ON"},
            "idempotency_key": _idempotency_key(str(cid), move["calc_date"]),
        })

    return actions, refused


def cap_suspends(
    actions: List[Dict[str, Any]], max_per_run: int = MAX_SUSPENDS_PER_RUN
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Не больше max_per_run выключений за прогон. Остальные ждут следующего.

    Отложенные не отказ и не потеря: следующий расчёт Э0 увидит портфель уже
    без выключенной кампании, пересчитает λ и либо снова назовёт их
    кандидатами (новый calc_date — новый ключ), либо нет.
    """
    return actions[:max_per_run], actions[max_per_run:]
