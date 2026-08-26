# -*- coding: utf-8 -*-
"""
sync/agent/config.py — панель настроек агента.

Параметры, которыми регулируется темп и осторожность, живут в одном месте и
меняются без правки кода. Пресет задаёт всё разом; отдельный параметр можно
переопределить поверх пресета. Активный конфиг печатается в отчёт каждого
прогона вместе с ИСТОЧНИКОМ каждого значения — иначе «почему агент вдруг стал
резче» превращается в археологию по коммитам.

Три правила слоя:

  * **Защиту нельзя ослабить настройками.** Красная линия, порог наблюдений,
    объёмная линия обвала расхода в панель не входят (LOCKED_KEYS): первая же
    «агрессивная» настройка сняла бы ровно то, ради чего построен контур.
    Ужесточать их можно — правкой кода, осознанно и с тестом.
  * **Опечатка — ошибка, а не умолчание.** Неизвестный ключ или значение вне
    диапазона роняют разбор: молча проигнорированная настройка выглядит как
    применённая, и человек считает, что агент работает иначе, чем на самом деле.
  * **Дефолты равны константам кода.** Появление панели само по себе не меняет
    поведения — это проверяется тестом, а не обещанием.

Хранение — таблица edu_agent_config (key, value, preset, updated_at,
updated_by); смена настроек логируется, как и всякое действие агента.
"""

from typing import Any, Dict, List, Optional, Tuple

from sync.agent.portfolio import EXPLORATION_SHARE
from sync.agent.writer.budget import BUDGET_COOLDOWN_DAYS, MAX_WRITE_STEP
from sync.agent.writer.risk import DEFAULT_RISK_SHARE_WEEK
from sync.agent.writer.switch import MAX_SUSPENDS_PER_RUN

# Что регулируется. Значение по умолчанию = константа кода, диапазон — границы,
# внутри которых параметр остаётся осмысленным (а не «лишь бы не падало»).
#
# В панель попадает ТОЛЬКО то, что реально подключено к рычагам. Параметр,
# который ничего не меняет, хуже отсутствующего: он создаёт ложную уверенность,
# что настройка применена.
SPEC: Dict[str, Dict[str, Any]] = {
    "autonomy": {
        "default": "full",
        "choices": ("full", "suggest_only", "off"),
        "about": "применять действия / только предлагать / не работать",
    },
    "explore_share": {
        "default": EXPLORATION_SHARE, "min": 0.0, "max": 0.20,
        "about": "доля бюджета кабинета на разведку (карман неопределённости)",
    },
    "max_write_step": {
        "default": MAX_WRITE_STEP, "min": 0.05, "max": 0.50,
        "about": "потолок шага бюджета и цели за одну запись, доля от расхода",
    },
    "budget_cooldown_days": {
        "default": BUDGET_COOLDOWN_DAYS, "min": 3, "max": 30,
        "about": "сколько дней кампанию не трогают после правки бюджета/цели",
    },
    "max_suspends_per_run": {
        "default": MAX_SUSPENDS_PER_RUN, "min": 0, "max": 3,
        "about": "сколько кампаний можно выключить за один прогон",
    },
    "p_sign_bid": {
        "default": 0.80, "min": 0.70, "max": 0.99,
        "about": "порог уверенности для корректировок и расписания",
    },
    "p_sign_budget": {
        "default": 0.90, "min": 0.80, "max": 0.99,
        "about": "порог уверенности для сдвигов бюджета и целевого CPA",
    },
    "p_sign_state": {
        # Нижняя граница 0.95, а не 0.80: выключение кампании обратимо дорого
        # (рестарт обучения стратегии), и «агрессивный» пресет не должен
        # опускать её до уровня корректировок.
        "default": 0.97, "min": 0.95, "max": 0.999,
        "about": "порог уверенности для выключения кампании",
    },
    "risk_share_week": {
        # Верх 6 % — не «побольше на всякий случай»: при недельном расходе
        # кабинета 5,7 млн ₽ (замер 26.08.2026) это 342 000 ₽ под
        # непроверенными изменениями против 57 000 ₽ на дефолте. Выше —
        # уже не темп, а снятие того самого потолка, ради которого слой
        # построен. Ноль — законное «ничего не применять по риску»: это
        # решение человека, в отличие от нуля от пробела в витрине, где
        # weekly_limit падает на абсолютный дефолт.
        "default": DEFAULT_RISK_SHARE_WEEK, "min": 0.0, "max": 0.06,
        "about": "доля недельного расхода кабинета на риск одной полосы",
    },
    "target_romi": {
        "default": 1.0, "min": 1.0, "max": 5.0,
        "about": "требуемая окупаемость целевого CPA (1.0 — безубыточность)",
    },
    "monthly_budget_cap_rub": {
        # Единственный параметр, у которого пусто — законное состояние, а не
        # «ещё не настроили»: общая сумма кабинета это деньги владельца, и
        # пока он не назвал потолок, агент рост только предлагает числом
        # (portfolio.account_budget). Верхняя граница — не оценка кабинета, а
        # защита от опечатки в порядке величины: потолок в сотни миллионов за
        # месяц агент честно попытался бы освоить.
        "default": None, "min": 0.0, "max": 100_000_000.0, "nullable": True,
        "about": "потолок месячного освоения на кабинет; пусто — агент общий "
                 "бюджет не растит, только предлагает",
    },
}

