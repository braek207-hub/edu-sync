# -*- coding: utf-8 -*-
"""
sync/agent/writer/guardrails.py — рельсы движка записи (слой 2 защиты).

Ограничения, которые агент не пересекает никогда — независимо от того, что
насчитала статистика и что предложил LLM:

  - удаление объектов запрещено (пауза вместо удаления);
  - корректировка ставки ограничена ±50%: расчёт уже сжат к нулю, всё что
    выходит за коридор — признак ошибки, а не находки;
  - заповедник неприкосновенен: его кампании не получают ни одного действия,
    иначе база сравнения для всех замеров теряется;
  - число действий за прогон ограничено: массовое изменение невозможно
    проверить сторожем и невозможно осмысленно откатить;
  - откат обязан возвращать ИМЕННО в прошлое состояние журнала, а не в любое
    значение, которое примет API: рельса возврата сверяет намерение, а не
    только форму запроса (check_rollback).
"""

from typing import Any, Dict, List, Optional, Set, Tuple

from sync.agent.writer.units import API_MAX, API_MIN, API_NEUTRAL

# Рельса работает по ДЕЛЬТЕ, а не по 100-базному коэффициенту Директа: в
# payload действия (diff.py) лежит внутренняя единица движка, перевод в шкалу
# API делается позже и только один раз — в apply.to_api_call через
# units.delta_to_api. Смысл рельсы: не выпускать корректировку за ±50 % от
# исходной ставки, то есть |дельта| <= 50 (в шкале API это коридор 50..150).
# Та же цифра, приложенная к 100-базе, разрешала бы 0..50 — «срезать ставку
# вдвое и сильнее», ровно наоборот к замыслу.
MODIFIER_CAP = 50          # потолок и пол корректировки, проценты (дельта)

# Allow-лист: разрешено ровно это, всё остальное отклоняется. Не блок-лист по
# словам — тот пропускает любой ещё не придуманный вид действия (purge,
# campaign.archive, adgroups.suspend, ...) молча, а рельса обязана держать
# "никогда", а не эвристику по подстроке.
ALLOWED_ACTION_KINDS = {"bidmodifier.add", "bidmodifier.set", "schedule.set",
                        "budget.set", "budget.set_daily", "campaign.suspend",
                        "tcpa.set", "goal.set", "strategy.set", "negative.add",
                        "placement.exclude", "negative.remove_added"}

# Виды, у которых рычага записи на стороне агента нет и не будет: тело
# кампании (группы, ключи, объявления) собирает ДРУГОЙ репозиторий, и агент
# физически не может отправить campaigns.add. Отказ им нужен свой, а не общий
# «вне allow-листа»: общий читается как недоделка, а это устройство системы —
# наряд едет билдеру, из кабинета кампанию заводит он.
BUILDER_ACTION_KINDS = {"campaign.create"}
BUILDER_REASON = (
    "создание кампании — наряд билдеру, а не запрос к API Директа: тело "
    "кампании собирает другой репозиторий, и отправить его агент не может"
)

# Виды со словом remove/delete в имени, которым сторож удаления объектов
# ходу не закрывает. Здесь ровно один, и это не послабление, а различение:
# запрет касается УДАЛЕНИЯ ОБЪЕКТОВ (кампаний, групп, объявлений,
# корректировок) — потери, которую нечем отменить. Снятие своей же минус-фразы
# правит ПОЛЕ живого объекта и является единственным способом отменить
# собственное отсечение, не затирая при этом фраз человека
# (negatives.remove_added).
REMOVAL_ALLOWED_ACTION_KINDS = {"negative.remove_added"}

# Коридор нового лимита ОТНОСИТЕЛЬНО ПРЕЖНЕГО РАСХОДА кампании (не прежнего
# лимита: тот бывает в разы выше расхода — 5 млн/нед при расходе 616 тыс. —
# и легитимный перенос к целевому расходу вылетал бы за любой коридор).
# Портфель капит сдвиг ×0.5–1.5, а при доказанном недоборе трафика — ×2
# (portfolio.step_cap_up); коридор шире политики, потому что рельса ловит не
# политику, а слом конверсии единиц: недели вместо дней (×7), микрорубли
# вместо рублей (×10⁶) — всё это выносит соотношение за края, и ×2.1 от них
# по-прежнему далеко.
BUDGET_RATIO_MIN = 0.4
BUDGET_RATIO_MAX = 2.1

