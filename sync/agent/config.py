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

# Виды значений, которые не описываются диапазоном чисел или перечнем строк.
# Заведены здесь, а не проверкой по имени ключа: имя проверяется в одном месте,
# а вид — в двух (валидация и подпись), и разъехаться им нельзя.
KIND_LANE_STEPS = "lane_steps"   # {полоса: ступень}
KIND_LANE_LIST = "lane_list"     # [полоса, полоса]
KIND_TEXT_LIST = "text_list"     # [подстрока, подстрока]

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
    "lane_steps": {
        # Ступень полосы вручную. Обычно её выдаёт лестница автономии по
        # послужному списку (writer/lanes.steps_by_lane), и этот ключ —
        # единственный способ её перебить: им ВЫПУСКАЮТ полосу из тени и им же
        # сажают обратно, не дожидаясь, пока накопленная история переварит
        # свежий провал. Пусто — лестница решает сама.
        "default": None, "nullable": True, "kind": KIND_LANE_STEPS,
        "about": "ступень полосы вручную: 0 — тень, 1–3 — доля недельного "
                 "расхода на риск полосы",
    },
    "shadow_lanes": {
        # Полосы, которые человек держит в ТЕНИ: они пишут намерения в журнал
        # и не применяют ничего. Список приходит настройкой, а не константой
        # кода, ровно потому, что выпуск рычага — решение человека, и оно не
        # должно требовать правки кода и деплоя.
        "default": None, "nullable": True, "kind": KIND_LANE_LIST,
        "about": "полосы на приёмке: пишут «сделал бы X, жду Y», ничего не "
                 "применяя",
    },
    "placement_blocklist": {
        # Чёрный список площадок владельца: подстроки домена или bundle id
        # («dsp-», «vpn», «.games»). Площадка, имя которой содержит любую из
        # них, запрещается БЕЗ статистики — это решение человека о качестве
        # трафика (Павел, 02.09.2026: DSP-обменники и VPN-приложения дают
        # спам-лиды, которые конверсиями Директа выглядят живыми, и
        # статистический критерий их защищает). Ворота — топ-N по расходу
        # (placement_blocklist_top_n): без них хвост из тысяч копеечных
        # матчей забил бы лимит кабинета (1000 слотов на кампанию) за неделю.
        "default": None, "nullable": True, "kind": KIND_TEXT_LIST,
        "about": "подстроки имён площадок, которые режутся без статистики "
                 "(решение владельца); действует только на топ по расходу — "
                 "см. placement_blocklist_top_n",
    },
    "placement_blocklist_top_n": {
        "default": 30, "min": 1, "max": 200,
        "about": "чёрный список площадок смотрит только на топ-N по расходу "
                 "за окно: защита лимита кабинета (1000 слотов) от "
                 "копеечного хвоста",
    },
    "idea_max_horizon_days": {
        # Ручка объявлена в ideas/limits.py («решать должен человек в
        # настройках»), но до 01.09.2026 в SPEC не значилась — задать её было
        # нельзя: валидация роняла прогон на «неизвестном параметре».
        "default": 90, "min": 30, "max": 365,
        "about": "предел срока идеи в днях: эксперимент дольше — соревнуется "
                 "с сезоном, а не с гипотезой",
    },
    "consolidate_min_step_events": {
        # Порог ступени лестницы ТОЛЬКО для групп выноса (consolidate).
        # Общий MIN_STEP_EVENTS=25 (ошибка счётчика 20 %) остаётся судьёй
        # остальных решений; вынос собирает фразы, УЖЕ доказанные деньгами
        # доноров (objects.expansion_candidates), кампания создаётся на паузе
        # и включается человеком — здесь владелец вправе принять оценку
        # грубее (10 событий — ошибка 32 %).
        "default": 25, "min": 5, "max": 100,
        "about": "вынос связок: минимум событий на ступени воронки группы "
                 "доноров (25 — ошибка 20 %, 10 — 32 %)",
    },
    "consolidate_min_verdict_conversions": {
        # Сколько КОНВЕРСИЙ новая кампания должна набрать за горизонт, чтобы
        # вердикт (success_rule: cpa_rub против доноров) имел силу. Именно
        # конверсий: вердикт меряется ценой конверсии, и требовать вместо них
        # объёма оплат — события в ~70 раз более редкого — значило отсекать
        # вынос всегда (замер 01.09.2026: 0 идей при 126 конверсиях доноров).
        "default": 25, "min": 5, "max": 100,
        "about": "вынос связок: минимум конверсий новой кампании за горизонт "
                 "на вердикт по CPA (меньше — вердикт грубее)",
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
# Причина рядом с ключом, а не в комментарии: она уезжает в манифест
# и показывается человеку. Без неё отсутствие ручки читается как недоделка
# экрана, а не как утверждение «защиту нельзя ослабить».
LOCKED_ABOUT: Dict[str, str] = {
    "red_line_tolerance": "+40 % к базовому CPA — порог автоотката",
    "min_leads_for_verdict": "минимум наблюдений для вердикта",
    "spend_collapse_share": "объёмная линия: обвал расхода",
    "seasonal_cap": "потолок сезонной поправки порога",
    "risk_budget_week": "недельный риск-бюджет: меняется только человеком",
}

LOCKED_KEYS: Tuple[str, ...] = tuple(LOCKED_ABOUT)

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

    kind = spec.get("kind")
    if kind == KIND_LANE_STEPS:
        return _lane_steps(key, value)
    if kind == KIND_LANE_LIST:
        return _lane_list(key, value)
    if kind == KIND_TEXT_LIST:
        return _text_list(key, value)

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


def _known_lane(key: str, lane: Any) -> str:
    """Имя полосы, проверенное по перечню. Опечатка — ошибка, а не умолчание.

    Полоса с опечаткой молча не нашлась бы ни в одном месте: «suspand» в
    списке тени означал бы, что выключения продолжают применяться, а человек
    считает, что убрал их на приёмку.
    """
    from sync.agent.writer.lanes import ALL_LANES

    name = str(lane)
    if name not in ALL_LANES:
        raise ValueError(f"{key}: неизвестная полоса {lane!r}; "
                         f"есть {', '.join(ALL_LANES)}")
    return name


def _lane_steps(key: str, value: Any) -> Dict[str, int]:
    """{полоса: ступень} — обе стороны проверены по своим перечням."""
    from sync.agent.autonomy import STEPS

    if not isinstance(value, dict):
        raise ValueError(f"{key}={value!r}: нужен словарь «полоса: ступень»")
    allowed = {candidate.step for candidate in STEPS}
    out: Dict[str, int] = {}
    for lane, step in value.items():
        try:
            number = int(step)
        except (TypeError, ValueError):
            raise ValueError(f"{key}[{lane}]={step!r}: ступень — целое число")
        if number not in allowed:
            raise ValueError(
                f"{key}[{lane}]={step}: нет такой ступени; "
                f"есть {', '.join(str(s) for s in sorted(allowed))}")
        out[_known_lane(key, lane)] = number
    return out


def _lane_list(key: str, value: Any) -> List[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set)):
        raise ValueError(f"{key}={value!r}: нужен список полос")
    return sorted({_known_lane(key, lane) for lane in value})


def _text_list(key: str, value: Any) -> List[str]:
    """Список подстрок-паттернов: нижний регистр, без пустых и без пробелов.

    Паттерн короче двух символов — ошибка, а не умолчание: подстрока «a»
    совпала бы почти с каждым доменом, и один неловкий элемент списка
    превратил бы точечный запрет в ковровый.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set)):
        raise ValueError(f"{key}={value!r}: нужен список подстрок")
    out: List[str] = []
    for item in value:
        text = str(item or "").strip().lower()
        if len(text) < 2:
            raise ValueError(f"{key}: паттерн {item!r} короче 2 символов")
        if " " in text:
            raise ValueError(f"{key}: паттерн {item!r} содержит пробел — "
                             "домены и bundle id пробелов не содержат")
        if text not in out:
            out.append(text)
    return sorted(out)


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
