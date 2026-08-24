# -*- coding: utf-8 -*-
"""
sync/agent/writer/rollback.py — красные линии и автооткат (слой 3 защиты).

Автооткат функционально заменяет апрув: вместо «человек предотвращает ошибку
заранее» — «система исправляет её за час». Для рекламы это строго лучше:
результат изменения человек всё равно не предсказывает.

Красная линия ставится вместе с действием, а не после: у каждого применённого
изменения заранее известно, при каком исходе оно считается провалом.

Базовый CPA считается только по кампаниям с эффективными лидами за окно
(sync/agent/db.py::load_baseline_cpa) — у новых и малонаблюдаемых кампаний
базы нет, и вызывающий код передаёт пустой или нулевой baseline. «Нет базы» —
не «всё хорошо»: относительный порог (проценты от базы) на нуле не считается,
поэтому красная линия в этом случае явно помечена (has_baseline=False) и
использует абсолютный аварийный порог вместо относительного — без этого у
самых непредсказуемых кампаний не было бы защиты вообще.

Откат никогда не удаляет: даже отмена добавленной корректировки — это
установка нейтрального коэффициента, а не delete. Нейтраль в шкале Директа
равна 100 (units.API_NEUTRAL), а НЕ нулю: ноль означает «ставка × 0», то есть
максимальное подавление сегмента. Откат, ставящий 0, не отменял бы изменение,
а бил бы сильнее исходного.

Идентификатор корректировки для отката всегда берётся из ответа API
(result.AddResults[].Id для bidmodifier.add), а не придумывается: Id=0 не
существует в Директе, и запрос с ним молча ничего не откатит. Если Id
неизвестен ни в payload, ни в сохранённом response действия — откат
невозможен, и функция явно возвращает None вместо запроса вслепую.
"""

from typing import Any, Dict, Optional, Tuple

from sync.agent.writer.risk import DEFAULT_DAYS_TO_MEASURE
from sync.agent.writer.units import delta_to_api

RED_LINE_TOLERANCE = 0.40      # +40% к базовой метрике
MIN_LEADS_FOR_VERDICT = 20     # до этого объёма вывод делать нельзя

# Объёмная красная линия. CPA-линия слепа к обвалу: вредная правка,
# придушившая кампанию, не набирает ни расхода, ни лидов — CPA-порог молчит,
# min_leads не достигается никогда, изменение живёт «под наблюдением» до
# истечения горизонта. Ожидаемый дневной расход берётся из risk_rub самого
# действия: риск и посчитан как расход за горизонт замера
# (risk.DEFAULT_DAYS_TO_MEASURE) — отдельной оценки не нужно.
SPEND_COLLAPSE_SHARE = 0.30    # дневной расход ниже этой доли ожидания — обвал
MIN_DAYS_FOR_SPEND_VERDICT = 3 # раньше — колебания открутки, не вердикт

# Виды действий, для которых обвал расхода — ИСПОЛНЕНИЕ решения, а не
# деградация: suspend глушит кампанию намеренно.
SPEND_COLLAPSE_EXEMPT_KINDS = ("campaign.suspend",)


def red_line_for(
    action: Dict[str, Any],
    baseline: Dict[str, Any],
    absolute_max_cpa: float,
) -> Dict[str, Any]:
    """Условие, при котором изменение считается провалом.

    База положительная — порог относительный, в процентах от неё. Базы нет
    или она нулевая — относительный порог не считается (проценты от нуля —
    ноль), поэтому используется абсолютный аварийный потолок, а красная
    линия помечается has_baseline=False, чтобы это состояние было видно
    вызывающему коду и сторожу, а не пряталось внутри max_value.

    absolute_max_cpa обязателен и без значения по умолчанию: захардкоженный
    дефолт — произвольное число, никак не связанное с экономикой конкретного
    кабинета. Вызывающий код (sync/agent_e1.py::build_red_line) обязан
    посчитать порог сам из медианы базовых CPA и передать явно; забытый
    аргумент обязан уронить вызов, а не тихо подставить бессмысленное число.
    """
    base_cpa = float(baseline.get("cpa") or 0.0)
    if base_cpa > 0:
        return {
            "metric": "cpa",
            "max_value": round(base_cpa * (1 + RED_LINE_TOLERANCE), 2),
            "min_leads": MIN_LEADS_FOR_VERDICT,
            "baseline_cpa": base_cpa,
            "has_baseline": True,
        }
    return {
        "metric": "cpa",
        "max_value": round(float(absolute_max_cpa), 2),
        "min_leads": MIN_LEADS_FOR_VERDICT,
        "baseline_cpa": base_cpa,
        "has_baseline": False,
    }


def is_breached(red_line: Dict[str, Any], observed: Dict[str, Any]) -> Tuple[bool, str]:
    """Пробита ли красная линия. До минимума наблюдений — никогда.

    Порог сравнивается через явную проверку на None, а не на истинность:
    max_value=0.0 — валидный порог (пробивается любым положительным
    значением), а не «порог не задан». Смешивать эти два случая через
    `if limit and ...` — баг: нулевой порог тогда никогда не пробивался бы.
    """
    leads = int(observed.get("leads") or 0)
    if leads < int(red_line.get("min_leads") or MIN_LEADS_FOR_VERDICT):
        return False, f"недостаточно наблюдений: {leads}"

    limit = red_line.get("max_value")
    if limit is None:
        return False, "порог не задан"

    metric = red_line.get("metric", "cpa")
    value = float(observed.get(metric) or 0.0)
    limit = float(limit)
    if value > limit:
        return True, f"{metric} = {value:.0f} при пределе {limit:.0f}"
    return False, ""