# Коридор новой ЦЕЛИ CPA относительно ФАКТИЧЕСКОГО CPA окна. Рельса ловит не
# политику (её капит writer/tcpa.clamp_step), а слом единиц: рубли вместо
# микрорублей и наоборот выносят соотношение на шесть порядков. Границы шире
# бюджетных, потому что цель законно ставится заметно ниже факта — стратегии
# систематически недодерживают её (замер кабинета: цель 1200 → факт 1687).
TCPA_RATIO_MIN = 0.25
TCPA_RATIO_MAX = 4.0

# Границы списка минус-фраз — СВОИ, независимые от writer/negatives.py: рельса,
# считающая формулой построителя, пропустит любую его ошибку. Числа — из
# ограничений Директа (ref-v5/campaigns/update) и из политики рычага.
NEGATIVE_MAX_TOTAL_CHARS = 20_000
NEGATIVE_MAX_WORDS = 7
NEGATIVE_MAX_WORD_CHARS = 35
NEGATIVE_MAX_ADDED_PER_ACTION = 10
NEGATIVE_OPERATOR_CHARS = set('"!+[]<>|')

# Границы списка запрещённых площадок — свои, как и у минус-фраз. Числа из
# ref-v5/campaigns/update: 1000 элементов, 255 символов каждый.
PLACEMENT_MAX_SITES = 1000
PLACEMENT_MAX_SITE_CHARS = 255
PLACEMENT_MAX_ADDED_PER_ACTION = 10

# Границы почасового расписания — СВОИ, независимые от writer/schedule.py.
# Дублирование намеренное: рельса обязана считать сама, иначе она проверяет
# построитель его же формулой и пропустит любую его ошибку.
SCHEDULE_NUMBERS_PER_ROW = 25   # день недели + 24 часа
SCHEDULE_MIN = 10               # ноль запрещён отдельно: он выключает показы
SCHEDULE_MAX = 200
SCHEDULE_STEP = 10

# Путь ВОЗВРАТА — только set: он переписывает уже существующий объект в его
# прежнее значение. add на этом пути означал бы создание НОВОГО объекта вместо
# восстановления старого, то есть ещё одно изменение кабинета под видом отмены.
ROLLBACK_ALLOWED_ACTION_KINDS = {"bidmodifier.set", "schedule.set",
                                 "budget.set", "budget.set_daily",
                                 "campaign.suspend", "tcpa.set",
                                 "goal.set", "strategy.set", "negative.add",
                                 "placement.exclude"}

# Куда обязан возвращать откат, в зависимости от вида ИСХОДНОГО действия.
# Отмена добавления — нейтраль (объекта до действия не было), отмена
# перезаписи — прежний коэффициент из previous_state журнала.
ROLLBACK_ORIGIN_ADD = "bidmodifier.add"
ROLLBACK_ORIGIN_SET = "bidmodifier.set"

_DELETE_REASON = "удаление объектов запрещено: агент только паузит"


def _is_delete(kind_lower: str) -> bool:
    return "delete" in kind_lower or "remove" in kind_lower


def check_action(action: Dict[str, Any],
                 cost_28d_by_campaign: Optional[Dict[str, float]] = None,
                 ) -> Tuple[bool, str]:
    """Проверка одного действия. Возвращает (можно ли, причина отказа).

    cost_28d_by_campaign — независимый расход кампаний из витрины для рельсы
    бюджета (см. _check_budget). Без него рельса вынуждена верить числу
    построителя.
    """
    kind = str(action.get("action_kind") or "")
    kind_lower = kind.lower()

    # Отдельная явная проверка поверх allow-листа — не для защиты (её уже
    # даёт allow-лист), а чтобы в журнале была понятная причина отказа
    # именно "удаление", а не общая "вне allow-листа".
    if _is_delete(kind_lower) and kind not in REMOVAL_ALLOWED_ACTION_KINDS:
        return False, _DELETE_REASON

    if kind in BUILDER_ACTION_KINDS:
        return False, BUILDER_REASON

    if kind not in ALLOWED_ACTION_KINDS:
        return False, f"вид действия вне allow-листа: {kind}"

    percent = action.get("payload", {}).get("BidModifier")
    if percent is not None:
        if abs(int(percent)) > MODIFIER_CAP:
            return False, f"потолок корректировки ±{MODIFIER_CAP}%, получено {percent}%"

    if kind == "schedule.set":
        ok, reason = _check_schedule(action)
        if not ok:
            return False, reason

    if kind in ("budget.set", "budget.set_daily"):
        ok, reason = _check_budget(action, cost_28d_by_campaign)
        if not ok:
            return False, reason

    if kind == "tcpa.set":
        ok, reason = _check_tcpa(action)
        if not ok:
            return False, reason

    if kind == "strategy.set":
        ok, reason = _check_strategy(action, cost_28d_by_campaign)
        if not ok:
            return False, reason

    if kind == "negative.add":
        ok, reason = _check_negatives(action)
        if not ok:
            return False, reason

    if kind == "negative.remove_added":
        ok, reason = _check_remove_added(action)
        if not ok:
            return False, reason

    if kind == "placement.exclude":
        ok, reason = _check_placements(action)
        if not ok:
            return False, reason

    if kind == "campaign.suspend":
        ok, reason = _check_suspend(action)
        if not ok:
            return False, reason
    return True, ""


