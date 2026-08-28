# -*- coding: utf-8 -*-
"""
sync/agent_e1_watchdog.py — сторож красных линий: наблюдение и автооткат.

Третий слой защиты движка записи. Первые два (рельсы guardrails.py и
риск-бюджет risk.py) не пускают заведомо недопустимое; этот слой отвечает за
то, чего заранее не знает никто, — за результат. Весь дизайн автономного
агента построен на замене человеческого апрува автооткатом: не «человек
предотвращает ошибку заранее», а «система исправляет её за час». До появления
этого прогона механика отката (rollback.py) существовала только для тестов и
не вызывалась из рабочего кода ни разу — то есть третьего слоя в системе не
было, хотя всё вокруг написано так, будто он есть.

Цикл прогона:
    гейт витрины фактов → открытые действия журнала → окно наблюдения каждого →
    факты за окно → пробита ли красная линия (с подтверждением) → рельсы пути
    возврата → откат → отчёт

Окно наблюдения — не «всё время с момента применения»:
  * день применения не считается (OBSERVATION_LAG_DAYS): изменение работало
    неполные сутки, и этот день смешивает «до» и «после»;
  * верхняя граница отодвинута от сегодня на OBSERVATION_TAIL_DAYS: сутки за
    неполный сегодняшний день плюс запас под лаг источника лидов (см.
    LEADS_LAG_DAYS) — расход и эффективные лиды приходят из разных источников
    с разной задержкой, и лид-неполный день завышает наблюдаемый CPA всегда в
    одну сторону;
  * длина ограничена горизонтом (OBSERVATION_HORIZON_DAYS): красная линия
    судит эффект изменения, а не общий дрейф кампании за квартал. Без
    горизонта наблюдение месячной давности сравнивало бы базовый CPA с
    накопленным средним, где влияния самого изменения почти не осталось.

По умолчанию ПЕСОЧНИЦА и DRY-RUN. Откат — это тоже запись в кабинет, поэтому
он требует тех же двух явных флагов, что и прямое применение. Комбинация
«песочница + запись» ЗАПРЕЩЕНА (см. SANDBOX_APPLY_REFUSAL): она выглядит
безопасной репетицией, а на деле разоружает боевые красные линии.

Запуск:
    python -m sync.agent_e1_watchdog                 # песочница, репетиция
    python -m sync.agent_e1_watchdog --prod          # боевой кабинет, репетиция
    python -m sync.agent_e1_watchdog --prod --apply  # боевой откат
ENV: DATABASE_URL, DIRECT_TOKEN
"""

import hashlib
import json
import math
from statistics import median
import sys
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sync.agent import blackbox
from sync.agent import experiments
from sync.agent import db as agent_db
from sync.agent import gate as gate_module
from sync.agent import holdout
from sync.agent import tact_effect
from sync.agent.gate import mart_gate, with_source_checks
from sync.agent.writer import db as writer_db
from sync.agent.writer.apply import _element_errors
# journal_allowed — общее правило движка (writer/client.py), а не местное
# решение сторожа: журнал ОДИН на оба окружения, и тем же правилом
# пользуется прогон применения. Для сторожа это второй рубеж — комбинация
# «песочница + запись» отклоняется ещё в main() до единого обращения к БД;
# здесь — защита от вызывающего кода, который соберёт такой клиент
# программно (живой тест, разовый скрипт).
from sync.agent.writer.client import WriteClient, journal_allowed
from sync.agent.writer.budget import BUDGET_DAILY_KIND, BUDGET_KIND
from sync.agent.writer.guardrails import check_rollback
from sync.agent.writer.tcpa import TCPA_KIND
from sync.agent.writer.rollback import (rollback_payload, is_breached,
                                        is_spend_collapsed, outcome_verdict)

# Чтение корректировок кампании берётся у прогона применения, а не пишется
# заново: форма запроса выверена пробой (Levels внутри SelectionCriteria), а
# нормализация ключей (_normalize_actual внутри) — единственное место, где
# определено, как ключ сегмента из API сходится с ключом плана. Вторая копия
# этой логики разошлась бы с первой на первом же изменении справочника типов.
from sync.agent_e1 import _actual_modifiers as read_actual_modifiers

OBSERVATION_LAG_DAYS = 1        # день применения в наблюдение не входит

# Запас верхней границы под лаг ИСТОЧНИКА ЛИДОВ. Расход и эффективные лиды
# приходят в витрину фактов из разных источников: расход за вчера уже полон,
# а лиды по вчерашним визитам ещё дозревают в CRM. Смещение от этого не
# случайное, а систематическое и всегда в одну сторону — наблюдаемый CPA
# завышается, а порог пробоя (+40 % к базе) перешагивается недосчётом лидов
# на треть. Минимум наблюдений от этого не защищает: это порог по ОБЪЁМУ, а
# не по ПОЛНОТЕ, и лид-неполный день проходит его наравне с полным.
#
# Двое суток — заведомый запас, а не измеренная величина: точный лаг CRM здесь
# не замерен, и число обязано быть пересчитано по расхождению eff_leads одного
# и того же дня между соседними прогонами Э0. До замера действует правило
# «лучше рассудить на день позже, чем откатить здоровое изменение».
LEADS_LAG_DAYS = 2
OBSERVATION_TAIL_DAYS = 1 + LEADS_LAG_DAYS
OBSERVATION_HORIZON_DAYS = 14   # длина окна наблюдения, дней

# Доля дней окна, за которые ВИТРИНА обязана быть наполнена (хоть по одной
# кампании кабинета — см. mart_filled_days). Вердикт по окну, наполненному
# наполовину, — это вердикт по случайной выборке дней: прогон расчёта
# (agent_e0) идёт по ручному запуску и расписания не имеет, поэтому дыры в
# витрине — штатное состояние, а не авария. Считать эту долю по дням САМОЙ
# кампании нельзя: см. mart_filled_days.
MIN_WINDOW_COVERAGE = 0.8

# Пороги гейта витрины — общие для всех пишущих прогонов: sync/agent/gate.py
# (там же — почему ширина дня и медианный эталон).
GATE_MIN_BREADTH = gate_module.GATE_MIN_BREADTH
FACTS_MAX_AGE_DAYS = gate_module.FACTS_MAX_AGE_DAYS

PREVIEW_SAMPLE_LIMIT = 5

# Причины, по которым откат невозможен. Повтор их не исправит: Id корректировки
# не появится сам, рельсы отклонят тот же запрос так же. Такие действия
# помечаются в журнале неоткатываемыми (writer_db.mark_rollback_failed с
# permanent=True) и снимаются с наблюдения — но не исчезают: изменение
# остаётся живым, риск-бюджет продолжает его оплачивать, счётчик
# failed_rollbacks_count показывает его в каждом отчёте.
NO_ID_REASON = (
    "неизвестен Id корректировки: откат вслепую невозможен "
    "(Id приходит только в ответе bidmodifier.add и не найден в кабинете)"
)
INCOMPLETE_REASON = "тело запроса на возврат неполное: нет Id или коэффициента"
HOLDOUT_REASON = (
    "кампания в заповеднике: сторож её не трогает — заповедник и есть база "
    "сравнения для всех замеров"
)
NO_RED_LINE_REASON = (
    "у действия нет красной линии — судить не по чему, нужна ручная сверка"
)
EXPIRED_REASON = (
    "горизонт наблюдения закрыт, вердикт так и не вынесен: автоматически он не "
    "появится никогда — нужна ручная сверка"
)
JOURNAL_CONFLICT_REASON = (
    "возврат в кабинет прошёл, но строку журнала уже закрыл другой контур: "
    "его отметка описывает исход точнее и не перезаписывается — сверять руками"
)
DATA_GATE_REASON = (
    "гейт витрины фактов красный: наблюдение показано, но откат не выполняется "
    "— на битых данных сторож бьёт по здоровым кампаниям"
)
GATE_DEFERS_CLOSING_REASON = (
    "гейт витрины фактов красный: строка БЕЗ вердикта не закрывается — "
    "«вердикта не будет никогда» на мёртвой витрине означает лишь «данных не "
    "было», и перманентная пометка похоронила бы действие по той же причине, "
    "что и запрещённый прогон в песочнице. Ждём зелёного гейта"
)
SANDBOX_APPLY_REFUSAL = (
    "запрещённая комбинация «песочница + запись»: боевые логины в песочнице "
    "недоступны, каждый откат там падает ошибкой «логин не подключен», а "
    "неудача пишется в БОЕВОЙ журнал (база одна). Три таких «безопасных» "
    "прогона снимают действие с наблюдения навсегда. Нужен реальный откат — "
    "--prod --apply; нужна репетиция — без --apply"
)

# Состояния наблюдения. Разделены, потому что за ними стоят разные решения:
#   waiting      — окно ещё не открылось, данных быть не может;
#   collecting   — окно открыто, наблюдений меньше минимума: молчим намеренно,
#                  иначе шум примут за провал и откатят здоровое изменение;
#   low_coverage — окно открыто, но витрина по нему не наполнена или дырявая:
#                  судить по случайной выборке дней нельзя;
#   unconfirmed  — линия пробита, но держится только на самом дорогом дне
#                  окна: это выброс, а не деградация (breach_survives_peak_day);
#   expired      — горизонт закрыт, а вердикта так и нет: автоматически он не
#                  появится никогда, строка уходит на ручной разбор;
#   watched      — наблюдений достаточно, линия не пробита;
#   breached     — линия пробита и подтверждена, действие откатывается;
#   no_red_line  — линии нет вовсе, судить не по чему: на ручной разбор.
STATE_WAITING = "waiting"
STATE_COLLECTING = "collecting"
STATE_LOW_COVERAGE = "low_coverage"
STATE_UNCONFIRMED = "unconfirmed"
STATE_EXPIRED = "expired"
STATE_WATCHED = "watched"
STATE_BREACHED = "breached"
STATE_NO_RED_LINE = "no_red_line"

# Состояния, из которых вердикт уже не появится сам. Их нельзя оставлять в
# наблюдении: изменение живёт в кабинете непроверенным, а прогон каждый раз
# пересчитывает по нему одно и то же. Строка закрывается для автоматики и
# попадает в счётчик ручного разбора (failed_rollbacks_count).
#
# «Не появится сам» верно только при ЗЕЛЁНОМ гейте витрины: на красном это
# утверждение о данных, которых не было, и закрытие ставится на паузу (см.
# GATE_DEFERS_CLOSING_REASON и счётчик closing_deferred).
TERMINAL_UNVERIFIED_STATES = {STATE_EXPIRED, STATE_NO_RED_LINE}

# Сервис API → префикс вида действия для рельс. Запрос на возврат обязан
# пройти рельсы пути возврата: иначе откат — единственный путь в кабинет, не
# проверенный ничем. Вид собирается из СЕРВИСА И МЕТОДА фактического запроса,
# а не берётся из действия: если rollback_payload когда-нибудь вернёт delete,
# allow-лист обязан это увидеть и отклонить.
_SERVICE_KIND_PREFIX = {"bidmodifiers": "bidmodifier"}


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value[:19].replace("Z", "")).date()
    return None


