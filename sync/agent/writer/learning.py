# -*- coding: utf-8 -*-
"""
sync/agent/writer/learning.py — какие действия перезапускают обучение стратегии.

Справка Директа (b2b.yandex.ru/adv/edu/materials/strategii-direct): обучение
начинается заново при выборе другой стратегии, смене модели атрибуции или
оплаты, ИЗМЕНЕНИИ ОГРАНИЧЕНИЯ РАСХОДА, КОРРЕКТИРОВКЕ ЦЕЛЕВЫХ ДЕЙСТВИЙ
(добавление, смена, удаление) и остановке кампании дольше семи дней. Пока
стратегия учится заново, её решения хуже — и наблюдение за нашим действием
меряет не наше действие, а переобучение.

Отсюда две обязанности модуля:
  • назвать класс каждого вида действия движка;
  • не давать трогать одну кампанию сбрасывающим действием чаще кулдауна.

Классификация — по видам действий apply.to_api_call, а не по полям payload:
вид действия задаётся движком и меняется только правкой кода, поле payload
может прийти любым.

Неизвестный вид — «unknown», и при отборе он ведёт себя как сбрасывающий.
Обратный дефолт («не знаем — значит безопасно») тихо пропускал бы каждый
новый рычаг: ровно тот довод, по которому рельса ALLOWED_ACTION_KINDS
устроена allow-листом, а не блок-листом.

ЭТО НЕ ВТОРОЙ КУЛДАУН. Кулдаун денежных ручек уже работает: budget.apply_cooldown
с окном BUDGET_COOLDOWN_DAYS применяется в agent_e1 к бюджетным действиям и к
цели CPA, по журналу применённых действий (writer_db.recent_action_objects).
Здесь — то же правило «не чаще, чем стратегия успевает выучиться», тем же
числом дней (LEARNING_COOLDOWN_DAYS берётся из budget, а не назначается
заново) и по тому же журналу, но:

  • с ОБЪЯСНЕНИЕМ — что именно сбрасывает обучение и почему;
  • шире по охвату — денежный кулдаун смотрит на свой вид действия
    (бюджет видит бюджет, цель видит цель), а обучение сбивают ещё
    остановка кампании и всё, чего мы про кабинет не знаем;
  • по ОБЪЕКТУ, а не по виду: сброс, устроенный целью CPA, запирает и
    последующую остановку той же кампании.

Порядок рельс в agent_e1 такой, что действие, запертое денежным кулдауном на
стадии desired, сюда не доходит вовсе: у одного действия ровно один запрет и
ровно одна формулировка причины.
"""

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from sync.agent.writer import budget

RESETS_LEARNING = {
    "tcpa.set",          # цель CPA — параметр целевого действия стратегии
    "goal.set",          # само целевое действие: справка называет его прямо
    "strategy.set",      # другая стратегия — обучение с нуля, первым пунктом
    "campaign.suspend",  # остановка дольше семи дней
}

SAFE_FOR_LEARNING = {
    "bidmodifier.set",
    "bidmodifier.add",
    "negative.add",
    "placement.exclude",
}

# geo.set НЕ ЗАПИСАН НИ В ОДНО из двух множеств, и это решение, а не пропуск.
# Справка географию перезапускающей не называет — значит утверждать «сбрасывает»
# нам нечем. Но география решает, в какие аукционы кампания входит вообще: под
# новым списком регионов стратегия торгуется на другом наборе аукционов с
# другой конкуренцией, и накопленное ею на прежнем наборе к нему относится
# ровно настолько, насколько наборы пересекаются. Записать это в безопасные
# значило бы выдать незнание за знание.
#
# Отсюда класс «unknown» — и кулдаун держит гео как сбрасывающее. Тот же
# довод и тот же исход, что у schedule.set: временного таргетинга в списке
# справки тоже нет, а объём показов он тоже меняет.