def _check_remove_added(action: Dict[str, Any]) -> Tuple[bool, str]:
    """Рельса снятия минус-фраз: снимаем ТОЛЬКО своё и ровно своё.

    Сторож удаления объектов этот вид пропускает по явному исключению
    (REMOVAL_ALLOWED_ACTION_KINDS), и освобождением от проверок это быть не
    должно: снять чужую минус-фразу значит вернуть трафик, который человек
    выключил сознательно, а в журнале это выглядит как аккуратный откат.

    Три проверки, и все считаются ЗДЕСЬ, по полям самого действия, а не
    формулой построителя:

      * снимаемое — подмножество добавленного агентом (AddedByAgent);
      * остаток совпадает с прежним списком минус снимаемое — иначе действие
        не «убирает своё», а переписывает список чем-то ещё;
      * список только сокращается: добавление фраз едет своим видом со своими
        лимитами Директа, и пролезть сюда мимо них оно не должно.
    """
    payload = action.get("payload") or {}
    if payload.get("CampaignId") is None:
        return False, "в действии снятия минус-фраз нет CampaignId"

    removed = [str(p) for p in (payload.get("RemovedPhrases") or ())]
    if not removed:
        return False, "снятие минус-фраз без списка снимаемого ничего не меняет"

    ours = {str(p) for p in (payload.get("AddedByAgent") or ())}
    stranger = sorted(set(removed) - ours)
    if stranger:
        return False, (f"снимаются фразы, которых агент не добавлял: "
                       f"{', '.join(stranger[:3])} — их выключил человек")

    previous = ((action.get("previous_state") or {}).get("NegativeKeywords")
                or {}).get("Items")
    if previous is None:
        return False, ("снятие построено без прежнего списка: проверить, что "
                       "убирается ровно своё, нечем")
    previous_set = {str(p) for p in previous}
    left = {str(p) for p in ((payload.get("NegativeKeywords") or {}).get("Items") or ())}

    if not left <= previous_set:
        return False, ("снятие добавляет фразы, которых в прежнем списке не "
                       "было: добавление едет своим видом")
    if left != previous_set - set(removed):
        return False, ("остаток не равен прежнему списку минус снимаемое: "
                       "действие переписывает список, а не убирает своё")
    return True, ""


def _check_suspend(action: Dict[str, Any]) -> Tuple[bool, str]:
    """Рельса выключения: пауза только из ПРОЧИТАННОГО состояния ON.

    previous_state.State — не формальность: откат выключения — это resume,
    и он вернёт кампанию в показы. Если бы действие строилось по кампании,
    которую человек уже остановил (SUSPENDED/OFF), откат «вернул» бы её в
    состояние, в котором она не была, — то есть сам стал бы правкой.
    Построитель (switch.diff_switch) это гарантирует; рельса проверяет
    независимо, по своему полю.
    """
    payload = action.get("payload") or {}
    if payload.get("CampaignId") is None:
        return False, "в действии выключения нет CampaignId"
    previous = action.get("previous_state") or {}
    if str(previous.get("State")) != "ON":
        return False, (f"выключение построено не из состояния ON "
                       f"(previous_state.State={previous.get('State')!r}): "
                       f"откат вернул бы кампанию не туда")
    return True, ""


