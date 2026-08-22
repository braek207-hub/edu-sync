# -*- coding: utf-8 -*-
"""Сторож свежести витрин: данные перестали приезжать, а прогон зелёный.

Зачем. 2026-08-02 обнаружилось, что crm_leads, crm_payments и crm_lead_details стоят
на 2026-07-29: лидов за 30, 31 июля и 1-2 августа нет вовсе, при норме 165-355 в день.
Синк при этом отчитывался успехом каждое утро и честно записывал 37 084 строки — ровно
то, что лежало в Google-таблице, а таблица перестала пополняться выше по течению.
Узнали случайно, на четвёртый день, при разборе совсем другой задачи.

Из этого два вывода, оба заложены здесь:

1. **Проверять надо результат в базе, а не код возврата прогона.** «Записано N строк»
   не значит «данные свежие»: N может быть большим и целиком историческим.
2. **Предупреждения в логе не работают.** Тот же класс поломки уже ловится
   `ecommerce_health.detect_ecommerce_drop`, пишет WARN — и WARN никто не читает.
   Поэтому сторож ПАДАЕТ: упавший workflow GitHub присылает письмо, лог — нет.

Две проверки, разные по природе:

- **свежесть** — насколько отстала самая поздняя дата в таблице. Ловит «поток
  оборвался»: ровно случай crm_leads выше;
- **объём** — сколько строк за позавчера против медианы предыдущей недели. Ловит
  «поток идёт, но половину потеряли». Позавчера, а не вчера: вчерашний день у
  части источников ещё дозаливается (замер 2026-08-02: у lime_stats за вчера 254
  строки против 590 за позавчера, у визитов Полины 504 против 1055).

Медиана, а не среднее — по той же причине, что в ecommerce_health: один уже
случившийся провал утащил бы среднее вниз вместе с порогом.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median


@dataclass(frozen=True)
class Source:
    """Витрина, за свежестью которой следим.

    Args:
        dashboard: слаг дашборда, которому эти данные видны.
        table: имя таблицы в Postgres.
        date_column: колонка календарной даты факта.
        max_lag_days: сколько дней отставания нормально. Считается от сегодняшней
            даты, поэтому 1 = «вчерашний день обязан быть».
        volume_floor: пока медиана недели ниже этого числа строк, объёмная проверка
            молчит. Малообъёмные витрины шумят: у Google Ads и товарных строк BJORN
            это единицы строк в день, где «минус половина» — обычное колебание.
        where: SQL-условие среза внутри таблицы (литерал из реестра, не ввод).
            Нужен, когда одну таблицу наполняют НЕЗАВИСИМЫЕ потоки: max(date) по всей
            lime_google_ads_stats был свежим за счёт KZ-скрипта, пока GCC-скрипт стоял
            месяц (встал ~2026-07-18, заметили 2026-08-19).
    """

    dashboard: str
    table: str
    date_column: str = "date"
    max_lag_days: int = 1
    volume_floor: int = 20
    where: str = ""

    @property
    def name(self) -> str:
        """Имя витрины в отчёте: таблица + срез, если следим за частью таблицы."""
        return f"{self.table} [{self.where}]" if self.where else self.table


@dataclass(frozen=True)
class SourceState:
    """Снимок витрины: самая поздняя дата и число строк по дням."""

    source: Source
    max_date: date | None
    rows_by_day: dict[date, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Finding:
    """Находка сторожа. `critical` роняет прогон, `warning` только печатается."""

    level: str  # "critical" | "warning"
    source: Source
    message: str

    @property
    def is_critical(self) -> bool:
        return self.level == "critical"


# Доля от медианы, ниже которой объём за день это уже не колебание. Порог тот же,
# что у detect_ecommerce_drop: половина — это уже не «выходной», это потеря.
VOLUME_RATIO = 0.5


def check_freshness(state: SourceState, today: date) -> Finding | None:
    """Отстала ли самая поздняя дата больше допустимого."""
    src = state.source
    if state.max_date is None:
        return Finding("critical", src, "таблица пуста — данных нет вовсе")

    lag = (today - state.max_date).days
    if lag <= src.max_lag_days:
        return None
    return Finding(
        "critical",
        src,
        f"самая поздняя дата {state.max_date} — отставание {lag} дн. "
        f"при допустимых {src.max_lag_days}",
    )


def check_volume(state: SourceState, today: date) -> Finding | None:
    """Не провалился ли объём за позавчера против медианы предыдущей недели."""
    src = state.source
    day = _days_ago(today, 2)
    baseline = [state.rows_by_day.get(_days_ago(today, i), 0) for i in range(3, 10)]
    baseline = [n for n in baseline if n > 0]
    if not baseline:
        return None

    base = median(baseline)
    if base < src.volume_floor:
        return None

    rows = state.rows_by_day.get(day, 0)
    if rows >= base * VOLUME_RATIO:
        return None

    # Ноль на фоне живой базовой линии — это дыра, а не просадка.
    level = "critical" if rows == 0 else "warning"
    return Finding(
        level,
        src,
        f"за {day} строк {rows} против медианы {base:.0f} за предыдущую неделю",
    )


def check_sources(states: list[SourceState], today: date) -> list[Finding]:
    """Обе проверки по всем витринам. Порядок находок — как у источников.

    Свежесть проверяется первой и, если сработала, объёмная не добавляется: у
    оборвавшегося потока объём провален по той же причине, и второе сообщение
    ничего не добавит к первому.
    """
    out: list[Finding] = []
    for state in states:
        stale = check_freshness(state, today)
        if stale is not None:
            out.append(stale)
            continue
        drop = check_volume(state, today)
        if drop is not None:
            out.append(drop)
    return out


def _days_ago(today: date, days: int) -> date:
    return today - timedelta(days=days)


# ============================================================
# Реестр витрин.
#
# max_lag_days выставлен по замеру состояния базы 2026-08-02 и расписанию синков,
# а не «на глаз»: где источник в этот день уже отдал сегодняшний день — 1 (вчера
# обязано быть), где отдаёт с суточной задержкой — 2.
# ============================================================
SOURCES: list[Source] = [
    # LIME. Витрина lime_stats и кабинеты обновляются несколько раз в день.
    Source("lime", "lime_stats"),
    Source("lime", "lime_direct_stats"),
    Source("lime", "lime_metrika_campaign_ru"),
    Source("lime", "lime_vk_ads_stats"),
    # Google Ads на 2026-08-02 отдавал максимум за вчера, не за сегодня.
    # KZ и GCC пишут РАЗНЫЕ Ads-скрипты из разных кабинетов → следим за каждым срезом,
    # иначе свежий KZ маскирует мёртвый GCC (см. docstring поля where).
    Source("lime", "lime_google_ads_stats", max_lag_days=2, where="region = 'kz'"),
    Source("lime", "lime_google_ads_stats", max_lag_days=2, where="region = 'gcc'"),
    # AppMetrica приезжает кроном 07:30 МСК.
    Source("lime", "lime_app_installs", max_lag_days=2),
    # EDU. Директ едет по API, CRM — через Google-таблицу (та самая, что встала).
    Source("edunetwork", "direct_stats"),
    Source("edunetwork", "crm_leads", max_lag_days=2),
    Source("edunetwork", "crm_payments", max_lag_days=2),
    Source("edunetwork", "crm_lead_details", date_column="created_date", max_lag_days=2),
    # BJORN. Витрина собирается ночью, поэтому суточное отставание — норма.
    Source("bjorn", "marketing_daily_fact", max_lag_days=2),
    Source("bjorn", "marketing_ecom_lines", max_lag_days=2),
    # Polina Repik.
    Source("polinarepik", "polinarepik_direct_stats"),
    Source("polinarepik", "polinarepik_metrica_visits"),
    Source("polinarepik", "polinarepik_metrica_sources"),
]