DEFAULTS: Dict[str, Any] = {key: spec["default"] for key, spec in SPEC.items()}

# Пороги защиты. В панель не входят намеренно — перечислены, чтобы попытка
# их задать давала внятный отказ, а не «неизвестный параметр».
LOCKED_KEYS: Tuple[str, ...] = (
    "red_line_tolerance",       # +40 % к базовому CPA — порог автооткатa
    "min_leads_for_verdict",    # минимум наблюдений для вердикта
    "spend_collapse_share",     # объёмная линия: обвал расхода
    "seasonal_cap",             # потолок сезонной поправки порога
    "risk_budget_week",         # недельный риск-бюджет: меняется только человеком
)

PRESETS: Dict[str, Dict[str, Any]] = {
    "conservative": {
        "max_write_step": 0.15,
        "budget_cooldown_days": 21,
        "explore_share": 0.03,
        "max_suspends_per_run": 0,
        "p_sign_bid": 0.85,
        "p_sign_budget": 0.95,
        "p_sign_state": 0.99,
    },
    "balanced": dict(DEFAULTS),
    "aggressive": {
        "max_write_step": 0.30,
        "budget_cooldown_days": 7,
        "explore_share": 0.15,
        "max_suspends_per_run": 2,
        "p_sign_bid": 0.75,
        "p_sign_budget": 0.85,
        # Выключение кампании остаётся строгим и в агрессивном пресете.
        "p_sign_state": 0.95,
    },
}


def _validate(key: str, value: Any) -> Any:
    spec = SPEC.get(key)
    if spec is None:
        if key in LOCKED_KEYS:
            raise ValueError(
                f"{key} не переопределяется настройками: это порог защиты, "
                f"его ослабление снимает контур, ради которого агент построен")
        raise ValueError(f"неизвестный параметр: {key}")

    if value is None and spec.get("nullable"):
        # Пусто — это ответ «не задано», и он обязан проходить валидацию:
        # иначе параметр нельзя ни оставить пустым, ни вернуть в пустое.
        return None

    choices = spec.get("choices")
    if choices is not None:
        if value not in choices:
            raise ValueError(
                f"{key}={value!r} вне допустимого диапазона: {', '.join(choices)}")
        return value

    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key}={value!r} вне допустимого диапазона: нужно число")
    if number < spec["min"] or number > spec["max"]:
        raise ValueError(
            f"{key}={value} вне допустимого диапазона [{spec['min']}, {spec['max']}]")
    return type(spec["default"])(number) if isinstance(spec["default"], int) else number


def resolve(preset: Optional[str] = None,
            overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Активный конфиг: дефолты → пресет → переопределения.

    Неизвестный пресет, неизвестный ключ или значение вне диапазона роняют
    вызов: настройка, которую молча проигнорировали, опаснее отсутствующей.
    """
    if preset is not None and preset not in PRESETS:
        raise ValueError(
            f"неизвестный пресет: {preset}; есть {', '.join(sorted(PRESETS))}")
    config = dict(DEFAULTS)
    for key, value in (PRESETS.get(preset or "", {}) or {}).items():
        config[key] = _validate(key, value)
    for key, value in (overrides or {}).items():
        config[key] = _validate(key, value)
    return config


def describe(preset: Optional[str] = None,
             overrides: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Конфиг построчно с источником значения — для отчёта прогона.

    Без источника непонятно, что именно человек менял: «max_write_step 0.3» и
    «max_write_step 0.3, потому что выбран агрессивный пресет» — разные факты.
    """
    config = resolve(preset, overrides)
    preset_values = PRESETS.get(preset or "", {}) or {}
    given = overrides or {}
    rows: List[Dict[str, Any]] = []
    for key in sorted(SPEC):
        if key in given:
            source = "override"
        elif key in preset_values and preset_values[key] != DEFAULTS[key]:
            source = "preset"
        else:
            source = "default"
        rows.append({"key": key, "value": config[key], "source": source,
                     "about": SPEC[key]["about"]})
    return rows