def observation_window(
    action: Dict[str, Any], today: date, crm_through: Optional[date]
) -> Optional[Tuple[date, date, bool]]:
    """Окно наблюдения действия: (первый день, последний день, закрыто ли).

    Отсчёт — от МОМЕНТА ПРИМЕНЕНИЯ этого действия, а не от общего окна прогона:
    красная линия ставилась вместе с действием, и судить о ней надо по тому,
    что случилось после него. Общее окно на всех смешало бы в наблюдение дни,
    когда изменения ещё не было.

    Для зависшей строки ('stale') applied_at равен моменту отправки запроса
    (writer/db.py::MARK_STALE_SQL), но подстраховка на created_at оставлена:
    строка без обеих отметок времени не наблюдаема вообще, и это не повод
    считать её здоровой.

    None — окно ещё не открылось: ни одного полного дня после применения не
    прошло. «Закрыто» значит, что горизонт исчерпан и новых данных в окно уже
    не добавится.
    """
    applied = _as_date(action.get("applied_at")) or _as_date(action.get("created_at"))
    if applied is None:
        return None
    if crm_through is None:
        # Лидов нет вовсе — наблюдать не по чему. Подставить сюда сегодня
        # значило бы вынести вердикт по пустоте: расход есть, лидов ноль,
        # CPA бесконечен, красная линия пробита на ровном месте.
        return None
    start = applied + timedelta(days=OBSERVATION_LAG_DAYS)
    horizon_end = start + timedelta(days=OBSERVATION_HORIZON_DAYS - 1)
    # Три ограничителя, и каждый отвечает за своё: горизонт — чтобы не судить
    # об изменении по кварталу дрейфа; вчера — сегодняшний день неполон по
    # расходу; граница зрелости CRM — дальше неё лежат дни с расходом и нулём
    # лидов, и это не провал кампании, а отставание источника.
    end = min(horizon_end, today - timedelta(days=1), crm_through)
    if start > end:
        return None
    return start, end, end >= horizon_end


def observed_metrics(rows: Iterable[Dict[str, Any]], window: Tuple[date, date, bool],
                     exclude: Optional[date] = None) -> Dict[str, Any]:
    """Наблюдаемые метрики объекта за окно: расход, эффективные лиды, CPA.

    Знаменатель — eff_leads, тот же, что у базового CPA (agent/db.py::
    load_baseline_cpa): красная линия сравнивает наблюдаемое с базовым, и
    разные знаменатели сделали бы сравнение бессмысленным.

    leads=0 → cpa=0: is_breached всё равно не выносит вердикт до минимума
    наблюдений, а деление на ноль дало бы бесконечность, которая пробила бы
    любой порог на первом же дне.

    exclude — день, который надо выбросить из подсчёта. Используется проверкой
    на выброс (breach_survives_peak_day): «то же окно, но без самого дорогого
    дня». Выбрасывается день целиком, вместе со своими лидами, иначе сравнение
    считалось бы по разным знаменателям.
    """
    start, end, _ = window
    cost = 0.0
    leads = 0
    days = 0
    for row in rows:
        fact_date = _as_date(row.get("fact_date"))
        if fact_date is None or fact_date < start or fact_date > end:
            continue
        if exclude is not None and fact_date == exclude:
            continue
        cost += float(row.get("cost") or 0.0)
        leads += int(row.get("eff_leads") or 0)
        days += 1
    return {
        "cost": round(cost, 2),
        "leads": leads,
        "cpa": round(cost / leads, 2) if leads > 0 else 0.0,
        "days": days,
    }


def observed_leads_delta(observed: Dict[str, Any],
                         action: Dict[str, Any]) -> Optional[float]:
    """Сколько лидов действие принесло сверх базового темпа за окно наблюдения.

    Не разность сумм: окно базы 28 дней, окно наблюдения 7–14, и голая
    разность показала бы обвал там, где темп вырос. Поэтому темп базы
    (red_line.baseline_leads_per_day, положен туда при планировании) множится
    на длину окна наблюдения и вычитается из его лидов.

    None, когда темпа базы в линии нет (действие спланировано до появления
    этой пары) или окно пустое: отсутствие числа честнее нуля, который петля
    обучения прочитала бы как «прогноз сбылся ровно наполовину».
    """
    base_rate = float((action.get("red_line") or {}).get("baseline_leads_per_day") or 0.0)
    days = int(observed.get("days") or 0)
    if base_rate <= 0 or days <= 0:
        return None
    return round(float(observed.get("leads") or 0) - base_rate * days, 2)


def peak_cost_day(rows: Iterable[Dict[str, Any]],
                  window: Tuple[date, date, bool]) -> Optional[date]:
    """День окна с максимальным расходом. None — фактов в окне нет вовсе."""
    start, end, _ = window
    by_day: Dict[date, float] = {}
    for row in rows:
        fact_date = _as_date(row.get("fact_date"))
        if fact_date is None or fact_date < start or fact_date > end:
            continue
        by_day[fact_date] = by_day.get(fact_date, 0.0) + float(row.get("cost") or 0.0)
    if not by_day:
        return None
    return max(sorted(by_day), key=lambda d: by_day[d])


def breach_survives_peak_day(
    red_line: Dict[str, Any], rows: Iterable[Dict[str, Any]],
    window: Tuple[date, date, bool]
) -> Tuple[bool, Optional[date], Dict[str, Any]]:
    """Держится ли пробой, если выбросить самый дорогой день окна.

    Фильтр разового выброса. Вердикт берётся на КАЖДОМ прогоне по РАСТУЩЕМУ
    окну, и выигрывает первое же пересечение порога: чем дольше наблюдение,
    тем больше независимых попыток пробить линию случайностью, и фактическая
    вероятность ложного отката выше номинальной.

    Проверять «то же окно без последнего дня» для этого нельзя, хотя выглядит
    похоже: окно НАКОПИТЕЛЬНОЕ, растёт от даты применения, поэтому «окно без
    последнего дня» — это буквально окно предыдущего прогона. Разовый дорогой
    день, продавивший накопительную среднюю сегодня, продавит её и завтра, и
    послезавтра: такая проверка откладывает откат ровно на один прогон, а не
    отфильтровывает выброс. Здоровая кампания с одним дорогим днём без лидов
    откатывалась на втором прогоне.

    Выброс невосстановим — значит и убирать надо его сам, а не хвост окна:
    пробой засчитывается, только если он держится БЕЗ дня с максимальным
    расходом. Настоящая деградация держится (она не в одном дне), одиночный
    сбой синка лидов или разовый дорогой клик — нет, и не начнёт держаться от
    того, что окно выросло.

    Плата за фильтр названа явно: деградация, целиком уместившаяся в один
    день, вердикта не получит никогда и уйдёт человеку по истечении горизонта
    (STATE_EXPIRED). Для однодневного эффекта это правильный размен —
    красная линия судит устойчивый вред, а не дневной шум.
    """
    peak = peak_cost_day(rows, window)
    if peak is None:
        return False, None, {}
    without_peak = observed_metrics(rows, window, exclude=peak)
    survives, _ = is_breached(red_line, without_peak)
    return survives, peak, without_peak


def mart_filled_days(mart: Dict[str, Any], window: Tuple[date, date, bool]) -> int:
    """Сколько дней окна наполнена ВИТРИНА — вся, а не наблюдаемые кампании.

    mart — ширина витрины по дням (agent_db.load_mart_day_breadth): отдельный
    дешёвый запрос по всей витрине за окно, БЕЗ фильтра по кампаниям открытых
    действий.

    Знаменатель полноты обязан описывать ВИТРИНУ, а не открутку конкретной
    кампании: витрина наполняется парами «день, кампания», реально
    присутствующими в источниках, и дня с нулевой откруткой в ней нет вовсе.
    Считая покрытие по дням самой кампании, сторож требовал бы от неё
    ежедневной открутки: кампания с прерывистой откруткой не получала бы
    вердикта никогда и по закрытии горизонта уходила в ручной разбор.

    Хуже того, смещение было направлено не в ту сторону: чем сильнее правка
    агента придушила кампанию, тем меньше у неё дней с фактами, тем ниже
    покрытие и тем вернее вердикта не будет — защита слабела ровно там, где
    вред сильнее. День, за который витрина наполнена, а у кампании расхода
    нет, — это честный ноль (ноль расхода, ноль лидов), а не дыра в данных.

    Ровно этот дефект вернулся через границу ЗАГРУЗКИ данных: строки брались
    только по кампаниям открытых действий, и при двух открытых действиях
    «доля дней, за которые наполнена витрина» означала «доля дней, когда была
    открутка у этих двух кампаний». Поэтому источник знаменателя теперь
    отдельный и к наблюдаемым кампаниям отношения не имеет.
    """
    start, end, _ = window
    days = (mart or {}).get("days") or {}
    return sum(1 for raw in days
               if _as_date(raw) is not None and start <= _as_date(raw) <= end)


def window_coverage(filled_days: int, window: Tuple[date, date, bool]) -> float:
    """Доля дней окна, за которые витрина наполнена."""
    start, end, _ = window
    length = (end - start).days + 1
    if length <= 0:
        return 0.0
    return min(1.0, int(filled_days) / float(length))


def _unverified(state: str, closed: bool) -> str:
    """Незавершённое состояние на закрытом горизонте — это 'истекло'.

    Пока горизонт открыт, «мало наблюдений», «дырявое покрытие» и
    «неподтверждённый пробой» рассосутся сами: окно растёт, данные доезжают.
    После закрытия горизонта окно уже не меняется, и каждый следующий прогон
    будет считать по нему ровно то же самое — вечно. Такая строка обязана
    уйти человеку, а не висеть в наблюдении.
    """
    return STATE_EXPIRED if closed else state