# Конверсии рельсы бюджета — СВОИ, не импорт из writer/budget.py: рельса,
# считающая формулой построителя, пропустит любую его ошибку. Тот же довод,
# что у границ расписания выше.
_BUDGET_VAT = 1.2
_BUDGET_WEEKS = 4.0
_BUDGET_MICROS = 1_000_000


# Допустимое расхождение расхода построителя с витриной. Окна считаются по
# разным границам зрелости, поэтому точного равенства не бывает; расхождение
# сверх этой доли означает, что построитель считает расход не тем, чем витрина.
BUILDER_SPEND_TOLERANCE = 0.25


def _check_budget(action: Dict[str, Any],
                  cost_28d_by_campaign: Optional[Dict[str, float]] = None,
                  ) -> Tuple[bool, str]:
    """Рельса бюджета: новый лимит против прежнего РАСХОДА кампании.

    payload.Cost28dVat — расход 28 дней с НДС из той же строки budget_target,
    по которой построено действие. Лимит, пересчитанный в расход того же окна
    (×недели×НДС), обязан отличаться от прежнего расхода не более чем в
    коридор: портфель капит сдвиг ×0.5–1.5, а слом конверсии единиц (недели
    вместо дней — ×7, микрорубли вместо рублей — ×10⁶) выносит соотношение
    за края немедленно.

    cost_28d_by_campaign — НЕЗАВИСИМЫЙ расход из витрины. Без него рельса
    сверяет лимит с числом, которое в payload положил сам построитель:
    ошибись он окном или кампанией — рельса это одобрит, потому что проверяет
    его же арифметику его же данными (аудит 2026-08-23). Со справочником
    коридор считается по витрине, а расхождение самих чисел сверх
    BUILDER_SPEND_TOLERANCE — отдельный отказ: это разошедшиеся источники, а
    не решение.
    """
    payload = action.get("payload") or {}
    if str(action.get("action_kind")) == "budget.set":
        micros = payload.get("WeeklySpendLimit")
        per_window = _BUDGET_WEEKS
    else:
        micros = (payload.get("DailyBudget") or {}).get("Amount")
        per_window = 28.0
    cost_28d = payload.get("Cost28dVat")
    try:
        micros_v = int(micros)
        cost_v = float(cost_28d)
    except (TypeError, ValueError):
        return False, (f"поля рельсы бюджета нечитаемы: лимит {micros!r}, "
                       f"расход Cost28dVat {cost_28d!r}")
    if micros_v <= 0 or cost_v <= 0:
        return False, (f"лимит и расход обязаны быть положительными: "
                       f"лимит {micros_v}, расход {cost_v}")

    mart_cost = (cost_28d_by_campaign or {}).get(str(action.get("object_id")))
    if mart_cost is not None and float(mart_cost) > 0:
        mart_v = float(mart_cost)
        drift = abs(cost_v - mart_v) / mart_v
        if drift > BUILDER_SPEND_TOLERANCE:
            return False, (f"расход построителя {cost_v:.0f} ₽ расходится с "
                           f"витриной {mart_v:.0f} ₽ на {drift:.0%} — источники "
                           f"разошлись, решение считать не по чему")
        cost_v = mart_v

    implied_28d = micros_v / _BUDGET_MICROS * per_window * _BUDGET_VAT
    ratio = implied_28d / cost_v
    if not (BUDGET_RATIO_MIN <= ratio <= BUDGET_RATIO_MAX):
        return False, (f"целевой бюджет ×{ratio:.2f} от прежнего расхода — вне "
                       f"коридора {BUDGET_RATIO_MIN}–{BUDGET_RATIO_MAX}: похоже "
                       f"на слом конверсии единиц, а не на решение")
    return True, ""