def is_spend_collapsed(
    action: Dict[str, Any], observed: Dict[str, Any],
    days_to_measure: int = DEFAULT_DAYS_TO_MEASURE,
) -> Tuple[bool, str]:
    """Обвалился ли расход объекта после применения действия.

    Сравнивается средний дневной расход окна с ожидаемым дневным
    (risk_rub / days_to_measure). Средний, а не последний день: обвал —
    состояние, а не событие, и один тихий день его не доказывает — как и
    один шумный не опровергает.
    """
    if str(action.get("action_kind") or "") in SPEND_COLLAPSE_EXEMPT_KINDS:
        return False, ""
    days = int(observed.get("days") or 0)
    if days < MIN_DAYS_FOR_SPEND_VERDICT:
        return False, f"наблюдаемых дней {days} из {MIN_DAYS_FOR_SPEND_VERDICT}"
    expected_daily = float(action.get("risk_rub") or 0.0) / days_to_measure
    if expected_daily <= 0:
        return False, "ожидаемый расход неизвестен (risk_rub пуст)"
    actual_daily = float(observed.get("cost") or 0.0) / days
    if actual_daily < SPEND_COLLAPSE_SHARE * expected_daily:
        return True, (f"обвал расхода: {actual_daily:.0f} ₽/день при ожидании "
                      f"{expected_daily:.0f} ₽/день — ниже доли "
                      f"{SPEND_COLLAPSE_SHARE:.0%}")
    return False, ""


def _added_modifier_id(action: Dict[str, Any]) -> Optional[Any]:
    """Id корректировки, добавленной действием bidmodifier.add.

    Директ не позволяет указать Id при add — он приходит только в ответе
    (result.AddResults[].Id) и сохраняется в журнале действий в поле
    response. payload.Id проверяется на случай, если вызывающий код уже
    дописал туда присвоенный Id заранее — сам rollback_payload его не
    придумывает.
    """
    payload = action.get("payload") or {}
    if payload.get("Id") is not None:
        return payload["Id"]

    response = action.get("response") or {}
    add_results = response.get("AddResults") or []
    if add_results and isinstance(add_results, list):
        first = add_results[0] or {}
        return first.get("Id")
    return None


def rollback_payload(action: Dict[str, Any]) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """Запрос, возвращающий объект в прошлое состояние.

    Ничего не удаляет: отмена add — это set нейтрального коэффициента
    (units.delta_to_api(0) == 100), а не delete и не ноль.
    Без известного Id откат невозможен — вслепую не отправляем.

    previous_state.percent хранится в дельтах, как и весь внутренний план,
    поэтому в тело запроса он идёт через ту же границу конверсии, что и
    прямое применение (apply.to_api_call).
    """
    kind = str(action.get("action_kind") or "")
    previous = action.get("previous_state") or {}

    if kind == "bidmodifier.set" and previous.get("Id") is not None:
        return "bidmodifiers", "set", {
            "BidModifiers": [{"Id": previous["Id"],
                              "BidModifier": delta_to_api(previous["percent"])}]
        }

    if kind == "bidmodifier.add":
        modifier_id = _added_modifier_id(action)
        if modifier_id is None:
            return None
        return "bidmodifiers", "set", {
            "BidModifiers": [{"Id": modifier_id, "BidModifier": delta_to_api(0)}]
        }

    if kind == "budget.set":
        # Назад едет ВЕСЬ прежний блок BiddingStrategy из журнала — тем же
        # правилом, что у расписания ниже: пересборка из одного лимита стёрла
        # бы соседние поля стратегии (цель, BidCeiling), настроенные человеком.
        previous_strategy = previous.get("BiddingStrategy")
        campaign_id = (action.get("payload") or {}).get("CampaignId") or action.get("object_id")
        if previous_strategy is None or campaign_id is None:
            return None
        return "campaigns", "update", {
            "Campaigns": [{"Id": int(campaign_id),
                           "TextCampaign": {"BiddingStrategy": previous_strategy}}]
        }

    if kind == "budget.set_daily":
        previous_daily = previous.get("DailyBudget")
        campaign_id = (action.get("payload") or {}).get("CampaignId") or action.get("object_id")
        if previous_daily is None or campaign_id is None:
            return None
        return "campaigns", "update", {
            "Campaigns": [{"Id": int(campaign_id), "DailyBudget": previous_daily}]
        }

    if kind == "campaign.suspend":
        # Отмена паузы — resume, не update: состоянием показов Директ
        # управляет отдельными методами. previous_state.State обязан быть ON —
        # выключение строится только из него (guardrails._check_suspend), и
        # возврат в любое другое состояние resume выразить не может.
        campaign_id = (action.get("payload") or {}).get("CampaignId") or action.get("object_id")
        if str(previous.get("State")) != "ON" or campaign_id is None:
            return None
        return "campaigns", "resume", {
            "SelectionCriteria": {"Ids": [int(campaign_id)]}
        }

    if kind == "schedule.set":
        # Возвращается ВЕСЬ прежний блок TimeTargeting, а не только часы:
        # вместе с расписанием в нём живут праздничный режим и учёт рабочих
        # выходных, настроенные человеком. Собери мы блок заново из одних
        # часов — откат стёр бы чужие настройки, то есть сам стал бы правкой.
        previous_targeting = previous.get("TimeTargeting")
        campaign_id = (action.get("payload") or {}).get("CampaignId") or action.get("object_id")
        if previous_targeting is None or campaign_id is None:
            # Прежнее состояние неизвестно — вслепую не пишем, по тому же
            # правилу, что и у корректировки без Id.
            return None
        return "campaigns", "update", {
            "Campaigns": [{"Id": int(campaign_id), "TimeTargeting": previous_targeting}]
        }

    return None
