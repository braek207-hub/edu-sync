# -*- coding: utf-8 -*-
"""
sync/agent/manifest.py — машинное описание агента для интерфейса.

Экран «Карта агента» в Panda-BI обязан показывать, КАК устроена машина:
полосы и их ступени, классы достоверности, виды действий и их рычаги, панель
настроек с диапазонами, пороги гейта данных. Всё это уже объявлено в Python —
и повторять список в TypeScript нельзя.

**Почему нельзя — замер, а не опасение.** Зеркало панели настроек в Panda-BI
(`lib/admin/agent-report.ts::AGENT_CONFIG_SPEC`) писалось руками и разъехалось:
на 28.08.2026 в нём тринадцать ключей против тринадцати питоновских, но три
из них — `lane_steps`, `shadow_lanes`, `risk_share_week` — в зеркале
отсутствуют, а это ровно те ручки, которыми полосу выпускают из тени и задают
долю риска. Человек, глядя на экран, видел панель БЕЗ главных рычагов и делал
вывод, что их не существует.

Поэтому манифест СОБИРАЕТСЯ из тех же констант, по которым работает прогон, и
кладётся в базу (edu_agent_manifest). Экран читает выгрузку. Расхождение
становится невозможным по построению: список в манифесте — это и есть список,
по которому агент работает, а не его копия.

Что здесь НЕ живёт: числа прогонов, состояние кабинета, история. Манифест
описывает устройство, а не события; события лежат в журналах и читаются
отдельно.
"""

from typing import Any, Dict, List, Optional

from sync.agent import autonomy, config, gate
from sync.agent.guard import SUM_TOLERANCE
from sync.agent.writer import guardrails, lanes, tier

# Версия ФОРМАТА. Растёт, когда меняется структура (не содержимое): читателю
# на другой стороне нужно основание отказаться разбирать незнакомый формат, а
# не гадать по наличию ключей.
SCHEMA_VERSION = 1

# Как называется и что означает каждый класс достоверности. Тексты живут
# здесь, рядом с числами класса, а не в интерфейсе: класс — это правило
# отбора, и его формулировка обязана меняться вместе с правилом.
TIER_ABOUT: Dict[int, Dict[str, str]] = {
    tier.TIER_ARITHMETIC: {
        "title": "арифметика",
        "about": "утверждение о прошлом: расход без конверсий при зрелом окне. "
                 "Риском не платит — вырезает трату, а не создаёт её",
    },
    tier.TIER_MEASURED: {
        "title": "замеренное",
        "about": "перенос измеренной разницы: сегмент/время/площадка с "
                 "накопленной статистикой",
    },
    tier.TIER_BET: {
        "title": "ставка",
        "about": "гипотеза с горизонтом и критерием успеха; исход закрывает "
                 "наблюдение",
    },
    tier.TIER_PROPOSAL: {
        "title": "предложение",
        "about": "рычага записи нет: текст человеку. В кабинет не уходит ни "
                 "при какой ступени",
    },
}

LANE_ABOUT: Dict[str, str] = {
    lanes.LANE_HYGIENE: "вырезает заведомо пустой расход: минус-фразы, площадки",
    lanes.LANE_TUNING: "крутит сегменты: корректировки, расписание, аудитории",
    lanes.LANE_ALLOCATION: "двигает деньги и цели: бюджет, tCPA, стратегия, гео",
    lanes.LANE_SUSPEND: "выключает кампанию целиком",
    lanes.LANE_EXPLORATION: "тратит карман неопределённости: проверка того, "
                            "чего мы не знаем",
    lanes.LANE_LAUNCH: "заводит новое: наряд билдеру и возврат из паузы",
    lanes.LANE_PROPOSAL: "пишет человеку, ничего не применяя",
}