def _check_placements(action: Dict[str, Any]) -> Tuple[bool, str]:
    """Рельса запрета площадок: список целиком и добавляемое этим действием.

    Считает независимо от построителя (writer/placements.py) — та же причина,
    что у минус-фраз: рельса, доверяющая тому, кого проверяет, не рельса. И
    цена ошибки та же: запрещённую площадку трафик обходит навсегда.
    """
    payload = action.get("payload") or {}
    items = ((payload.get("ExcludedSites") or {}).get("Items"))
    if not isinstance(items, list) or not items:
        return False, "пустой список запрещённых площадок: действие без содержания"

    added = payload.get("AddedSites")
    added_list = added if isinstance(added, list) else items
    if len(added_list) > PLACEMENT_MAX_ADDED_PER_ACTION:
        return False, (f"за одно действие запрещается {len(added_list)} площадок "
                       f"при пределе {PLACEMENT_MAX_ADDED_PER_ACTION}: "
                       f"отсечённый трафик не вернуть")
    if len(items) > PLACEMENT_MAX_SITES:
        return False, (f"в списке {len(items)} площадок при пределе "
                       f"{PLACEMENT_MAX_SITES}: Директ отклонит весь запрос")
    for item in items:
        site = str(item or "").strip()
        if not site:
            return False, "пустое имя площадки в списке"
        if len(site) > PLACEMENT_MAX_SITE_CHARS:
            return False, (f"имя площадки длиннее {PLACEMENT_MAX_SITE_CHARS} "
                           f"символов: {site[:40]}…")
        if " " in site:
            return False, (f"в имени площадки есть пробел: {site[:40]!r} — "
                           f"это не домен и не идентификатор приложения")
    return True, ""


def _check_negatives(action: Dict[str, Any]) -> Tuple[bool, str]:
    """Рельса минус-фраз: список целиком и то, что добавляется этим действием.

    Цена ошибки здесь — не «ставка выше», а «показов по фразе нет вовсе», и
    вернуть отсечённый трафик задним числом нельзя. Поэтому проверяется и
    форма (операторы, длина слов и фраз, суммарный бюджет символов кабинета),
    и ОБЪЁМ добавляемого за одно действие: даже если план сорвётся, за такт
    не уйдёт больше горсти фраз.
    """
    payload = action.get("payload") or {}
    items = ((payload.get("NegativeKeywords") or {}).get("Items"))
    if not isinstance(items, list) or not items:
        return False, "пустой список минус-фраз: действие без содержания"

    added = payload.get("AddedPhrases")
    added_list = added if isinstance(added, list) else items
    if len(added_list) > NEGATIVE_MAX_ADDED_PER_ACTION:
        return False, (f"за одно действие добавляется {len(added_list)} фраз при "
                       f"пределе {NEGATIVE_MAX_ADDED_PER_ACTION}: отсечённый "
                       f"трафик не вернуть, такт обязан оставаться различимым")

    total = 0
    for item in items:
        phrase = str(item or "").strip()
        if not phrase:
            return False, "пустая минус-фраза в списке"
        if NEGATIVE_OPERATOR_CHARS & set(phrase):
            return False, (f"во фразе {phrase[:40]!r} есть операторы языка "
                           f"запросов — автоматический рычаг такие не ставит")
        words = phrase.split()
        if len(words) > NEGATIVE_MAX_WORDS:
            return False, (f"во фразе {phrase[:40]!r} {len(words)} слов при "
                           f"пределе {NEGATIVE_MAX_WORDS}")
        for word in words:
            if len(word) > NEGATIVE_MAX_WORD_CHARS:
                return False, (f"слово длиннее {NEGATIVE_MAX_WORD_CHARS} "
                               f"символов: {word[:40]!r}")
        total += len(phrase)
    if total > NEGATIVE_MAX_TOTAL_CHARS:
        return False, (f"суммарная длина списка {total} символов при пределе "
                       f"{NEGATIVE_MAX_TOTAL_CHARS}: Директ отклонит весь запрос")
    return True, ""


def _check_tcpa(action: Dict[str, Any]) -> Tuple[bool, str]:
    """Рельса целевого CPA: новая цель против ФАКТИЧЕСКОГО CPA окна.

    Считает независимо от построителя (writer/tcpa.py) — рельса, доверяющая
    тому, кого проверяет, не рельса. Фактический CPA приходит в payload из
    той же строки расчёта, что и цель; без него сравнивать не с чем, и это
    отказ, а не молчаливый пропуск.
    """
    payload = action.get("payload") or {}
    micros = payload.get("TargetCpa")
    fact = payload.get("CpaFact")
    try:
        micros_v = int(micros)
        fact_v = float(fact)
    except (TypeError, ValueError):
        return False, (f"поля рельсы цели CPA нечитаемы: цель {micros!r}, "
                       f"фактический CPA {fact!r}")
    if micros_v <= 0 or fact_v <= 0:
        return False, (f"цель и фактический CPA обязаны быть положительными: "
                       f"цель {micros_v}, факт {fact_v}")
    ratio = micros_v / _BUDGET_MICROS / fact_v
    if not (TCPA_RATIO_MIN <= ratio <= TCPA_RATIO_MAX):
        return False, (f"новая цель ×{ratio:.2f} от фактического CPA — вне "
                       f"коридора {TCPA_RATIO_MIN}–{TCPA_RATIO_MAX}: похоже "
                       f"на слом конверсии единиц, а не на решение")
    return True, ""


