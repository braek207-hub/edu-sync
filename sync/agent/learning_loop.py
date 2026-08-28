# -*- coding: utf-8 -*-
"""
sync/agent/learning_loop.py — обучение на СВОИХ закрытых действиях.

Косвенная петля уже работает: применённое изменение меняет расход, детектор
скачков (mining.detect_change_points) видит его в фактах, DiD даёт
эластичность, она входит в кривые следующего такта. Здесь замыкается прямая
петля — по журналу действий:

  • track_record — послужной список рычага: сколько закрытых наблюдений
    улучшили метрику, сколько ухудшили, и подтвердили ли это деньги на
    втором чекпоинте (agent_e1_watchdog.money_checkpoint). Это же фундамент
    лестницы автономии (Ф9): класс действий с доказанной долей попаданий
    получает больше свободы, недоказанный остаётся в тени.

  • forecast_bias — систематическое смещение прогноза: медиана отношения
    «факт / ожидание». Медиана, а не среднее: одно действие с ожиданием
    близким к нулю даёт отношение в сотни и утаскивает среднее. Усадка к
    1.0 по объёму наблюдений — тот же приём, что эмпирический Байес в
    history.combine: три наблюдения не повод верить, что модель завышает
    вдвое.

Два правила, без которых оба числа врут в одну сторону:

  1. Ключ калибровки — не вид действия, а вид ПЛЮС направление
     (`budget.set:up` / `budget.set:down`). Модель может завышать эффект
     доливки и занижать эффект срезания; одно усреднённое число по
     `budget.set` спрятало бы обе ошибки друг за другом.
  2. Доля попаданий печатается раздельно для растящих и сокращающих
     действий. Мера исхода в журнале судит по цене конверсии, а срезав
     объём, кампания почти всегда дешевеет: общий hit_rate систематически
     хвалит резаков. Сравнивать направления между собой нельзя даже после
     поправки — у них разные обещания (см. agent_e1_watchdog.economic_outcome).

Модуль ничего не решает и никуда не пишет: считает числа по уже прочитанному
журналу. Потребители — отчёт Э0 и (следующим шагом) поправка ожиданий в
портфеле.
"""

import statistics
from typing import Any, Dict, List, Optional

# Длина свежего окна — у ПОТРЕБИТЕЛЯ (лестница автономии): это её правило
# «падение мгновенное, подъём медленный», и число обязано быть одно на оба
# конца. Импорт безопасен: autonomy ничего не импортирует из расчётного слоя.
from sync.agent.autonomy import RECENT_WINDOW

# Сила приора усадки смещения: при n наблюдениях вес факта n/(n+BIAS_PRIOR_N).
# Десять — примерно такт-два боевой работы движка записи: до этого поправку
# применять рано, и усадка держит множитель около единицы сама собой.
BIAS_PRIOR_N = 10

# Минимальное ожидание по модулю, при котором отношение «факт / ожидание»
# вообще осмысленно. Меньше одного лида — деление на почти ноль: отношение
# улетает в сотни, и медиана поплывёт, если таких действий наберётся половина.
MIN_EXPECTED_LEADS = 1.0

SUCCESS = "improved"
FAILURE = "worsened"
# Третий закрытый исход шкалы журнала (writer/rollback.outcome_verdict):
# эффект меньше собственной ошибки. Наблюдение закрыто, но попаданием не
# считается. В плане это поле называлось "unchanged" — в коде такого исхода
# не существует, и слот назван по тому, что реально лежит в колонке.
NEUTRAL = "inconclusive"

CLOSED_VERDICTS = (SUCCESS, FAILURE, NEUTRAL)

UP = "up"
DOWN = "down"


def _verdict(action: Dict[str, Any]) -> str:
    """Исход наблюдения, как бы строка ни пришла.

    В БД колонка называется observation_verdict; closing_verdict — функция
    сторожа, которая её заполняет, и одноимённый алиас в выборке
    (writer_db.closed_actions). Читаются оба имени: запрос, написанный по
    имени функции, вернул бы пустоту молча, и петля обучения тихо перестала
    бы учиться — отказ, который ничем себя не проявляет.
    """
    return str(action.get("closing_verdict")
               or action.get("observation_verdict") or "")


def _direction(action: Dict[str, Any]) -> Optional[str]:
    """Направление действия по знаку ожидания: растим или сокращаем.

    None — ожидания в строке нет (рычаг его не несёт или действие спланировано
    до появления пары «прогноз / факт»). Такие в разбивку по направлению не
    идут: приписать им сторону значило бы выдумать намерение.
    """
    expected = action.get("expected_leads_delta")
    if expected is None:
        return None
    try:
        value = float(expected)
    except (TypeError, ValueError):
        return None
    if value == 0.0:
        return None
    return UP if value > 0 else DOWN