# Такт целиком: узлы и их связи. Единственная часть манифеста, объявленная
# перечислением, — потому что порядок стадий нигде в коде не выражен одним
# объектом: он собран из расписания воркфлоу и вызовов внутри прогонов.
# Каждый узел назван МОДУЛЕМ, и это проверяется тестом: узел, потерявший свой
# модуль, — это стадия, которую переименовали, а карту забыли.
PIPELINE: List[Dict[str, Any]] = [
    {"id": "sources", "title": "Источники", "module": "sync/agent/facts.py",
     "about": "Директ, CRM, Метрика, AppMetrica — дневные факты по кампаниям",
     "next": ["mart"]},
    {"id": "mart", "title": "Витрина фактов", "module": "sync/agent/db.py",
     "about": "edu_agent_facts: расход, лиды, эффективные лиды, оплаты, позиция",
     "next": ["gate", "compute"]},
    {"id": "gate", "title": "Гейт данных", "module": "sync/agent/gate.py",
     "about": "свежесть, ширина дня, сверка сумм с источником, аномалия объёма",
     "next": ["plan"]},
    {"id": "compute", "title": "Расчёт (Э0)", "module": "sync/agent_e0.py",
     "about": "кривые насыщения, λ, целевой CPA, портфель, кандидаты",
     "next": ["ideas"]},
    {"id": "ideas", "title": "Генераторы идей", "module": "sync/agent/ideas/",
     "about": "proven, bundles, consolidate, abtests, audiences, market",
     "next": ["registry"]},
    {"id": "registry", "title": "Реестр идей", "module": "sync/agent/ideas/registry.py",
     "about": "очередь с ожиданием, ценой проверки и порогом окупаемости",
     "next": ["plan"]},
    {"id": "plan", "title": "Планировщик (Э1)", "module": "sync/agent_e1.py",
     "about": "собирает действия, считает риск и уверенность, режет по капам",
     "next": ["lanes"]},
    {"id": "lanes", "title": "Полосы и риск", "module": "sync/agent/writer/lanes.py",
     "about": "семь полос, ступень автономии, риск-карман кабинета",
     "next": ["rails"]},
    {"id": "rails", "title": "Рельсы", "module": "sync/agent/writer/guardrails.py",
     "about": "allow-лист видов, коридоры значений, запрет удаления",
     "next": ["cabinet"]},
    {"id": "cabinet", "title": "Кабинет Директа", "module": "sync/agent/writer/apply.py",
     "about": "отправка и отметка исхода; журнал edu_agent_actions",
     "next": ["watchdog", "drift"]},
    {"id": "watchdog", "title": "Сторож (Э2)", "module": "sync/agent_e1_watchdog.py",
     "about": "красные линии, откат вредного, закрытие наблюдений",
     "next": ["learning"]},
    {"id": "drift", "title": "Сверка с кабинетом", "module": "sync/agent_drift.py",
     "about": "записанное против того, что реально стоит в Директе",
     "next": ["learning"]},
    {"id": "learning", "title": "Обучение", "module": "sync/agent/learning_loop.py",
     "about": "исходы → послужной список полосы → ступень автономии",
     "next": ["lanes"]},
]


def _setting_kind(spec: Dict[str, Any]) -> str:
    """Вид поля панели: перечисление, число или составное значение."""
    if spec.get("kind"):
        return str(spec["kind"])
    if spec.get("choices") is not None:
        return "choice"
    return "number"


def settings() -> List[Dict[str, Any]]:
    """Панель настроек: ключ, вид, дефолт, границы, пояснение.

    Порядок — как в SPEC: он смысловой (сначала режим, потом темп, потом
    пороги), и алфавитная сортировка на экране его бы потеряла.
    """
    out: List[Dict[str, Any]] = []
    for key, spec in config.SPEC.items():
        row: Dict[str, Any] = {
            "key": key,
            "kind": _setting_kind(spec),
            "default": spec.get("default"),
            "nullable": bool(spec.get("nullable")),
            "about": spec.get("about", ""),
        }
        if spec.get("choices") is not None:
            row["choices"] = list(spec["choices"])
        if "min" in spec:
            row["min"] = spec["min"]
        if "max" in spec:
            row["max"] = spec["max"]
        # Целочисленность выводится из ТИПА дефолта — ровно как приведение в
        # config._validate. Отдельный флаг в SPEC разъехался бы с ним.
        if isinstance(spec.get("default"), int) and not isinstance(spec.get("default"), bool):
            row["integer"] = True
        out.append(row)
    return out


def locked() -> List[Dict[str, str]]:
    """Пороги защиты: их нет в панели, и это утверждение, а не пробел."""
    return [{"key": key} for key in config.LOCKED_KEYS]


def presets() -> Dict[str, Dict[str, Any]]:
    return {name: dict(values) for name, values in config.PRESETS.items()}


def steps() -> List[Dict[str, Any]]:
    """Лестница автономии: ступень, доля риска, что нужно, чтобы её заслужить."""
    return [{
        "step": s.step,
        "share": s.share,
        "closed_min": s.min_closed,
        "hit_rate_min": s.min_hit_rate,
        "money_hit_rate_min": s.min_money_rate,
    } for s in autonomy.STEPS]