def _check_strategy(action: Dict[str, Any],
                    cost_28d_by_campaign: Optional[Dict[str, float]] = None,
                    ) -> Tuple[bool, str]:
    """Рельса смены стратегии: форма тела и деньги, которые она уносит с собой.

    Проверяется не «правильно ли решение» (этого рельса не знает), а три вещи,
    которыми смена стратегии отличается от прочих правок:

      * ТЕЛО СОГЛАСОВАНО С САМИМ СОБОЙ. Тип в payload и тип, записанный в блок,
        обязаны совпадать: разойдись они — в кабинет уедет одно, а в журнал
        ляжет другое, и откат вернёт не туда.
      * ФОРМА ИЗВЕСТНА. Имя подблока параметров выводится из типа стратегии;
        тип вне справочника форм означает тело, собранное по догадке.
      * ОГРАНИЧИТЕЛЬ РАСХОДА НА МЕСТЕ. Конверсионная стратегия держит деньги
        внутри себя (WeeklySpendLimit), и переход без лимита оставляет кампанию
        без ограничения вовсе. Сам лимит сверяется с НЕЗАВИСИМЫМ расходом
        витрины по тому же коридору, что у бюджета: слом единиц (рубли вместо
        микрорублей) выносит соотношение за края немедленно.

    Справочник форм берётся у рычага (writer/strategy.STRATEGY_FORMS) — это
    описание API, а не решение построителя, и вторая его копия здесь
    разъехалась бы с первой. Импорт ленивый: writer/strategy тянет ожидание и
    полосы, и модульный импорт замкнул бы кольцо на полуготовом пакете.
    """
    from sync.agent.writer.strategy import STRATEGY_FORMS

    payload = action.get("payload") or {}
    block = payload.get("BiddingStrategy")
    if not isinstance(block, dict) or not isinstance(block.get("Search"), dict):
        return False, "в теле смены стратегии нет блока BiddingStrategy"

    declared = str(payload.get("BiddingStrategyType") or "")
    written = str((block["Search"] or {}).get("BiddingStrategyType") or "")
    if declared != written:
        return False, (f"тип в теле ({written or '—'}) не совпадает с типом "
                       f"действия ({declared or '—'}): в кабинет уедет одно, "
                       f"а в журнал ляжет другое")

    form = STRATEGY_FORMS.get(written)
    if form is None:
        return False, f"тип стратегии {written or '—'} вне справочника форм"

    sub_name = form.get("block")
    params = (block["Search"] or {}).get(sub_name) if sub_name else None
    if sub_name and not isinstance(params, dict):
        return False, (f"у стратегии {written} нет подблока параметров "
                       f"{sub_name}: тело неполное")
    if not sub_name:
        leftovers = [k for k in block["Search"] if k != "BiddingStrategyType"]
        if leftovers:
            return False, (f"в теле стратегии {written} остались поля прежней "
                           f"стратегии: {', '.join(sorted(leftovers))}")
        return True, ""

    limit = params.get("WeeklySpendLimit")
    target = params.get("AverageCpa")
    try:
        limit_v = int(limit)
        target_v = int(target)
    except (TypeError, ValueError):
        return False, (f"поля рельсы стратегии нечитаемы: лимит {limit!r}, "
                       f"цель CPA {target!r}")
    if limit_v <= 0 or target_v <= 0:
        return False, (f"лимит и цель CPA обязаны быть положительными: "
                       f"лимит {limit_v}, цель {target_v}")
    if target_v > limit_v:
        return False, (f"цель CPA {target_v / _BUDGET_MICROS:.0f} ₽ больше "
                       f"недельного лимита {limit_v / _BUDGET_MICROS:.0f} ₽: "
                       f"похоже на слом единиц, а не на решение")

    mart_cost = (cost_28d_by_campaign or {}).get(str(action.get("object_id")))
    if mart_cost is not None and float(mart_cost) > 0:
        implied_28d = limit_v / _BUDGET_MICROS * _BUDGET_WEEKS * _BUDGET_VAT
        ratio = implied_28d / float(mart_cost)
        if not (BUDGET_RATIO_MIN <= ratio <= BUDGET_RATIO_MAX):
            return False, (f"лимит новой стратегии ×{ratio:.2f} от прежнего "
                           f"расхода — вне коридора {BUDGET_RATIO_MIN}–"
                           f"{BUDGET_RATIO_MAX}: похоже на слом конверсии "
                           f"единиц, а не на решение")
    return True, ""