def judge(action: Dict[str, Any], facts_by_campaign: Dict[str, List[Dict[str, Any]]],
          today: date, mart: Dict[str, Any],
          crm_through: Optional[date],
          account_totals: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Вердикт по одному действию: состояние наблюдения и наблюдаемые метрики.

    Два источника данных, и они РАЗНЫЕ намеренно: наблюдаемые метрики берутся
    по кампании действия (facts_by_campaign), а знаменатель полноты — по всей
    витрине (mart). Считай их по одним строкам — и полнота данных мерилась бы
    откруткой той самой кампании, которую действие могло придушить.
    """
    red_line = action.get("red_line") or {}
    if not red_line:
        return {"state": STATE_NO_RED_LINE, "reason": NO_RED_LINE_REASON}

    window = observation_window(action, today, crm_through)
    if window is None:
        return {"state": STATE_WAITING, "reason": "окно наблюдения ещё не открылось"}

    rows = facts_by_campaign.get(str(action.get("object_id"))) or []
    observed = observed_metrics(rows, window)
    start, end, closed = window
    filled = mart_filled_days(mart, window)
    coverage = window_coverage(filled, window)
    verdict = {
        "observed": observed,
        "coverage": round(coverage, 2),
        "mart_filled_days": filled,
        "window": {"from": start.isoformat(), "to": end.isoformat(), "closed": closed},
    }

    # Полнота ДО вывода. Пустое окно даёт нули по всем метрикам, и «нулей нет
    # выше порога» читается как «проблемы нет»: красная линия молча не
    # срабатывает никогда, а изменение живёт непроверенным до истечения
    # горизонта. Отсутствие данных — это отсутствие данных, а не здоровье.
    if coverage < MIN_WINDOW_COVERAGE:
        return {**verdict, "state": _unverified(STATE_LOW_COVERAGE, closed),
                "reason": (f"витрина наполнена за {filled} дн. из "
                           f"{(end - start).days + 1} — покрытие "
                           f"{coverage:.0%} при минимуме {MIN_WINDOW_COVERAGE:.0%}")}

    # Сезонная поправка порога: если подорожал весь кабинет, пост-CPA вырос
    # бы и у здоровой кампании. Порог двигается следом за контролем, зажато.
    factor = seasonal_factor(account_totals, _baseline_window(red_line),
                             (start, end))
    verdict["seasonal_factor"] = round(factor, 3)
    if factor != 1.0 and red_line.get("max_value") is not None:
        red_line = {**red_line,
                    "max_value": float(red_line["max_value"]) * factor}
        verdict["seasonal_max_value"] = round(red_line["max_value"], 2)

    breached, reason = is_breached(red_line, observed)
    if breached:
        # Пробой засчитывается, только если переживает выброс: см.
        # breach_survives_peak_day — там же, почему прежняя проверка «то же
        # окно без последнего дня» ничего не фильтровала на накопительном окне.
        confirmed, peak, without_peak = breach_survives_peak_day(red_line, rows, window)
        if peak is not None:
            verdict["without_peak_day"] = {"date": peak.isoformat(), **without_peak}
        if not confirmed:
            return {**verdict, "state": _unverified(STATE_UNCONFIRMED, closed),
                    "reason": (f"{reason}; пробой держится только на самом дорогом "
                               f"дне окна ({peak.isoformat() if peak else '—'}) — "
                               f"это выброс, а не деградация")}
        return {**verdict, "state": STATE_BREACHED,
                "reason": f"{reason}; держится и без самого дорогого дня окна"}

    # Обвал расхода — ДО проверки min_leads: у придушенной кампании лидов
    # мало по построению, и CPA-ветка навсегда оставляла бы её в
    # «наблюдений N из 20». Фильтр пикового дня не применяется: обвал —
    # среднее за ≥3 дня, разовый выброс его не создаёт.
    collapsed, collapse_reason = is_spend_collapsed(action, observed)
    if collapsed:
        return {**verdict, "state": STATE_BREACHED, "reason": collapse_reason}

    min_leads = int(red_line.get("min_leads") or 0)
    if observed["leads"] < min_leads:
        # Горизонт закрыт, а минимума так и нет — вердикта не будет никогда.
        # Это не «всё хорошо»: изменение живёт непроверенным, и в отчёте оно
        # обязано стоять отдельной строкой, а не растворяться в «под наблюдением».
        return {**verdict, "state": _unverified(STATE_COLLECTING, closed),
                "reason": f"наблюдений {observed['leads']} из {min_leads}"}
    return {**verdict, "state": STATE_WATCHED, "reason": reason or ""}


def _window_cost_leads(rows: Iterable[Dict[str, Any]],
                       window: Tuple[date, date]) -> Tuple[float, int]:
    """Расход и эффективные лиды дневных строк, попавших в окно."""
    start, end = window
    cost = 0.0
    leads = 0
    for row in rows or ():
        day = _as_date(row.get("fact_date"))
        if day is None or day < start or day > end:
            continue
        cost += float(row.get("cost") or 0.0)
        leads += int(row.get("eff_leads") or 0)
    return cost, leads


def _totals_cpa(rows: Iterable[Dict[str, Any]],
                window: Tuple[date, date]) -> Optional[float]:
    """Кабинетный CPA за окно из дневных агрегатов витрины, или None."""
    cost, leads = _window_cost_leads(rows, window)
    if leads < SEASONAL_MIN_LEADS or cost <= 0:
        return None
    return cost / leads


def seasonal_factor(account_totals: Iterable[Dict[str, Any]],
                    baseline_window: Optional[Tuple[date, date]],
                    observation: Optional[Tuple[date, date]]) -> float:
    """Во сколько раз подорожал лид у КАБИНЕТА между базой и окном наблюдения.

    Красная линия строится от базового CPA кампании, снятого до применения
    изменения. Если за время наблюдения подорожал весь аукцион, пост-CPA
    вырастет у всех — и у здоровых кампаний тоже. Поправка нормирует порог на
    движение контроля за ТО ЖЕ окно (рекомендация аудита), зажатая
    SEASONAL_CAP в обе стороны.

    Контрольных данных не хватает (мало лидов, нет окна, нет базы у действия) —
    поправки нет: 1.0. Молчаливое «наверное, сезон» ослабило бы защиту.
    """
    if not baseline_window or not observation:
        return 1.0
    base_cpa = _totals_cpa(account_totals, baseline_window)
    now_cpa = _totals_cpa(account_totals, observation)
    if not base_cpa or not now_cpa:
        return 1.0
    return min(max(now_cpa / base_cpa, 1.0 / SEASONAL_CAP), SEASONAL_CAP)


def _baseline_window(red_line: Dict[str, Any]) -> Optional[Tuple[date, date]]:
    """Окно базового CPA из красной линии, если оно там записано.

    Действия, применённые до появления этих полей, поправки не получают —
    судить о сезоне без базового окна не по чему.
    """
    start = _as_date(red_line.get("baseline_from"))
    end = _as_date(red_line.get("baseline_to"))
    return (start, end) if start and end else None


def closing_verdict(observed: Dict[str, Any], action: Dict[str, Any]) -> str:
    """Вердикт закрываемого наблюдения — по непрерывному эффекту (Э2.4).

    Считает тот же эффект, что уйдёт в историю экспериментов
    (action_experiment), и переводит его в градуированный исход
    (rollback.outcome_verdict). Прежнее «held» означало лишь «не пробил
    аварийный порог» и записывало умеренный вред как успех.
    """
    red = action.get("red_line") or {}
    base = float(red.get("baseline_cpa") or 0.0)
    cpa = float((observed or {}).get("cpa") or 0.0)
    leads = int((observed or {}).get("leads") or 0)
    if base <= 0 or cpa <= 0:
        return "unknown"
    return outcome_verdict((cpa - base) / base, math.sqrt(1.0 / max(leads, 1)))


# Рычаги, которыми агент ПОКУПАЕТ ОБЪЁМ. Только у них ожидание может быть
# положительным, и только их обещание — «лидов станет больше», а не «лид
# станет дешевле». Список именно рычагов, а не «всех действий с плюсовым
# ожиданием»: корректировка ставки вверх обещает перераспределение внутри
# того же бюджета, и спрашивать с неё объём было бы подменой обещания.
GROWTH_KINDS = frozenset({BUDGET_KIND, BUDGET_DAILY_KIND, TCPA_KIND})


def economic_outcome(action: Dict[str, Any], observed: Dict[str, Any],
                     max_cpa: float) -> str:
    """Исход действия в терминах его же намерения — мера ОБУЧЕНИЯ.

    Красная линия и откат этой функции не касаются: защита денег обязана
    оставаться консервативной, и «CPA вырос» — правильный повод притормозить.
    Меняется то, на чём агент учится.

    Сокращение обещало цену — с него и спрашивается цена (closing_verdict).
    Доливка обещала ОБЪЁМ при цене не выше предельно допустимой: лиды
    выросли, а CPA остался под потолком — обещание выполнено, даже если цена
    поднялась. Судить доливку по «подешевело ли» значит требовать от неё
    того, ради чего её не делали, и получить механизм, который умеет только
    резать: срезав объём, кампания почти всегда дешевеет, а за объём и платят
    более высокой ценой конверсии.

    max_cpa — потолок цены конверсии из красной линии этого же действия
    (rollback.red_line_for: база × (1 + RED_LINE_TOLERANCE)), при
    необходимости уже поправленный на сезон. В плане на его месте стояла λ
    портфеля — но λ там безразмерное отношение окупаемости (portfolio.py:212),
    и сравнивать с ним рубли нельзя; потолок линии — та самая «предельно
    допустимая цена», о которой договорились при планировании.

    Факта объёма нет (у действия не было темпа базы) — «unknown», а не ноль:
    измерения не делали, и записывать его промахом нельзя.
    """
    expected = (action.get("payload") or {}).get("expected_leads_delta")
    up = float(expected or 0.0) > 0
    if not up or str(action.get("action_kind")) not in GROWTH_KINDS:
        return closing_verdict(observed, action)
    delta = observed_leads_delta(observed, action)
    if delta is None or max_cpa <= 0:
        return "unknown"
    cpa = float((observed or {}).get("cpa") or 0.0)
    if delta > 0 and 0 < cpa <= max_cpa:
        return "improved"
    if delta <= 0:
        return "worsened"
    # Объём пришёл, но дороже оговорённого потолка: обещание выполнено
    # наполовину, и записывать это ни успехом, ни провалом нельзя.
    return "inconclusive"


def money_metrics(rows: Iterable[Dict[str, Any]],
                  window: Tuple[date, date, bool]) -> Dict[str, Any]:
    """Наблюдение за тем же окном, но в оплатах: расход, оплаты, цена оплаты.

    Зеркало observed_metrics со сменой знаменателя. Отдельная функция, а не
    параметр: у них разные базы сравнения (baseline_cpa против baseline_cpo)
    и разные сроки готовности данных, и смешивать их в одном вызове значило
    бы однажды сравнить цену заявки с ценой оплаты.

    payments=0 → cpo=0, ровно как leads=0 → cpa=0 у соседа: «цены нет» — не
    «цена бесконечна». Бесконечность пробила бы любой порог на первом дне.
    """
    start, end, _ = window
    cost = 0.0
    payments = 0
    days = 0
    for row in rows:
        fact_date = _as_date(row.get("fact_date"))
        if fact_date is None or fact_date < start or fact_date > end:
            continue
        cost += float(row.get("cost") or 0.0)
        payments += int(row.get("payments_fact") or 0)
        days += 1
    return {
        "cost": round(cost, 2),
        "payments": payments,
        "cpo": round(cost / payments, 2) if payments > 0 else 0.0,
        "days": days,
    }


def money_verdict(money: Dict[str, Any], action: Dict[str, Any]) -> str:
    """Исход того же действия, посчитанный по цене ОПЛАТЫ.

    Та же шкала, что у вердикта по заявкам (rollback.outcome_verdict), и та
    же оценка ошибки — из объёма знаменателя. Разница в знаменателе и в
    сроке: оплаты дозревают дольше наблюдения, поэтому вердикт снимается
    позже (money_check_due).

    Нет денежной базы — «unknown», а не успех. Действия, применённые до
    появления baseline_cpo, сверять не с чем, и записывать их как
    подтверждённые деньгами нельзя.
    """
    red = action.get("red_line") or {}
    base = float(red.get("baseline_cpo") or 0.0)
    cpo = float((money or {}).get("cpo") or 0.0)
    payments = int((money or {}).get("payments") or 0)
    if base <= 0 or cpo <= 0:
        return "unknown"
    return outcome_verdict((cpo - base) / base, math.sqrt(1.0 / max(payments, 1)))


def money_check_due(action: Dict[str, Any], today: date) -> bool:
    """Пора ли сверять вердикт деньгами: прошло ли MONEY_CHECKPOINT_DAYS.

    Отсчёт от ПРИМЕНЕНИЯ, а не от закрытия наблюдения: дозревают оплаты
    лидов, пришедших после изменения, и их возраст считается от него же.
    Строка без даты применения чекпоинта не получает — отсчитывать не от
    чего, и подставлять сюда дату создания значило бы измерять окно, которого
    не было.
    """
    applied = _as_date(action.get("applied_at"))
    if applied is None:
        return False
    return (today - applied).days >= MONEY_CHECKPOINT_DAYS


# Минимум эффективных лидов заповедника в окне наблюдения, чтобы контроль
# вообще считался контролем. Число живёт в holdout.py — там же, где сам
# заповедник: контролем он работает и здесь, и в замере такта целиком
# (tact_effect), а две копии порога однажды назвали бы контролем разное. Имя
# здесь оставлено прежним: на него ссылаются читатели сторожа.
MIN_CONTROL_LEADS = holdout.MIN_CONTROL_LEADS


def tact_effect_report(actions: List[Dict[str, Any]],
                       facts_by_campaign: Dict[str, List[Dict[str, Any]]],
                       holdout_facts: Optional[List[Dict[str, Any]]],
                       holdout_ids: Iterable[str],
                       today: date) -> Dict[str, Any]:
    """Эффект ТАКТА, чей горизонт наблюдения закрывается сегодня.

    Зачем рядом с индивидуальным DiD. Тот судит одно действие по одной
    кампании, и при четырёхстах одновременных правках у каждого своего
    контроля слишком мало. Общий сдвиг кабинета при этом складывается не в
    четыреста маленьких провалов, а в один — общий, которого агент не делал.
    Замер такта спрашивает с одного решения одним числом и там, где объёма
    хватает: все обработанные разом против заповедника разом.

    Такт определяется днём ПРИМЕНЕНИЯ, а не днём планирования: отложенное
    риск-бюджетом действие уезжает в кабинет следующим прогоном и работает
    вместе с ним, а не с тем тактом, где было задумано.

    Факты обеих групп идут в замер одним списком — разделяет их он сам по
    спискам кампаний. Делить снаружи значило бы однажды положить кампанию в
    обе стороны сразу, и заповедник перестал бы быть контролем ровно там, где
    это важнее всего заметить.
    """
    tact_day = today - timedelta(days=OBSERVATION_HORIZON_DAYS)
    applied = sorted({str(action.get("object_id")) for action in actions or ()
                      if _as_date(action.get("applied_at")) == tact_day})
    facts: List[Dict[str, Any]] = []
    for rows in (facts_by_campaign or {}).values():
        facts.extend(rows)
    facts.extend(holdout_facts or [])
    return tact_effect.measure(tact_day, facts, holdout_ids, applied,
                               OBSERVATION_HORIZON_DAYS)


def holdout_control(rows: Iterable[Dict[str, Any]],
                    baseline_window: Optional[Tuple[date, date]],
                    observation: Optional[Tuple[date, date]]) -> Optional[Dict[str, Any]]:
    """Заповедник за те же два окна: цена до и цена после — контроль для DiD.

    Заповедник (holdout.select_holdout) — кампании, к которым агент сознательно
    не применяет ничего. Их движение между базовым окном действия и окном его
    наблюдения и есть сезон плюс рынок за тот же период.

    None — контроля нет: пустой заповедник, нет окна базы у действия или в
    одном из окон нет лидов. Молчаливая подстановка нулей выдала бы «сезон
    равен нулю», то есть подтвердила бы авторство действия там, где о нём
    ничего не известно.
    """
    if not rows or not baseline_window or not observation:
        return None
    base_cost, base_leads = _window_cost_leads(rows, baseline_window)
    obs_cost, obs_leads = _window_cost_leads(rows, observation)
    if base_leads <= 0 or obs_leads <= 0:
        return None
    return {"baseline_cpa": round(base_cost / base_leads, 2),
            "cpa": round(obs_cost / obs_leads, 2),
            "leads": obs_leads}


def _did_effect(observed: Dict[str, Any], baseline_cpa: float,
                control: Optional[Dict[str, Any]]) -> Optional[float]:
    """Эффект действия против контроля за то же окно (разность разностей).

    Вычитая изменение заповедника, получаем эффект самого действия. Без
    контроля — None и остаёмся на before_after: завышенный класс надёжности
    дороже отсутствующего, потому что на классах строится автономия (Ф9).
    """
    if not control or int(control.get("leads") or 0) < MIN_CONTROL_LEADS:
        return None
    base_c = float(control.get("baseline_cpa") or 0.0)
    obs_c = float(control.get("cpa") or 0.0)
    cpa = float((observed or {}).get("cpa") or 0.0)
    if baseline_cpa <= 0 or cpa <= 0 or base_c <= 0 or obs_c <= 0:
        return None
    treated = (cpa - baseline_cpa) / baseline_cpa
    control_shift = (obs_c - base_c) / base_c
    return round(treated - control_shift, 4)


def action_experiment(action: Dict[str, Any], observed: Dict[str, Any],
                      window: Tuple[date, date, bool], verdict: str,
                      control: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Исход собственного действия — строкой истории экспериментов (Э2.4).

    Замыкает петлю «применили → понаблюдали → выучили»: до этого закрытые
    действия не оставляли в edu_agent_experiments ничего, и читатель истории
    видел только чужие скачки бюджета, но не опыт самого агента.

    Класс надёжности — B, пока нет контроля: сравнение «наблюдение против
    базы до изменения» не вычитает сезон, а знание даты и намерения сезон не
    отменяет. Класс A даёт контроль за ТО ЖЕ окно — заповедник портфеля
    (holdout.select_holdout): по нему считается разность разностей, и только
    тогда механизм становится did_holdout. Ошибка оценки — только из счётчика лидов окна
    наблюдения: в красной линии лежит ТЕМП базы (лиды в день, ради
    observed_leads_delta), а не число лидов, на котором она снята, — поэтому
    rel_error по-прежнему нижняя граница, и это отмечено полем
    rel_error_floor.
    """
    red = action.get("red_line") or {}
    base = float(red.get("baseline_cpa") or 0.0)
    cpa = float((observed or {}).get("cpa") or 0.0)
    leads = int((observed or {}).get("leads") or 0)
    rel = math.sqrt(1.0 / max(leads, 1))
    effect = effect_lo = effect_hi = None
    if base > 0 and cpa > 0:
        effect = round((cpa - base) / base, 4)
        effect_lo = round(effect - rel, 4)
        effect_hi = round(effect + rel, 4)
    # Контроль за то же окно повышает и оценку, и класс: DiD вычитает сезон,
    # before_after — нет. Подменяются ровно три поля, остальные как есть.
    did = _did_effect(observed, base, control)
    mechanism, reliability = "before_after", "B"
    control_params: Dict[str, Any] = {}
    if did is not None:
        effect = did
        effect_lo = round(did - rel, 4)
        effect_hi = round(did + rel, 4)
        mechanism, reliability = "did_holdout", "A"
        # Контроль едет в параметры: без его чисел разность разностей нечем
        # перепроверить, а класс A — самое сильное утверждение в истории.
        control_params = {"control": {k: control.get(k)
                                      for k in ("baseline_cpa", "cpa", "leads")}}
    start, end, _ = window
    applied = _as_date(action.get("applied_at")) or _as_date(action.get("created_at"))
    raw = f"action:{action.get('action_id')}"
    return {
        "experiment_id": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24],
        "hypothesis_type": str(action.get("action_kind") or "unknown"),
        "object_level": str(action.get("object_level") or "campaign"),
        "object_id": str(action.get("object_id")),
        "params": {
            "action_id": action.get("action_id"),
            "direct_type": action.get("direct_type"),
            "setting_key": action.get("setting_key"),
            "baseline_cpa": base or None,
            "observed_cpa": cpa or None,
            "leads": leads,
            "rel_error": round(rel, 4),
            "rel_error_floor": True,
            **control_params,
        },
        "mechanism": mechanism,
        "started_on": (applied or start).isoformat(),
        "measured_on": end.isoformat(),
        "effect": effect,
        "effect_lo": effect_lo,
        "effect_hi": effect_hi,
        "metric": "eff_cpl",
        "verdict": verdict,
        "reliability_class": reliability,
        "source": "action",
    }