def lane_rows() -> List[Dict[str, Any]]:
    """Полосы: что они делают, чем платят, сколько ждут исхода."""
    kinds_by_lane: Dict[str, List[str]] = {lane: [] for lane in lanes.ALL_LANES}
    for kind, lane in lanes.LANE_OF_KIND.items():
        kinds_by_lane.setdefault(lane, []).append(kind)
    out: List[Dict[str, Any]] = []
    for lane in lanes.ALL_LANES:
        out.append({
            "lane": lane,
            "about": LANE_ABOUT.get(lane, ""),
            "measure_days": lanes.MEASURE_DAYS[lane],
            "pays_risk": lane in lanes.RISK_PAYING_LANES,
            "multi_lever": lane in lanes.MULTI_LEVER_LANES,
            "manual_release": lane in autonomy.MANUAL_RELEASE_LANES,
            "default_step": lanes.default_step_of(lane),
            "max_cut_share": (lanes.HYGIENE_MAX_CUT_SHARE
                              if lane == lanes.LANE_HYGIENE else None),
            "kinds": sorted(kinds_by_lane.get(lane, [])),
        })
    return out


def tiers() -> List[Dict[str, Any]]:
    return [{
        "tier": value,
        "title": TIER_ABOUT[value]["title"],
        "about": TIER_ABOUT[value]["about"],
        "pays_risk": value in tier.RISK_PAYING_TIERS,
        "applied": value in tier.APPLIED_TIERS,
    } for value in tier.ALL_TIERS]


def action_kinds() -> List[Dict[str, Any]]:
    """Виды действий: полоса, право применять, право откатывать.

    Список строится по КАРТЕ ПОЛОС, а не по allow-листу: карта идёт впереди
    (вид появляется в ней вместе с генератором, а право применять — вместе с
    рычагом и тестом), и разница между двумя списками — это ровно то, что
    человек должен видеть на экране «Рычаги».
    """
    out: List[Dict[str, Any]] = []
    for kind in sorted(lanes.LANE_OF_KIND):
        # Куда возвращает откат. Плоского «откатывается: да/нет» мало и оно
        # соврало бы: bidmodifier.add в списке пути возврата не значится
        # вовсе, потому что отменяется он НЕ собой, а перезаписью в нейтраль
        # (ROLLBACK_ORIGIN_ADD_KINDS). Экран, показавший бы по нему «откат
        # невозможен», описывал бы систему неверно.
        if kind in guardrails.ROLLBACK_ORIGIN_ADD_KINDS:
            rollback_to = "neutral"
        elif kind in guardrails.ROLLBACK_ALLOWED_ACTION_KINDS:
            rollback_to = "previous"
        else:
            rollback_to = None
        out.append({
            "kind": kind,
            "lane": lanes.LANE_OF_KIND[kind],
            "applied": kind in guardrails.ALLOWED_ACTION_KINDS,
            "rollback": rollback_to is not None,
            "rollback_to": rollback_to,
            "builder": kind in guardrails.BUILDER_ACTION_KINDS,
            "cutting": kind in tier.CUTTING_KINDS,
            "reason": (guardrails.BUILDER_REASON
                       if kind in guardrails.BUILDER_ACTION_KINDS else None),
        })
    return out


def gates() -> Dict[str, Any]:
    """Пороги гейта данных — те же, по которым прогон решает писать или нет."""
    return {
        "window_days": gate.GATE_WINDOW_DAYS,
        "min_breadth": gate.GATE_MIN_BREADTH,
        "facts_max_age_days": gate.FACTS_MAX_AGE_DAYS,
        "sum_settle_days": gate.SUM_SETTLE_DAYS,
        "sum_tolerance": SUM_TOLERANCE,
    }


def build(generated_at: Optional[str] = None) -> Dict[str, Any]:
    """Манифест целиком.

    generated_at передаёт вызывающий: время — это вход, а не факт из кода, и
    собранный внутри now() сделал бы функцию непроверяемой ровно в том месте,
    где её проверяют сравнением с эталоном.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "pipeline": PIPELINE,
        "lanes": lane_rows(),
        "steps": steps(),
        "tiers": tiers(),
        "action_kinds": action_kinds(),
        "settings": settings(),
        "locked": locked(),
        "presets": presets(),
        "gates": gates(),
    }