def _check_schedule(action: Dict[str, Any]) -> Tuple[bool, str]:
    """Рельса расписания: цена ошибки здесь выше, чем у корректировки сегмента.

    Расписание правит кампанию ЦЕЛИКОМ, и ноль в нём — это не «ставка ниже», а
    «показов в этот час нет». Ошибка в построении обернулась бы не потерей
    эффективности, а выключенным трафиком, поэтому проверка стоит отдельно от
    построителя (writer/schedule.py) и считает независимо от него: рельса,
    доверяющая тому, кого проверяет, не рельса.
    """
    items = (((action.get("payload") or {}).get("TimeTargeting") or {})
             .get("Schedule") or {}).get("Items")
    if not items:
        return False, "расписание без часов: пустой Schedule.Items"

    for item in items:
        parts = [p.strip() for p in str(item).split(",")]
        if len(parts) != SCHEDULE_NUMBERS_PER_ROW:
            return False, (f"строка расписания обязана нести день недели и 24 часа, "
                           f"получено полей: {len(parts)}")
        try:
            numbers = [int(p) for p in parts]
        except ValueError:
            return False, f"нечисловое значение в расписании: {item!r}"

        day, hours = numbers[0], numbers[1:]
        if not 1 <= day <= 7:
            return False, f"день недели вне 1..7: {day}"
        for hour_value in hours:
            if hour_value == 0:
                return False, ("ноль в расписании выключает показы в этот час — "
                               "остановку трафика агент не назначает")
            if not SCHEDULE_MIN <= hour_value <= SCHEDULE_MAX:
                return False, (f"коэффициент расписания вне {SCHEDULE_MIN}..{SCHEDULE_MAX}: "
                               f"{hour_value}")
            if hour_value % SCHEDULE_STEP:
                return False, (f"коэффициент расписания обязан быть кратен "
                               f"{SCHEDULE_STEP}: {hour_value}")
    return True, ""


def expected_rollback_coefficient(
    origin_action_kind: Any, previous_state: Any
) -> Tuple[Optional[int], str]:
    """Коэффициент, в который откат ОБЯЗАН вернуть объект, — из журнала.

    Выводится здесь, а не принимается готовым от вызывающего кода: смысл
    сверки в том, чтобы рельса считала ожидание НЕЗАВИСИМО от построителя
    запроса (rollback.rollback_payload). Получи она ожидание из того же
    источника, что и сам запрос, — сверяла бы код сам с собой.

    Отмена добавления возвращает нейтраль (объекта до действия не было),
    отмена перезаписи — прежний коэффициент. previous_state хранится в
    ДЕЛЬТАХ, как весь внутренний план, поэтому переводится в 100-базу API.

    None вместо числа — «ожидание не выводится»: вид исходного действия
    неизвестен или прошлое состояние нечитаемо. Это отказ, а не исключение:
    вызывающий код превращает исключение в пометку «неоткатываемо навсегда».
    """
    origin = str(origin_action_kind or "")
    previous = previous_state if isinstance(previous_state, dict) else {}

    if origin == ROLLBACK_ORIGIN_ADD:
        return API_NEUTRAL, ""

    if origin == ROLLBACK_ORIGIN_SET:
        percent = previous.get("percent")
        if percent is None:
            return None, "в журнале нет прошлого коэффициента (previous_state.percent)"
        try:
            return API_NEUTRAL + int(percent), ""
        except (TypeError, ValueError):
            return None, f"прошлый коэффициент нечитаем: {percent!r}"

    return None, f"вид исходного действия неизвестен рельсе: {origin or '—'}"