def facts_gate(mart: Dict[str, Any], today: date,
               max_age_days: int = FACTS_MAX_AGE_DAYS) -> Dict[str, Any]:
    """Гейт качества витрины ВНУТРИ сторожевого прогона.

    Тонкая обёртка над общим гейтом пишущих прогонов (sync/agent/gate.py,
    mart_gate) — там же вся логика ширины дня и медианного эталона. Расчёт
    лишь считает числа, а сторож МЕНЯЕТ КАБИНЕТ: красный гейт запрещает
    откат, но не наблюдение — смотреть на плохие данные можно, действовать
    по ним нельзя.
    """
    return mart_gate(mart, today, max_age_days)


def gate_allows_rollback(gate: Optional[Dict[str, Any]]) -> bool:
    """Красный или невычисленный гейт откат запрещает. Умолчание — запрет."""
    return bool(gate) and str(gate.get("status")) == "GREEN"


def rollback_guard_form(action: Dict[str, Any], service: str, method: str,
                        params: Dict[str, Any]) -> Dict[str, Any]:
    """Запрос на возврат в той форме, которую понимает рельса пути возврата.

    Коэффициент передаётся в 100-БАЗНОЙ шкале API и под именем
    api_coefficient — check_rollback проверяет по нему диапазон Директа
    (0..1300), а не потолок назначения ±50 %. Потолок описывает, что агенту
    позволено НАЗНАЧАТЬ; прошлое значение он не описывает вообще — его
    поставил человек, и +80 % или 0 («показы выключены») для Директа штатны.

    Вместе с запросом рельсе передаются СЫРЫЕ поля журнала — вид исходного
    действия и previous_state, — а не готовое ожидание: рельса выводит, куда
    обязан вернуть откат, сама и сверяет с тем, что построил rollback_payload.
    Посчитай ожидание здесь — сверка проверяла бы построитель им же самим.
    """
    if str(service) == "campaigns":
        # У campaigns.update вид не выводится из пары сервис+метод: одним
        # методом возвращаются и расписание, и бюджет. Вид выводится из
        # СОДЕРЖИМОГО фактического запроса — по тому же принципу «не верить
        # полю журнала»: если построитель соберёт не тот блок, allow-лист
        # увидит не тот вид и отклонит.
        campaign = ((params.get("Campaigns") or [{}])[0]) or {}
        if (str(method) == "resume"
                and (params.get("SelectionCriteria") or {}).get("Ids")):
            # Возврат выключения (Э3.4): у resume нет Campaigns, кампании
            # адресуются SelectionCriteria. suspend сюда не попадает — из
            # него вывелся бы вид "campaigns.suspend", и рельса возврата
            # отклонила бы его: повторное выключение — не отмена.
            kind = "campaign.suspend"
            campaign = {"Id": (params["SelectionCriteria"]["Ids"] or [None])[0]}
        elif "TimeTargeting" in campaign:
            kind = "schedule.set"
        elif "DailyBudget" in campaign:
            kind = "budget.set_daily"
        elif (campaign.get("TextCampaign") or {}).get("BiddingStrategy"):
            kind = "budget.set"
        else:
            kind = f"campaigns.{method}"  # неизвестный блок — рельса отклонит
        return {
            "action_kind": kind,
            "object_level": action.get("object_level"),
            "object_id": action.get("object_id"),
            "api_coefficient": None,
            "origin_action_kind": action.get("action_kind"),
            "previous_state": action.get("previous_state") or {},
            "payload": {"Id": campaign.get("Id")},
        }

    if str(service) == "adgroups":
        # География живёт в ГРУППАХ: campaigns поля регионов не имеет вовсе
        # (edu_direct_settings: RegionIds спрашивается у adgroups.get). Вид
        # выводится из СОДЕРЖИМОГО запроса, как и у campaigns выше: соберёт
        # построитель не тот блок — allow-лист увидит не тот вид и отклонит.
        groups = params.get("AdGroups") or []
        has_regions = bool(groups) and all(
            "RegionIds" in (group or {}) for group in groups)
        return {
            "action_kind": "geo.set" if has_regions else f"adgroups.{method}",
            "object_level": action.get("object_level"),
            "object_id": action.get("object_id"),
            "api_coefficient": None,
            "origin_action_kind": action.get("action_kind"),
            "previous_state": action.get("previous_state") or {},
            "payload": {"Id": (groups[0] or {}).get("Id") if groups else None},
        }

    prefix = _SERVICE_KIND_PREFIX.get(str(service), str(service))
    item = ((params.get("BidModifiers") or [{}])[0]) or {}
    return {
        "action_kind": f"{prefix}.{method}",
        "object_level": action.get("object_level"),
        "object_id": action.get("object_id"),
        "api_coefficient": item.get("BidModifier"),
        "origin_action_kind": action.get("action_kind"),
        "previous_state": action.get("previous_state") or {},
        "payload": {"Id": item.get("Id")},
    }