# Бюджетные действия судятся ВЕЛИЧИНОЙ, а не видом. Справка называет
# перезапускающим «изменение ограничения расхода» без порога, но на практике
# ведения кабинетов (решение Павла 25.08) сдвиг лимита в пределах ±20 %
# стратегию не сбивает — а именно такими шагами и работает перераспределение
# (portfolio.py двигает бюджеты каждый такт). Записать весь класс в
# сбрасывающие значило бы запереть перелив кулдауном в две недели и
# остановить главный механизм.
BUDGET_KINDS = {budget.BUDGET_KIND, budget.BUDGET_DAILY_KIND}
BUDGET_SAFE_DELTA = 0.20

# Обучение занимает недели (справка: «прежде чем стратегия покажет наилучшие
# результаты, как правило, проходит несколько недель»). Две недели — нижняя
# граница этого срока: чаще трогать значит мерить переобучение, а не эффект.
# Число НЕ своё: это тот же кулдаун, что у денежных ручек (budget.py), и две
# копии одного порога разъехались бы при первой же правке одной из них.
LEARNING_COOLDOWN_DAYS = budget.BUDGET_COOLDOWN_DAYS

COOLDOWN_REASON = (
    "обучение стратегии перезапускалось меньше {days} дней назад "
    "({last}) — повторное сбрасывающее изменение мерило бы переобучение, "
    "а не эффект действия"
)


def _budget_values(action: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """Новый и прежний лимит действия (в микрорублях), если оба читаемы."""
    payload = action.get("payload") or {}
    previous = action.get("previous_state") or {}
    if str(action.get("action_kind")) == budget.BUDGET_DAILY_KIND:
        new = (payload.get("DailyBudget") or {}).get("Amount")
        old = (previous.get("DailyBudget") or {}).get("Amount")
    else:
        new = payload.get("WeeklySpendLimit")
        old = previous.get("WeeklySpendLimit")
    try:
        return (float(new), float(old)) if new and old else (None, None)
    except (TypeError, ValueError):
        return None, None


def learning_impact(action: Dict[str, Any]) -> str:
    """Класс действия: 'resets' | 'safe' | 'unknown'."""
    kind = str(action.get("action_kind") or "")
    if kind in BUDGET_KINDS:
        new, old = _budget_values(action)
        if not new or not old:
            # Величину изменения не посчитать — а «безопасно» без неё догадка.
            return "unknown"
        return "safe" if abs(new / old - 1.0) <= BUDGET_SAFE_DELTA else "resets"
    if kind in RESETS_LEARNING:
        return "resets"
    if kind in SAFE_FOR_LEARNING:
        return "safe"
    return "unknown"


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, date):
        return value
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def split_by_learning_cooldown(
    actions: List[Dict[str, Any]],
    last_reset_by_object: Dict[str, Any],
    today: date,
    cooldown_days: int = LEARNING_COOLDOWN_DAYS,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Делит действия на разрешённые и запертые кулдауном обучения.

    Внутри одного прогона счётчик тоже ведётся: два сбрасывающих действия по
    одной кампании в один день — два перезапуска обучения, и второе обязано
    выпасть здесь, а не быть замеченным через две недели по журналу.

    Разрешённые действия возвращаются КАК ПРИШЛИ: класс приписывается строке
    один раз и в одном месте — там, где собирается строка журнала
    (agent_e1), — иначе у половины действий он появлялся бы здесь, а у
    безопасных не появлялся вовсе, и колонка журнала врала бы пропусками.
    """
    allowed: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    seen: Dict[str, date] = {}
    for action in actions:
        impact = learning_impact(action)
        if impact == "safe":
            allowed.append(action)
            continue
        object_id = str(action.get("object_id"))
        last = seen.get(object_id) or _as_date(last_reset_by_object.get(object_id))
        if last is not None and (today - last).days < cooldown_days:
            blocked.append({**action,
                            "learning_impact": impact,
                            "blocked_reason": COOLDOWN_REASON.format(
                                days=cooldown_days, last=last.isoformat()),
                            "last_learning_reset_at": last.isoformat()})
            continue
        seen[object_id] = today
        allowed.append(action)
    return allowed, blocked