def check_rollback(request: Dict[str, Any]) -> Tuple[bool, str]:
    """Рельса пути ВОЗВРАТА. Проверяет вид действия и диапазон API, но не потолок.

    Потолок MODIFIER_CAP описывает, что агенту позволено НАЗНАЧАТЬ: коридор
    ±50 % — это ограничение на его собственные решения. Куда агенту позволено
    ВЕРНУТЬСЯ, эта величина не описывает вообще: прошлое значение поставил
    человек, оно уже действовало в кабинете и штатно для Директа (+80 % на
    кампании или 0 — «показы на устройстве выключены» — обычные настройки).
    Пропущенное через потолок назначения, такое возвращаемое значение
    отклонялось бы, действие помечалось неоткатываемым навсегда, и изменение
    агента оставалось бы в кабинете вечно — то есть рельса, поставленная для
    защиты, сама отменяла бы третий слой защиты.

    Что на пути возврата остаётся жёстким:
      * удаление запрещено так же, как везде;
      * allow-лист уже пути назначения (только set: возврат переписывает
        существующий объект, а не создаёт новый);
      * коэффициент обязан лежать в диапазоне API Директа — вне его запрос
        либо будет отклонён поэлементно, либо применит не то, что задумано;
      * коэффициент обязан СОВПАДАТЬ с прошлым состоянием из журнала (для
        отмены добавления — с нейтралью). Без этой сверки рельса проверяла бы
        только форму: любое значение внутри диапазона API — а это 0..1300, то
        есть почти что угодно, — проходило бы насквозь, и единственным, что
        связывает запрос с реальным прошлым значением, оставался бы сам код,
        который этот запрос строит. Ошибка в нём (предельное значение вместо
        прежнего, чужой previous_state, потерянный знак дельты) шла бы прямо в
        боевой кабинет: путь возврата больше ничем не проверен.

    Коэффициент здесь — в 100-БАЗНОЙ ШКАЛЕ API (api_coefficient), а не в
    дельтах, как у check_action. Поле названо иначе намеренно: две рельсы
    считают в разных единицах, и одноимённое поле рано или поздно передали бы
    не в ту функцию молча.

    Отказ всегда возвращается парой (False, причина) и никогда не летит
    исключением: вызывающий код (agent_e1_watchdog.rollback_one) трактует
    исключение на этом участке как «запрос не строится» и хоронит действие
    пометкой permanent=True — то есть падение рельсы стоило бы дороже отказа.
    """
    kind = str(request.get("action_kind") or "")
    if _is_delete(kind.lower()):
        return False, _DELETE_REASON

    if kind not in ROLLBACK_ALLOWED_ACTION_KINDS:
        return False, f"вид действия вне allow-листа возврата: {kind}"

    if kind in ("schedule.set", "budget.set", "budget.set_daily"):
        # У расписания и бюджета возврат не описывается одним коэффициентом:
        # назад едет весь прежний блок (TimeTargeting / BiddingStrategy /
        # DailyBudget). Сверять его с прошлым состоянием здесь незачем —
        # строитель возврата (rollback_payload) берёт блок прямо из журнала
        # и не собирает его заново, поэтому «вернуть не туда» тут невозможно
        # по построению. Требовать api_coefficient — значит запретить такой
        # откат вовсе.
        return True, ""

    if kind == "campaign.suspend":
        # Возврат выключения — campaigns.resume: он бинарен и не несёт
        # значения, в которое можно вернуть «не туда». Что кампания была
        # включена (previous_state.State == ON) — гарантия рельсы назначения
        # (_check_suspend), без неё действие не применялось.
        return True, ""

    coefficient = request.get("api_coefficient")
    if coefficient is None:
        return False, "коэффициент возврата не задан"
    try:
        value = int(coefficient)
    except (TypeError, ValueError):
        return False, f"коэффициент возврата не число: {coefficient!r}"
    if not (API_MIN <= value <= API_MAX):
        return False, (f"коэффициент возврата {value} вне диапазона Директа "
                       f"{API_MIN}..{API_MAX}")

    expected, why = expected_rollback_coefficient(
        request.get("origin_action_kind"), request.get("previous_state"))
    if expected is None:
        return False, f"куда возвращать — неизвестно: {why}"
    if value != expected:
        return False, (f"коэффициент возврата {value} не равен прошлому состоянию "
                       f"журнала {expected}: это не отмена, а новое изменение")
    return True, ""


def check_holdout(
    actions: List[Dict[str, Any]], holdout_ids: Set[Any]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Разделяет действия на разрешённые и заблокированные заповедником."""
    holdout_ids = {str(h) for h in holdout_ids}
    allowed = [a for a in actions if str(a.get("object_id")) not in holdout_ids]
    blocked = [a for a in actions if str(a.get("object_id")) in holdout_ids]
    return allowed, blocked