def _rate(hits: int, closed: int) -> Optional[float]:
    """Доля попаданий. None при пустом знаменателе — ноль соврал бы про провал."""
    return round(hits / closed, 4) if closed else None


def track_record(actions: List[Dict[str, Any]],
                 recent_window: int = RECENT_WINDOW) -> Dict[str, Dict[str, Any]]:
    """Доля попаданий по видам действий среди ЗАКРЫТЫХ наблюдений.

    Кроме накопленных счётчиков считается СВЕЖЕЕ окно — последние
    recent_window закрытых наблюдений вида (recent_closed / recent_improved).
    Накопленная доля падает медленно: у рычага с сотней наблюдений две плохие
    недели не сдвинут её и на процент, а лестница обязана уронить ступень В
    ТОМ ЖЕ ТАКТЕ, что и провал. Разделять эти два счёта — не удобство отчёта:
    на одном числе «упал только что» и «был плох всегда» неразличимы.

    «Последние» — по порядку строк на входе, а он задан выборкой журнала
    (writer/db.CLOSED_ACTIONS_SQL: ORDER BY applied_at). Сортировать здесь
    заново нечем: у строки, пришедшей не из журнала, отметки времени может
    не быть вовсе, и молчаливая пересортировка по отсутствующему полю
    перемешала бы окно, вместо того чтобы упасть.
    """
    out: Dict[str, Dict[str, Any]] = {}
    verdicts: Dict[str, List[str]] = {}
    for action in actions:
        verdict = _verdict(action)
        if verdict not in CLOSED_VERDICTS:
            continue  # 'unknown' и незакрытые — не свидетельство ни за, ни против
        kind = str(action.get("action_kind") or "")
        slot = out.setdefault(kind, {
            "closed": 0, SUCCESS: 0, FAILURE: 0, NEUTRAL: 0,
            "money_confirmed": 0, "money_contradicted": 0,
            "closed_up": 0, "hits_up": 0, "closed_down": 0, "hits_down": 0,
        })
        slot["closed"] += 1
        slot[verdict] += 1
        verdicts.setdefault(kind, []).append(verdict)
        direction = _direction(action)
        if direction is not None:
            slot[f"closed_{direction}"] += 1
            if verdict == SUCCESS:
                slot[f"hits_{direction}"] += 1
        money = str(action.get("money_verdict") or "")
        if verdict == SUCCESS and money == SUCCESS:
            slot["money_confirmed"] += 1
        elif verdict == SUCCESS and money == FAILURE:
            slot["money_contradicted"] += 1

    for kind, slot in out.items():
        window = verdicts.get(kind, [])[-int(recent_window):] if recent_window > 0 else []
        slot["recent_closed"] = len(window)
        slot["recent_improved"] = sum(1 for v in window if v == SUCCESS)
        slot["hit_rate"] = _rate(slot[SUCCESS], slot["closed"]) or 0.0
        # Раздельно — и без общего знаменателя: доливка и срезание отвечают
        # на разные обещания, и складывать их попадания в одну дробь значит
        # снова спрятать перекос меры.
        slot["hit_rate_up"] = _rate(slot.pop("hits_up"), slot["closed_up"])
        slot["hit_rate_down"] = _rate(slot.pop("hits_down"), slot["closed_down"])
    return out


def forecast_bias(actions: List[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """Систематическое смещение прогноза по видам действий и направлениям."""
    ratios: Dict[str, List[float]] = {}
    for action in actions:
        expected = action.get("expected_leads_delta")
        observed = action.get("observed_leads_delta")
        direction = _direction(action)
        # observed=None — «не измерено» (у действия не было темпа базы), а не
        # «эффекта не было»: колонка отличает эти случаи, и петля обязана тоже.
        if observed is None or direction is None:
            continue
        try:
            expected_value = float(expected)
            ratio = float(observed) / expected_value
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if abs(expected_value) < MIN_EXPECTED_LEADS:
            continue
        key = f"{str(action.get('action_kind') or '')}:{direction}"
        ratios.setdefault(key, []).append(ratio)

    out: Dict[str, Dict[str, float]] = {}
    for key, values in ratios.items():
        median = statistics.median(values)
        n = len(values)
        out[key] = {
            "ratio": round(median, 4),
            "n": n,
            "shrunk_ratio": round((median * n + 1.0 * BIAS_PRIOR_N) / (n + BIAS_PRIOR_N), 4),
        }
    return out