def _segment_of(action: Dict[str, Any]) -> Tuple[str, str]:
    """Сегмент действия: (тип корректировки Директа, ключ внутри типа).

    Колонки direct_type/setting_key появились позже payload, поэтому у старых
    строк журнала сегмент лежит только в payload.
    """
    payload = action.get("payload") or {}
    direct_type = action.get("direct_type") or payload.get("Type") or ""
    setting_key = action.get("setting_key") or payload.get("key") or ""
    return str(direct_type), str(setting_key)


def resolve_added_modifier_id(client, action: Dict[str, Any]) -> Optional[Any]:
    """Id добавленной корректировки, восстановленный ЧТЕНИЕМ кабинета.

    Строка bidmodifier.add без сохранённого ответа API (обрыв процесса между
    отправкой и отметкой — статус 'stale') неоткатываема по построению: Id
    созданного объекта неизвестен. Но он не потерян: bidmodifiers.get отдаёт
    Id для каждой пары «тип, ключ» кампании, а тип и ключ у действия записаны.
    Сверка с кабинетом превращает вечно неоткатываемую строку в обычную.

    Чтение состояния не меняет и потому разрешено всегда — в том числе в
    репетиции.

    Совпадение обязано быть ЕДИНСТВЕННЫМ и однозначным: составная
    корректировка («мужчины 25–34») и запись без годного коэффициента в
    кандидаты не берутся, а двойное совпадение по одному сегменту означает,
    что кабинет не такой, каким мы его считаем, — угадывать там нельзя.
    """
    want_type, want_key = _segment_of(action)
    if not want_type or not want_key:
        return None
    try:
        actual = read_actual_modifiers(client, str(action.get("object_id")))
    except Exception:
        # Кабинет недоступен — это не «Id не существует». Молчаливый None
        # оставит строку неоткатываемой на этот прогон, но не пометит её
        # неоткатываемой навсегда: пометку ставит вызывающий код по своей
        # причине, и следующий прогон попробует прочитать снова.
        return None
    matched = [item.get("Id") for item in actual
               if str(item.get("Type")) == want_type
               and str(item.get("key")) == want_key
               and not item.get("composite")
               and not item.get("unusable")
               and item.get("Id") is not None]
    return matched[0] if len(matched) == 1 else None


def _fail(db_module, action: Dict[str, Any], reason: str, permanent: bool,
          journal_ok: bool) -> Dict[str, Any]:
    """Неудачный откат: пометка в журнале и строка для отчёта.

    В репетиции и у песочного клиента журнал не трогается — отчёт всё равно
    показывает, что откат неисполним, но состояние боевой базы от этого не
    меняется.
    """
    marked = None
    if journal_ok:
        marked = db_module.mark_rollback_failed(action["action_id"], reason,
                                                permanent=permanent)
    return {
        "result": "rollback_failed",
        "action_id": action.get("action_id"),
        "object_id": action.get("object_id"),
        "reason": reason,
        "permanent": bool(permanent),
        "journal_written": bool(journal_ok),
        "attempts": (marked or {}).get("rollback_attempts"),
        "retries_stopped": bool((marked or {}).get("rollback_failed_at")),
    }


def close_unverified(db_module, action: Dict[str, Any], state: str, reason: str,
                     journal_ok: bool) -> Dict[str, Any]:
    """Снимает с наблюдения строку, по которой вердикта уже не будет.

    Механизм тот же, что у неисполнимого отката (rollback_failed_at): строка
    уходит из open_actions, изменение ОСТАЁТСЯ живым в кабинете, риск-бюджет
    продолжает его оплачивать, а failed_rollbacks_count показывает её в
    отчёте каждого прогона как ждущую человека. Отдельного механизма для
    этого случая в журнале нет и заводить его незачем: последствия ровно те
    же — автоматика сделала всё, что могла, дальше решает человек.
    """
    marked = None
    if journal_ok:
        marked = db_module.mark_rollback_failed(action["action_id"], reason,
                                                permanent=True)
    return {
        "result": "closed_unverified",
        "action_id": action.get("action_id"),
        "object_id": action.get("object_id"),
        "action_kind": action.get("action_kind"),
        "state": state,
        "reason": reason,
        "journal_written": bool(journal_ok),
        "retries_stopped": bool((marked or {}).get("rollback_failed_at")),
    }


def rollback_one(client, action: Dict[str, Any], db_module,
                 holdout_ids: set) -> Dict[str, Any]:
    """Возврат одного объекта в прошлое состояние.

    Ничего не удаляет ни при каких обстоятельствах: rollback_payload строит
    только set нейтрального или прежнего коэффициента, а allow-лист рельсы
    возврата (check_rollback) не пропустил бы delete, даже если бы он там
    появился.
    """
    write_allowed = client.is_write_allowed()
    journal_ok = journal_allowed(client)

    if str(action.get("object_id")) in holdout_ids:
        # Действия в заповедник не попадают (agent_e1 отсекает их check_holdout
        # до применения), но кампанию могли внести в заповедник ПОСЛЕ
        # применения — тогда в журнале живёт применённое действие по её
        # объекту. Трогать её нельзя: заповедник неприкосновенен. Пометки
        # неоткатываемости здесь нет намеренно — состав заповедника меняется,
        # и на следующем прогоне откат может стать возможным.
        return {"result": "blocked_holdout", "action_id": action.get("action_id"),
                "object_id": action.get("object_id"), "reason": HOLDOUT_REASON}

    try:
        request = rollback_payload(action)
        if request is None:
            # Id созданного объекта неизвестен — но он восстановим чтением
            # кабинета по паре «тип, ключ». Пробуем, прежде чем хоронить.
            recovered = resolve_added_modifier_id(client, action)
            if recovered is not None:
                request = rollback_payload({
                    **action,
                    "payload": {**(action.get("payload") or {}), "Id": recovered},
                })
    except Exception as exc:
        # delta_to_api роняет вызов на коэффициенте вне диапазона Директа:
        # прошлое состояние испорчено, и повтор его не починит.
        return _fail(db_module, action, f"запрос на возврат не строится: {exc}"[:300],
                     True, journal_ok)

    if request is None:
        return _fail(db_module, action, NO_ID_REASON, True, journal_ok)

    service, method, params = request
    if str(service) == "campaigns" and str(method) == "resume":
        # Возврат выключения (Э3.4): полнота — непустой список Ids.
        if not (params.get("SelectionCriteria") or {}).get("Ids"):
            return _fail(db_module, action, INCOMPLETE_REASON, True, journal_ok)
    elif str(service) == "campaigns":
        # Возврат через campaigns.update (расписание, бюджет): полнота — это
        # Id кампании и хотя бы один возвращаемый блок. Проверка по
        # BidModifiers здесь хоронила такие откаты как «тело неполное».
        campaign = ((params.get("Campaigns") or [{}])[0]) or {}
        has_block = any(k for k in campaign if k != "Id")
        if campaign.get("Id") is None or not has_block:
            return _fail(db_module, action, INCOMPLETE_REASON, True, journal_ok)
    else:
        item = ((params.get("BidModifiers") or [{}])[0]) or {}
        if item.get("Id") is None or item.get("BidModifier") is None:
            return _fail(db_module, action, INCOMPLETE_REASON, True, journal_ok)

    ok, reason = check_rollback(rollback_guard_form(action, service, method, params))
    if not ok:
        return _fail(db_module, action, f"рельсы отклонили запрос на возврат: {reason}",
                     True, journal_ok)

    if not write_allowed:
        return {"result": "dry_run", "action_id": action.get("action_id"),
                "object_id": action.get("object_id"),
                "request": {"service": service, "method": method, "params": params}}

    try:
        response = client.mutate(service, method, params)
        errors = _element_errors(method, response)
    except Exception as exc:
        # Сбой отправки бывает разовым — попытка засчитывается, но строка не
        # снимается с наблюдения сразу: её снимет счётчик попыток.
        return _fail(db_module, action, f"{type(exc).__name__}: {exc}"[:300],
                     False, journal_ok)

    if errors:
        return _fail(db_module, action, f"API отклонил возврат: {json.dumps(errors, ensure_ascii=False)}"[:300],
                     False, journal_ok)

    if journal_ok and db_module.mark_rolled_back(action["action_id"]) is False:
        # Возврат в кабинет ушёл и прошёл, но строку журнала уже забрал другой
        # контур (второй сторож, ручная правка). Перезаписывать его исход
        # нельзя: он описывает строку точнее, чем эта попытка. Сам кабинет при
        # этом в порядке — возврат идемпотентен, это set прежнего или
        # нейтрального коэффициента.
        # Отметка на None не проверяется намеренно: модуль журнала мог не
        # вернуть ничего (тестовый двойник, старая сигнатура), и трактовать
        # отсутствие возврата как конфликт значило бы заваливать отчёт
        # ложными конфликтами.
        return {"result": "conflicted", "action_id": action.get("action_id"),
                "object_id": action.get("object_id"),
                "reason": JOURNAL_CONFLICT_REASON,
                "journal_written": False, "response": response}
    return {"result": "rolled_back", "action_id": action.get("action_id"),
            "object_id": action.get("object_id"),
            "journal_written": bool(journal_ok), "response": response}


def watch(client, actions: List[Dict[str, Any]], db_module, holdout_ids: set,
          facts_by_campaign: Dict[str, List[Dict[str, Any]]], today: date,
          data_gate: Optional[Dict[str, Any]], crm_through: Optional[date],
          lease: Any = None, mart: Optional[Dict[str, Any]] = None,
          account_totals: Optional[List[Dict[str, Any]]] = None,
          holdout_facts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Наблюдение и откат по всем открытым действиям одного кабинета.

    crm_through — граница зрелости CRM (agent_db.crm_maturity_date). Как и
    data_gate, параметр обязателен и без умолчания: забытая граница означает
    наблюдение по дням, где расход уже есть, а лидов ещё нет, — то есть откат
    здорового изменения по бесконечному CPA. Такая ошибка обязана быть видна
    как ошибка вызова.

    holdout_facts — дневные факты заповедника (load_holdout_facts). Контроль
    за то же окно, из которого исход собственного действия получает класс
    надёжности A вместо B. Нет заповедника — исходы пишутся как раньше,
    механизмом before_after: класс даёт контроль, а не авторство.

    data_gate — вердикт гейта витрины фактов (facts_gate). Красный гейт
    наблюдение не отменяет: состояния считаются и печатаются, пробои видны,
    но в кабинет прогон не пишет. Параметр обязателен и без умолчания:
    забытый гейт обязан быть виден как ошибка вызова, а не тихо разрешать
    откат по данным неизвестного качества.
    """
    states: Dict[str, int] = {}
    breached: List[Dict[str, Any]] = []
    rolled_back = 0
    failures: List[Dict[str, Any]] = []
    blocked_holdout: List[Dict[str, Any]] = []
    blocked_by_gate: List[Dict[str, Any]] = []
    would_roll_back: List[Dict[str, Any]] = []
    needs_review: List[Dict[str, Any]] = []
    deferred_closing: List[Dict[str, Any]] = []
    closed_held: List[Dict[str, Any]] = []
    closed_verdicts: Dict[str, int] = {}
    would_close_held: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    rollback_allowed = gate_allows_rollback(data_gate)
    conflicted: List[Dict[str, Any]] = []

    for action in actions:
        # Одна испорченная строка журнала не должна ослеплять сторожа по всем
        # остальным: непойманное исключение здесь означало бы, что ни одно
        # пробившее линию изменение в этом прогоне не откатится. Строка с
        # ошибкой видна в отчёте и разбирается руками — но не молча, и не
        # ценой всех остальных.
        try:
            verdict = judge(action, facts_by_campaign, today, mart or {},
                            crm_through, account_totals)
        except Exception as exc:
            errors.append({"action_id": action.get("action_id"),
                           "object_id": action.get("object_id"),
                           "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        state = verdict["state"]
        states[state] = states.get(state, 0) + 1

        if state in TERMINAL_UNVERIFIED_STATES:
            if not rollback_allowed:
                # Перманентная пометка — тот же приговор, что и неудавшийся
                # откат: строка уходит из наблюдения навсегда, изменение
                # остаётся живым в кабинете, красная линия по нему не
                # сработает уже никогда. Ставить его по красному гейту
                # нельзя: цепочка «расчёт перестали запускать → покрытие
                # низкое каждый прогон → горизонт закрылся» хоронила действие
                # именно тогда, когда данных не было вовсе. Ждём зелёного
                # гейта — состояние при этом видно в отчёте, а не молчит.
                deferred_closing.append({
                    "action_id": action.get("action_id"),
                    "object_id": action.get("object_id"),
                    "state": state,
                    "reason": GATE_DEFERS_CLOSING_REASON,
                    "gate": (data_gate or {}).get("reason"),
                })
                continue
            reason = EXPIRED_REASON if state == STATE_EXPIRED else NO_RED_LINE_REASON
            detail = verdict.get("reason")
            if detail and state == STATE_EXPIRED:
                reason = f"{reason}: {detail}"
            try:
                needs_review.append(close_unverified(
                    db_module, action, state, reason, journal_allowed(client)))
            except Exception as exc:
                errors.append({"action_id": action.get("action_id"),
                               "object_id": action.get("object_id"),
                               "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue

        # Наблюдение закрывается либо по исчерпанию горизонта, либо ДОСРОЧНО —
        # когда вердикт уже определён. Пока действие «под наблюдением», оно
        # держит риск-бюджет и кулдаун своей кампании, и следующая правка по
        # ней не начинается: лишние дни ожидания — прямая потеря темпа. На
        # живых данных 39 % кампаний набирают порог наблюдений за неделю,
        # то есть горизонт для них вдвое длиннее необходимого.
        if state == STATE_WATCHED and not (verdict.get("window") or {}).get("closed"):
            observed_now = verdict.get("observed") or {}
            red = action.get("red_line") or {}
            enough_days = int(observed_now.get("days") or 0) >= EARLY_CLOSE_MIN_DAYS
            enough_leads = (int(observed_now.get("leads") or 0)
                            >= int(red.get("min_leads") or MIN_LEADS_FOR_VERDICT))
            certain = closing_verdict(observed_now, action) in ("improved", "worsened")
            if enough_days and enough_leads and certain:
                verdict = {**verdict, "early_close": True,
                           "window": {**(verdict.get("window") or {}), "closed": True}}

        if state == STATE_WATCHED and (verdict.get("window") or {}).get("closed"):
            # Горизонт закрыт, линия не пробита: изменение ВЫДЕРЖАЛО. Исход
            # уходит в историю экспериментов, строка — из наблюдения (Э2.4).
            # До этой ветки чистый исход не записывался никуда, и действие
            # пересчитывалось сторожем вечно.
            if not rollback_allowed:
                # Вердикт «выдержало» по красному гейту не выносится: нули
                # мёртвой витрины неотличимы от здоровья — та же логика, что
                # у отложенного закрытия строк без вердикта.
                deferred_closing.append({
                    "action_id": action.get("action_id"),
                    "object_id": action.get("object_id"),
                    "state": "held",
                    "reason": GATE_DEFERS_CLOSING_REASON,
                    "gate": (data_gate or {}).get("reason"),
                })
                continue
            if not journal_allowed(client):
                # Репетиция и песочница журнал не трогают — но исход виден.
                would_close_held.append({
                    "action_id": action.get("action_id"),
                    "object_id": action.get("object_id"),
                    "observed": verdict.get("observed"),
                })
                continue
            try:
                window = observation_window(action, today, crm_through)
                # Захват строки — до записи эксперимента: из двух параллельных
                # сторожей исход записывает тот, чья пометка легла.
                observed = verdict.get("observed") or {}
                # Градуированный исход вместо «held»: умеренный вред обязан
                # лечь в историю вредом, а неотличимый от нуля — «неясно»,
                # иначе шум отобранных по экстремуму действий читается как
                # успех (winner's curse).
                # Мера — по обещанию действия: растящему спрашивается объём
                # при цене под потолком, сокращающему — цена. Потолок берётся
                # уже поправленным на сезон, если поправка была.
                ceiling = float(verdict.get("seasonal_max_value")
                                or (action.get("red_line") or {}).get("max_value")
                                or 0.0)
                outcome = economic_outcome(action, observed, ceiling)
                # Факт кладётся той же отметкой, что и вердикт: разнести их
                # на два запроса значило бы допустить строку с исходом, но
                # без числа, — а петля обучения читает именно пару.
                if window is not None and db_module.mark_observation_closed(
                        action["action_id"], outcome,
                        observed_leads_delta(observed, action)):
                    db_module.record_experiments([action_experiment(
                        action, observed, window, outcome,
                        control=holdout_control(
                            holdout_facts,
                            _baseline_window(action.get("red_line") or {}),
                            (window[0], window[1])))])
                    closed_held.append({
                        "action_id": action.get("action_id"),
                        "object_id": action.get("object_id"),
                        "action_kind": action.get("action_kind"),
                        "verdict": outcome,
                        "observed": observed,
                    })
                    closed_verdicts[outcome] = closed_verdicts.get(outcome, 0) + 1
            except Exception as exc:
                errors.append({"action_id": action.get("action_id"),
                               "object_id": action.get("object_id"),
                               "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue

        if state != STATE_BREACHED:
            continue

        breached.append({
            "action_id": action.get("action_id"),
            "object_id": action.get("object_id"),
            "action_kind": action.get("action_kind"),
            "reason": verdict.get("reason"),
            "observed": verdict.get("observed"),
        })

        # Вердикт «вредно» фиксируется в журнале ДО попытки возврата и
        # независимо от её исхода: признак вредности сегмента — пробитая
        # красная линия, а не успешность технической операции отката. Из этой
        # отметки планировщик берёт кулдаун (writer_db.harmful_segments), и
        # без неё неоткатанный пробой был бы для него невидим: процент
        # дрейфует между расчётами, ключ идемпотентности получается новый, и
        # агент назавтра подкрутил бы тот же сегмент ещё раз.
        if rollback_allowed and journal_allowed(client):
            try:
                db_module.mark_harmful_verdict(
                    action["action_id"], str(verdict.get("reason") or "")[:500])
            except Exception as exc:
                errors.append({"action_id": action.get("action_id"),
                               "object_id": action.get("object_id"),
                               "error": f"{type(exc).__name__}: {exc}"[:200]})

        if not rollback_allowed:
            # Смотрим, но не откатываем: линия пробита по данным, качеству
            # которых доверять нельзя.
            blocked_by_gate.append({"action_id": action.get("action_id"),
                                    "object_id": action.get("object_id"),
                                    "reason": DATA_GATE_REASON,
                                    "gate": (data_gate or {}).get("reason")})
            continue

        try:
            # Откат — тоже запись в кабинет, и аренда прогона перед ней
            # перепроверяется по тому же правилу, что и прямое применение:
            # два сторожа откатят одно действие дважды и дважды спишут
            # попытки.
            if lease is not None:
                lease.guard()
            outcome = rollback_one(client, action, db_module, holdout_ids)
        except writer_db.RunLeaseLost:
            raise
        except Exception as exc:
            errors.append({"action_id": action.get("action_id"),
                           "object_id": action.get("object_id"),
                           "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        if outcome["result"] == "rolled_back":
            rolled_back += 1
            # Пробой — тоже исход, и он тоже пополняет историю: без этой
            # записи агент учился бы только на выдержавших изменениях.
            if journal_allowed(client):
                try:
                    window = observation_window(action, today, crm_through)
                    if window is not None:
                        db_module.record_experiments([action_experiment(
                            action, verdict.get("observed") or {},
                            window, "breached",
                            control=holdout_control(
                                holdout_facts,
                                _baseline_window(action.get("red_line") or {}),
                                (window[0], window[1])))])
                except Exception as exc:
                    errors.append({"action_id": action.get("action_id"),
                                   "object_id": action.get("object_id"),
                                   "error": f"{type(exc).__name__}: {exc}"[:200]})
        elif outcome["result"] == "rollback_failed":
            failures.append(outcome)
        elif outcome["result"] == "blocked_holdout":
            blocked_holdout.append(outcome)
        elif outcome["result"] == "conflicted":
            conflicted.append(outcome)
        else:
            would_roll_back.append(outcome)

    return {
        "under_watch": len(actions),
        # Эффект такта целиком — одно число на всё сегодняшнее решение, а не
        # четыреста частных вердиктов, каждый из которых на своём объёме
        # означает шум. Считается всегда, в том числе при красном гейте:
        # замер ничего не пишет в кабинет, а знать, что делал такт двухнедельной
        # давности, нужно тем сильнее, чем хуже данные.
        "tact_effect": tact_effect_report(actions, facts_by_campaign,
                                          holdout_facts, holdout_ids, today),
        "states": dict(sorted(states.items())),
        "breached": len(breached),
        "breached_sample": breached[:PREVIEW_SAMPLE_LIMIT],
        "rolled_back": rolled_back,
        "would_roll_back": len(would_roll_back),
        "rollback_failed": len(failures),
        "failures": [{k: v for k, v in f.items() if k != "result"}
                     for f in failures[:PREVIEW_SAMPLE_LIMIT]],
        "blocked_holdout": len(blocked_holdout),
        # Возврат прошёл в кабинете, а отметку в журнале уже поставил другой
        # контур. Не «откатано» и не «не удалось» — отдельное состояние,
        # которое сверяют руками.
        "conflicted": len(conflicted),
        "conflicted_sample": [{k: v for k, v in c.items() if k != "result"}
                              for c in conflicted[:PREVIEW_SAMPLE_LIMIT]],
        # Пробои, по которым прогон намеренно ничего не сделал: гейт данных
        # красный. Отдельный счётчик, а не тишина, — иначе «сторож молчит»
        # неотличимо от «всё хорошо».
        "blocked_data_gate": len(blocked_by_gate),
        "blocked_data_gate_sample": blocked_by_gate[:PREVIEW_SAMPLE_LIMIT],
        # Чистый исход (Э2.4): горизонт закрыт, линия не пробита — изменение
        # выдержало, записано в историю экспериментов и снято с наблюдения.
        "closed_held": len(closed_held),
        # Распределение исходов закрытых наблюдений: «сколько выдержало» —
        # не ответ, пока не видно, сколько из них на самом деле навредило.
        "closed_verdicts": dict(sorted(closed_verdicts.items())),
        "closed_held_sample": closed_held[:PREVIEW_SAMPLE_LIMIT],
        # То же в репетиции: закрыл бы, но журнал репетиции недоступен.
        "would_close_held": len(would_close_held),
        # Строки, снятые с наблюдения без вердикта: горизонт истёк или красной
        # линии не было вовсе. Изменение живёт в кабинете непроверенным и ждёт
        # человека — молчать о таких нельзя.
        "needs_review": len(needs_review),
        "needs_review_sample": [{k: v for k, v in n.items() if k != "result"}
                                for n in needs_review[:PREVIEW_SAMPLE_LIMIT]],
        # Строки без вердикта, которые прогон НАМЕРЕННО не закрыл: гейт
        # витрины красный, и «вердикта не будет никогда» на мёртвых данных
        # неотличимо от «данных не было». Останутся под наблюдением до
        # зелёного гейта — но в отчёте видны отдельным счётчиком.
        "closing_deferred": len(deferred_closing),
        "closing_deferred_sample": deferred_closing[:PREVIEW_SAMPLE_LIMIT],
        # Строки, которые прогон не смог даже рассудить: испорченные данные
        # журнала. Не «ноль пробитых», а явный отдельный счётчик.
        "errors": len(errors),
        "errors_sample": errors[:PREVIEW_SAMPLE_LIMIT],
    }


def facts_window(actions: List[Dict[str, Any]], today: date,
                 crm_through: Optional[date]) -> Optional[Tuple[date, date]]:
    """Общий отрезок дат, покрывающий окна наблюдения всех действий.

    Один запрос к фактам на прогон вместо запроса на действие; окно каждого
    действия вырезается из этих строк по его собственным границам.
    """
    windows = [observation_window(a, today, crm_through) for a in actions]
    windows = [w for w in windows if w]
    if not windows:
        return None
    return min(w[0] for w in windows), max(w[1] for w in windows)


def load_facts(actions: List[Dict[str, Any]], today: date,
               crm_through: Optional[date]) -> Dict[str, List[Dict[str, Any]]]:
    """Факты по наблюдаемым кампаниям — до СЕГОДНЯ, а не до конца окон.

    Верхняя граница запроса шире верхней границы наблюдения намеренно: по
    этим же строкам гейт (facts_gate) судит о свежести витрины, а последний
    её день лежит за границей окна — окно отодвинуто от сегодня на запас под
    лаг лидов. Запрашивай мы ровно окно — «витрина мертва неделю» и «мы
    спросили только про старые дни» выглядели бы одинаково. На вердикты
    лишние дни не влияют: observed_metrics режет строки по своему окну.
    """
    span = facts_window(actions, today, crm_through)
    if span is None:
        return {}
    # Нижняя граница отодвинута на ДВА горизонта назад: замер такта целиком
    # (tact_effect_report) сравнивает окно наблюдения с равным ему окном ДО
    # такта, а окна наблюдения начинаются днём применения. Спрашивай мы ровно
    # их — базы у такта не было бы никогда, и замер честно отказывал бы
    # «мерить нечем» каждый прогон. Лишние дни на вердикты действий не влияют:
    # observed_metrics режет строки по собственному окну.
    earliest = min(span[0], today - timedelta(days=2 * OBSERVATION_HORIZON_DAYS))
    rows = agent_db.load_daily_facts(
        [str(a.get("object_id")) for a in actions], earliest.isoformat(), today.isoformat())
    out: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row["campaign_id"]), []).append(row)
    return out


def load_account_totals(actions: List[Dict[str, Any]], today: date,
                        crm_through: Optional[date]) -> List[Dict[str, Any]]:
    """Дневные агрегаты ВСЕЙ витрины — контроль для сезонной поправки.

    Охват шире окна наблюдений: базовый CPA красной линии снят до применения
    действия (agent_e1.build_red_line), и его окно тоже должно попасть в
    выборку. Запрос дешёвый — по одной строке на день, а не на кампанию.
    """
    span = facts_window(actions, today, crm_through)
    if span is None:
        return []
    earliest = min(span[0], (today - timedelta(days=SEASONAL_LOOKBACK_DAYS)))
    return agent_db.load_daily_account_totals(
        earliest.isoformat(), today.isoformat())


def load_holdout_facts(actions: List[Dict[str, Any]], holdout_ids: Iterable[str],
                       today: date,
                       crm_through: Optional[date]) -> List[Dict[str, Any]]:
    """Дневные факты заповедника — контроль за то же окно (класс A).

    Охват шире окон наблюдения по той же причине, что у load_account_totals:
    база красной линии снята до применения действия, и разность разностей
    сравнивает именно эти два окна.

    Строки возвращаются плоским списком, а не словарём по кампаниям:
    контроль — это заповедник ЦЕЛИКОМ, отдельная кампания в нём слишком
    мелкая, чтобы служить эталоном.
    """
    span = facts_window(actions, today, crm_through)
    ids = sorted({str(i) for i in holdout_ids or ()})
    if span is None or not ids:
        return []
    earliest = min(span[0], (today - timedelta(days=SEASONAL_LOOKBACK_DAYS)))
    return agent_db.load_daily_facts(ids, earliest.isoformat(), today.isoformat())


def load_mart_breadth(actions: List[Dict[str, Any]], today: date,
                      crm_through: Optional[date]) -> Dict[str, Any]:
    """Ширина витрины по дням — отдельным дешёвым запросом по ВСЕЙ витрине.

    Верхняя граница та же, что у load_facts, и по той же причине: свежесть
    витрины видна только за границей окна наблюдения, отодвинутой от сегодня
    на запас под лаг лидов.

    Список действий нужен только чтобы узнать НИЖНЮЮ границу окна — фильтра
    по их кампаниям здесь нет и быть не должно: и знаменатель покрытия, и
    ширина гейта описывают состояние витрины, а не открутку наблюдаемых
    кампаний.
    """
    span = facts_window(actions, today, crm_through)
    if span is None:
        return {"days": {}, "campaigns_total": 0}
    return agent_db.load_mart_day_breadth(span[0].isoformat(), today.isoformat())


def refusal(sandbox: bool, dry_run: bool) -> Optional[str]:
    """Причина не начинать прогон вовсе, или None.

    «Песочница + запись» выглядит безопасной репетицией и потому будет первым,
    что попробует оператор. На деле песочница боевым логинам НЕДОСТУПНА: любой
    вызов отвечает «логин не подключен», исключение ловится, попытка отката
    засчитывается неудачной — и запись об этом уходит в БОЕВОЙ журнал, потому
    что база одна. После MAX_ROLLBACK_ATTEMPTS таких прогонов действие
    снимается с наблюдения навсегда: изменение остаётся живым в кабинете, а
    красная линия по нему не сработает уже никогда. То есть самый безопасный
    на вид режим — единственный, который разоружает боевые красные линии.
    """
    if sandbox and not dry_run:
        return SANDBOX_APPLY_REFUSAL
    return None


def main() -> int:
    sandbox = "--prod" not in sys.argv
    dry_run = "--apply" not in sys.argv
    today = date.today()

    # Отказ ДО первого обращения к БД: боевой журнал не должен получить ни
    # строчки от прогона, который физически не может ничего откатить.
    refused = refusal(sandbox, dry_run)
    if refused:
        print(json.dumps({"verdict": "REFUSED", "sandbox": sandbox,
                          "dry_run": dry_run, "reason": refused},
                         ensure_ascii=False, indent=2))
        return 2

    writer_db.ensure_writer_tables()

    # Аренда на прогон — ОБЩАЯ с прямым применением ("agent_writer"): оба
    # пишут в один кабинет и один журнал. Два одновременных сторожа
    # откатывают одно действие дважды и дважды списывают попытки
    # (MAX_ROLLBACK_ATTEMPTS = 3 — двух параллельных прогонов хватает,
    # чтобы похоронить строку за один заход); e1, применяющий изменения,
    # пока сторож их откатывает, — та же гонка с другого конца.
    try:
        with writer_db.run_lock("agent_writer") as lease:
            return _run_all(sandbox, dry_run, today, lease,
                            fail_on_alarm="--fail-on-alarm" in sys.argv)
    except writer_db.RunLockBusy as exc:
        print(json.dumps({"verdict": "RUN_LOCKED", "reason": str(exc)},
                         ensure_ascii=False, indent=2))
        return 1
    except writer_db.RunLeaseLost as exc:
        print(json.dumps({"verdict": "RUN_LEASE_LOST", "reason": str(exc)},
                         ensure_ascii=False, indent=2))
        return 1


# Порог зависшей строки — тот же час, что у e1 (STALE_PLANNED_MINUTES) и у
# срока аренды: процесс, молчащий дольше, по общему критерию считается мёртвым.
STALE_SWEEP_MINUTES = 60

# Минимум дней наблюдения для ДОСРОЧНОГО закрытия. Полная неделя, потому что
# недельный ритм рекламы (будни против выходных) сам по себе даёт разницу,
# которую легко принять за эффект действия.
EARLY_CLOSE_MIN_DAYS = 7

# Второй чекпоинт: через столько дней после ПРИМЕНЕНИЯ вердикт по заявкам
# сверяется с деньгами. 35 — потому что к этому дню дозревают 91 % оплат
# (лаг CRM, замер направления). Первый чекпоинт закрывает наблюдение рано и
# этим возвращает темп; второй не даёт превратить темп в самообман: заявка
# подешевела, а оплата подорожала — обычный исход, и без сверки он уходит в
# обучающую историю как успех.
MONEY_CHECKPOINT_DAYS = 35

# Потолок сезонной поправки красной линии. Кабинет дорожает и дешевеет вместе
# с рынком, и порог обязан двигаться следом — иначе перелом сезона устраивает
# массовые ложные пробои и записывает «действия вредны» ровно там, где
# подорожал аукцион (аудит 2026-08-23). Но безграничная нормировка позволила
# бы «сезоном» оправдать любой рост: контроль сам дёргается, а красная линия —
# последняя защита боевого кабинета.
SEASONAL_CAP = 1.5

# Минимум эффективных лидов в КАЖДОМ контрольном окне, чтобы поправка вообще
# считалась: на десятке лидов кабинетный CPA — шум, и поправка по нему сдвинула
# бы порог случайным образом.
SEASONAL_MIN_LEADS = 20

# На сколько дней назад заведомо хватает контроля: база красной линии снята за
# 30 дней до применения (agent_e1), плюс горизонт наблюдения и лаг лидов.
SEASONAL_LOOKBACK_DAYS = 75


def alarm_reasons(out: Dict[str, Any]) -> List[str]:
    """Состояния прогона, требующие человека. Пусто — тревоги нет.

    Крон-письмо GitHub приходит только на красный ран, поэтому каждая из
    этих находок обязана уметь уронить прогон (--fail-on-alarm): тревога,
    не влияющая на код выхода, — молчание.
    """
    reasons: List[str] = []
    if int(out.get("needs_manual_rollback") or 0) > 0:
        reasons.append(f"неоткатываемых вручную: {out['needs_manual_rollback']}")
    if str((out.get("data_gate") or {}).get("status")) != "GREEN":
        reasons.append("гейт данных не зелёный — откаты заморожены")
    # Заявки сказали одно, деньги — другое. Не аварийная ситуация, но
    # молчать о ней нельзя: на этих вердиктах учится портфель, и «подешевела
    # заявка, подорожала оплата» обязано дойти до человека, а не осесть
    # строкой в журнале.
    contradictions = (out.get("money_checkpoint") or {}).get("contradictions") or []
    if contradictions:
        reasons.append(
            f"вердикт по деньгам разошёлся с вердиктом по заявкам: {len(contradictions)}")
    if int((out.get("stale_marked") or {}).get("count") or 0) > 0:
        reasons.append(f"зависших строк помечено: {out['stale_marked']['count']}")
    for account in out.get("accounts") or []:
        login = account.get("account") or "?"
        if int(account.get("rolled_back") or 0) > 0:
            reasons.append(f"{login}: авто-откатов {account['rolled_back']}")
        if int(account.get("rollback_failed") or 0) > 0:
            reasons.append(f"{login}: неудачных откатов {account['rollback_failed']}")
        if account.get("errors"):
            reasons.append(f"{login}: ошибок наблюдения {len(account['errors'])}")
        if int(account.get("needs_review") or 0) > 0:
            reasons.append(f"{login}: строк без вердикта {account['needs_review']}")
    return reasons


def money_checkpoint(db_module: Any, today: date, crm_through: Optional[date],
                     journal_ok: bool) -> Dict[str, Any]:
    """Второй чекпоинт: вердикт закрытых действий сверяется с оплатами.

    Замыкает то, что первый чекпоинт открыл. Досрочное закрытие судит по
    заявкам на седьмой день — это темп; здесь, на тридцать пятый, то же самое
    окно пересчитывается по оплатам, которые к этому дню дозрели на 91 %
    (edu_agent_facts зачисляет оплату в день СОЗДАНИЯ лида, поэтому окно не
    надо сдвигать — оно само наполняется задним числом).

    Расхождение вердиктов — не ошибка и не повод откатывать: изменение живёт
    месяц, откатывать его вслепую вреднее, чем оставить. Это повод записать
    правду в историю обучения и показать её человеку. Без сверки история
    наполняется «успехами», подтверждёнными только заявками, — тот же дефект,
    что аудит поймал у бинарного вердикта.

    Репетиция журнал не трогает: сверка — утверждение о боевом кабинете.
    """
    due = db_module.actions_awaiting_money_check(MONEY_CHECKPOINT_DAYS)
    out: Dict[str, Any] = {"due": len(due), "checked": 0, "verdicts": {},
                           "contradictions": [], "skipped_no_baseline": 0}
    if not due:
        return out
    facts = load_facts(due, today, crm_through)
    for action in due:
        # Выборка журнала сужает грубо (интервал считает БД), а правило
        # созревания живёт здесь: одна проверка на обеих сторонах границы
        # надёжнее, чем доверие к тому, что интервал посчитан тем же числом.
        if not money_check_due(action, today):
            continue
        window = observation_window(action, today, crm_through)
        if window is None:
            continue
        rows = facts.get(str(action.get("object_id"))) or []
        money = money_metrics(rows, window)
        verdict = money_verdict(money, action)
        if verdict == "unknown":
            out["skipped_no_baseline"] += 1
        by_leads = str(action.get("observation_verdict") or "")
        # Противоречие — только между ОПРЕДЕЛЁННЫМИ исходами: «неясно» с
        # любой стороны означает, что сравнивать нечего, а не что вердикты
        # разошлись.
        if (by_leads in ("improved", "worsened") and verdict in ("improved", "worsened")
                and by_leads != verdict):
            out["contradictions"].append({
                "action_id": action.get("action_id"),
                "object_id": action.get("object_id"),
                "by_leads": by_leads, "by_money": verdict,
                "cpo": money.get("cpo"),
                "baseline_cpo": (action.get("red_line") or {}).get("baseline_cpo"),
                "payments": money.get("payments"),
            })
        if not journal_ok:
            continue
        if db_module.mark_money_checked(action.get("action_id"), verdict):
            out["checked"] += 1
            out["verdicts"][verdict] = out["verdicts"].get(verdict, 0) + 1
    return out


def settle_hypotheses(today: date, journal_ok: bool = True) -> Dict[str, Any]:
    """Проход по открытым ставкам реестра: какие закрылись сегодня.

    Живёт в стороже, а не в такте записи, по той же причине, по которой здесь
    же считаются исходы действий: ставка закрывается ЗАМЕРОМ, а замер делает
    сторож. Держать это в Э1 значило бы судить гипотезу в прогоне, который
    приехал её ставить.

    Судья один — economic_outcome. Реестр вердикт не пересчитывает, а переводит
    в свой статус (experiments.settle): два независимых судьи разошлись бы на
    первом же пограничном случае и разошлись бы молча.

    journal_ok=False (репетиция) — считаем и печатаем, в базу не пишем.
    """
    rows = agent_db.load_open_hypotheses(experiments.OPEN_STATUSES)
    moved: List[Dict[str, Any]] = []
    illegal: List[Dict[str, Any]] = []
    lost_race = 0
    for row in rows:
        try:
            step = experiments.settle(row, today)
        except experiments.IllegalTransition as exc:
            # Не проглатываем: недопустимый переход означает, что в реестре
            # лежит состояние, которого жизненный цикл не предусматривает, и
            # молчание здесь пряталось бы до разбора через две недели.
            illegal.append({"experiment_id": str(row.get("experiment_id")),
                            "status": str(row.get("status")),
                            "error": str(exc)[:200]})
            continue
        if step is None:
            continue
        if journal_ok and not agent_db.move_hypothesis(
                step["experiment_id"], step["from_status"], step["status"],
                step["reason"], experiments.CLOSED_STATUSES):
            # Строку уже забрал другой прогон. Не ошибка: исход записан,
            # просто не нами.
            lost_race += 1
            continue
        moved.append(step)

    by_status: Dict[str, int] = {}
    for step in moved:
        by_status[step["status"]] = by_status.get(step["status"], 0) + 1
    out = {
        "open_before": len(rows),
        "moved": len(moved),
        "by_status": by_status,
        "still_open": len(rows) - len(moved),
        "sample": moved[:PREVIEW_SAMPLE_LIMIT],
    }
    if lost_race:
        out["lost_race"] = lost_race
    if illegal:
        out["illegal_transitions"] = illegal
    return out


def _run_all(sandbox: bool, dry_run: bool, today: date, lease: Any = None,
             fail_on_alarm: bool = False) -> int:
    # Уборка зависших — ГЛОБАЛЬНО и ДО чтения открытых действий: помечать
    # их умел только e1, и только по своему логину — кабинет, по которому
    # e1 больше не запускают, копил planned-строки вечно. Свежепомеченные
    # ('stale') попадают под наблюдение этим же прогоном. Репетиция журнал
    # не трогает.
    stale_found: List[Dict[str, Any]] = []
    if not dry_run:
        stale_found = writer_db.mark_stale_planned(STALE_SWEEP_MINUTES)

    actions = writer_db.open_actions()
    holdout_ids = {str(h) for h in agent_db.load_holdout_ids()}
    # Граница зрелости CRM — один запрос на прогон. Дальше неё лежат дни, где
    # расход Директа уже приехал, а лиды ещё нет: 19.08.2026 — 927 945 рублей
    # и НОЛЬ лидов. Судить по таким дням значит откатывать здоровое.
    crm_through = agent_db.crm_maturity_date()
    facts_by_campaign = load_facts(actions, today, crm_through)
    # Полнота данных — по витрине, наблюдаемые метрики — по кампаниям
    # действий. Два разных запроса намеренно: см. mart_filled_days.
    mart = load_mart_breadth(actions, today, crm_through)
    # Кабинетный контроль для сезонной поправки красных линий: дневные
    # агрегаты ВСЕЙ витрины за тот же охват, одним дешёвым запросом. Окно
    # шире охвата наблюдений — базы линий лежат до применения действий.
    account_totals = load_account_totals(actions, today, crm_through)
    # Заповедник — контроль ЗА ТО ЖЕ ОКНО: из него исход собственного
    # действия получает класс надёжности A. Кабинетные агрегаты для этого не
    # годятся: в них сидят и кампании, которые агент как раз двигал.
    holdout_facts = load_holdout_facts(actions, holdout_ids, today, crm_through)
    # Поверх витринного гейта — сверка сумм источник↔витрина: битая
    # сборка не видна ни свежести, ни ширине. Аномалия объёма сторожа
    # НЕ гейтует (include_volume=False): обвал расхода — возможное
    # следствие вредного изменения, откат по нему замораживать нельзя.
    gate = with_source_checks(facts_gate(mart, today),
                              include_volume=False)

    by_account: Dict[str, List[Dict[str, Any]]] = {}
    for action in actions:
        by_account.setdefault(str(action.get("account") or ""), []).append(action)

    accounts: List[Dict[str, Any]] = []
    for login, account_actions in sorted(by_account.items()):
        client = WriteClient(login, sandbox=sandbox, dry_run=dry_run)
        report = watch(client, account_actions, writer_db, holdout_ids,
                       facts_by_campaign, today, gate, crm_through, lease, mart,
                       account_totals, holdout_facts)
        accounts.append({"account": login, **report, "units_left": client.units_left})

    out = {
        "sandbox": sandbox,
        "dry_run": dry_run,
        "today": today.isoformat(),
        "data_gate": gate,
        "crm_through": crm_through.isoformat() if crm_through else None,
        "crm_lag_days": (today - crm_through).days if crm_through else None,
        "observation": {
            "lag_days": OBSERVATION_LAG_DAYS,
            "horizon_days": OBSERVATION_HORIZON_DAYS,
            "tail_days": OBSERVATION_TAIL_DAYS,
            "leads_lag_days": LEADS_LAG_DAYS,
            "min_window_coverage": MIN_WINDOW_COVERAGE,
            # Знаменатель покрытия — по всей витрине, а не по наблюдаемым
            # кампаниям. В отчёте видно, по скольким дням он считался.
            "mart_days_in_window": len((mart or {}).get("days") or {}),
            "mart_campaigns_total": (mart or {}).get("campaigns_total"),
        },
        "under_watch": len(actions),
        "stale_marked": {"count": len(stale_found),
                         "sample": stale_found[:PREVIEW_SAMPLE_LIMIT]},
        "accounts": accounts,
        # Изменения, живые в кабинете и неоткатываемые автоматически: их
        # разбирает человек. Число накопительное, а не за прогон, — иначе
        # находка прошлого прогона исчезала бы из виду.
        "needs_manual_rollback": writer_db.failed_rollbacks_count(),
        # Второй чекпоинт — сверка закрытых вердиктов с деньгами. Стоит
        # отдельным блоком, а не внутри кабинета: строка сверяется по своему
        # возрасту, а не по тому, чей кабинет попал в этот прогон.
        "money_checkpoint": money_checkpoint(writer_db, today, crm_through,
                                             journal_ok=not dry_run),
        # Реестр гипотез — тот же проход, что и по действиям, но по ЗАМЫСЛАМ:
        # ставка закрывается вердиктом сторожа, откатом по красной линии или
        # истёкшим горизонтом. Без этого блока «сразу увидеть результат и
        # сразу двигаться дальше» некуда возвращаться.
        "hypotheses": settle_hypotheses(today, journal_ok=not dry_run),
    }
    out["alarms"] = alarm_reasons(out)
    # Вердикт — в отчёт, а не только в код выхода: save_run читает его из
    # report["verdict"], и без этой строки сторож ложился в историю пустым.
    # Тревога, видимая лишь в логе прогона, живёт до следующего прогона.
    out["verdict"] = "ALARM" if out["alarms"] else "GREEN"
    # Сторож — третья часть той же истории: расчёт решил, запись применила,
    # сторож увидел исход. В чёрном ящике они лежат рядом, иначе разбор
    # инцидента снова собирается из трёх логов разной свежести.
    out["blackbox"] = blackbox.save_run(
        blackbox.new_run_id(), stage="watchdog",
        mode=blackbox.run_mode(sandbox, dry_run), report=out)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if fail_on_alarm and out["alarms"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
