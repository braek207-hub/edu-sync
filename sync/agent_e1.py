# -*- coding: utf-8 -*-
"""
sync/agent_e1.py — прогон Э1a: применение вычисленных настроек.

Порядок: гейт данных → план → свежий факт из API → diff → рельсы → заповедник →
риск-бюджет → применение → сторож красных линий.

По умолчанию ПЕСОЧНИЦА и DRY-RUN. Боевая запись требует двух явных флагов:
--prod и --apply. Это не перестраховка: единственный необратимый шаг здесь —
касание живого кабинета, и он должен быть намеренным.

Запуск:
    python -m sync.agent_e1                    # песочница, dry-run
    python -m sync.agent_e1 --prod             # боевой кабинет, dry-run
    python -m sync.agent_e1 --prod --apply     # боевая запись
    ... --campaigns=123        # только названные кампании
    ... --max-campaigns=1      # не больше N кампаний НА ВЕСЬ прогон

ПЕРВЫЙ БОЕВОЙ ПРОГОН запускается ограниченным по кампаниям — это третья опора
безопасности наравне с репетицией и пробой по несуществующим идентификаторам
(песочница боевым логинам недоступна, выяснено пробой):

    python -m sync.agent_e1 --prod --apply --max-campaigns=1

Сколько кампаний прогон тронул на самом деле — поле campaigns_touched в отчёте
каждого кабинета.

ENV: DATABASE_URL, DIRECT_TOKEN, DIRECT_CLIENTS_JSON

Два отклонения от исходного плана задачи (см. task-9-report.md):

1. Список кампаний кабинета берётся через campaigns.get ЭТОГО кабинета,
   а не как срез справочника расходов по всем кабинетам сразу. Справочник
   расходов (edu_agent_facts) копит кампании ВСЕХ клиентов в одной таблице;
   без пересечения с собственным списком кампаний агент слал бы
   bidmodifiers.get по чужим Id — гарантированная ошибка "объект не найден"
   и лишние Units на чужой кабинет.

2. Одна запись bidmodifiers.get → максимум одна нормализованная actual-запись.
   Запись DemographicsAdjustment может нести Gender И Age ОДНОВРЕМЕННО (ставка
   на пересечение сегментов). Это ОДИН объект Директа с одним Id и одним
   коэффициентом, и он не эквивалентен паре одномерных: «мужчины 25–34» — не
   «мужчины». Такая запись сворачивается в одну actual-запись с составным
   ключом, который заведомо не сойдётся с одномерным планом (подробности — в
   _normalize_actual). Прежняя раскладка на две записи с одним Id выпускала из
   diff два изменения на один физический объект.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from sync.agent import config as agent_config
from sync.agent import db as agent_db
from sync.agent.confidence import thresholds_from_config
from sync.agent.balance import (
    MIN_ASSIGNED_SHARE,
    balance_inputs,
    require_growth_address,
    tact_balance,
)
from sync.agent.coverage import blind_share
from sync.agent.gate import data_gate
from sync.agent.writer import budget
from sync.agent.writer import switch
from sync.agent.writer import negatives
from sync.agent.writer import placements
from sync.agent.writer import tcpa
from sync.agent.writer import db as writer_db
from sync.agent.writer.apply import apply_actions
from sync.agent.writer.client import WriteClient, journal_writes_allowed
from sync.agent.writer.diff import diff_modifiers, diff_schedule
from sync.agent.writer.guardrails import (
    MAX_ACTIONS_PER_RUN,
    cap_actions,
    check_action,
    check_holdout,
)
from sync.agent.writer.learning import (
    LEARNING_COOLDOWN_DAYS,
    learning_impact,
    split_by_learning_cooldown,
)
from sync.agent.writer.plan import plan_bid_modifiers, plan_schedule
from sync.agent.writer.schedule import describe as describe_schedule
from sync.agent.writer.schedule import schedule_changed, schedule_items
from sync.agent.writer.risk import (
    DAYS_IN_WEEK,
    DEFAULT_RISK_SHARE_WEEK,
    action_risk,
    action_risk_basis,
    fit_into_budget,
    median,
    object_cap,
    object_daily_cost,
    paced_allowance,
    risk_object,
    week_start,
    weekly_limit,
)
from sync.agent import blackbox, conflicts, experiments, rejects
from sync.agent.writer.rollback import red_line_for
from sync.agent.writer.units import api_to_delta

CAMPAIGN_PAGE_LIMIT = 1000


def weekly_risk_limit(week_start_iso: str, daily_cost: Dict[str, float],
                      config: Optional[Dict[str, Any]] = None) -> float:
    """Недельный потолок риска прогона — доля расхода; строка в таблице поверх.

    Расход берётся тем же справочником дневных расходов, которым считается
    цена каждого действия (agent_db.load_daily_cost_by_campaign): сумма
    дневных темпов × 7. Он общий на все кабинеты прогона — ровно как и сам
    риск-бюджет, который читается один раз на неделю, а не по кабинету.

    Абсолютный потолок из edu_agent_risk_budget, если строка на неделю есть,
    перебивает долю: это ручное решение человека (risk_budget_week в
    LOCKED_KEYS панели), и оно не обязано ни с чем сходиться.

    Пустой справочник — расход неизвестен: weekly_limit отдаёт прежний
    абсолютный дефолт, а не ноль, иначе прогон при первом же пробеле в
    витрине отложил бы всё до единого действия и выглядел бы в отчёте как
    исправно остановленный.
    """
    share = float((config or {}).get("risk_share_week", DEFAULT_RISK_SHARE_WEEK))
    run_weekly_spend = sum(float(v) for v in daily_cost.values()) * DAYS_IN_WEEK
    return writer_db.risk_limit(week_start_iso,
                                weekly_limit(run_weekly_spend, share))


def campaign_expectation_context(
        campaign_id: str,
        daily_cost: Dict[str, float],
        campaign_computed: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Экономика кампании для ожидания корректировок и расписания.

    Без неё writer/expectation.of возвращает None, и семь рычагов из девяти
    остались бы без обещания ИМЕННО В ПРОДЕ: тесты подставляют контекст сами и
    ожидание видят, а боевой прогон отдал бы в кабинет действия, которые нечем
    ранжировать при отборе (задача 7) и нечем судить в замере такта.

    Цена лида берётся из той же строки портфеля, по которой планируются
    выключения (switch.window_economics: расход и лиды окна 28 дней), — второй
    копии экономики не заводим. Строки нет или в ней ноль лидов — ключ не
    подставляется вовсе: ноль здесь означал бы «лид бесплатен», и ожидание
    вышло бы бесконечным.
    """
    context: Dict[str, Any] = {}
    cost_per_day = daily_cost.get(str(campaign_id))
    if cost_per_day:
        context["daily_cost_rub"] = float(cost_per_day)

    economics = switch.window_economics(campaign_computed.get(str(campaign_id)) or [])
    leads = float(economics.get("leads_28d") or 0.0)
    cost = float(economics.get("cost_28d") or 0.0)
    if leads > 0 and cost > 0:
        context["cpa_rub"] = cost / leads
    return context


# --------------------------------------------- ограничение объёма прогона

# Безопасность ПЕРВОГО боевого применения держится на трёх вещах сразу:
# репетиция без записи (--prod без --apply), проба по несуществующим
# идентификаторам и первое применение НА ОДНОЙ КАМПАНИИ. Песочница боевым
# логинам недоступна (выяснено пробой), поэтому третье — не удобство, а
# полноправная часть защиты, и его не было в коде вовсе.
#
# Что ограничивало прогон до этого: лимит действий (MAX_ACTIONS_PER_RUN = 50)
# и риск-бюджет. Ни то, ни другое не про кампании: вычисленные настройки
# лежат на уровне КАБИНЕТА, то есть один набор ключей раскатывается на все
# его кампании сразу — первый боевой прогон тронул бы столько кампаний,
# сколько влезет в лимит действий, а какие именно, решил бы порядок
# сортировки own_campaign_ids.
CAMPAIGNS_ARG = "--campaigns="
MAX_CAMPAIGNS_ARG = "--max-campaigns="

# Всё, что прогон понимает. Флаги режима (--prod/--apply) здесь тоже: их
# читает main(), но проверяются они здесь, где разбираются аргументы.
KNOWN_ARGS = ("--prod", "--apply", CAMPAIGNS_ARG, MAX_CAMPAIGNS_ARG)

UNKNOWN_ARG_REASON = (
    "неизвестный аргумент {arg}: прогон остановлен, потому что опечатка в "
    "ограничителе даёт прогон ШИРЕ запрошенного. Понимаются: {known}"
)

BAD_CAMPAIGNS_ARG_REASON = (
    "{arg}: список кампаний пуст. Форма — --campaigns=123 или "
    "--campaigns=123,456; пустой список это не «все кампании», а опечатка"
)
BAD_MAX_CAMPAIGNS_ARG_REASON = (
    "{arg}: число кампаний должно быть целым и не меньше единицы. Форма — "
    "--max-campaigns=1"
)


class CampaignScope:
    """Сколько кампаний и каких именно прогону позволено трогать.

    Ограничение применяется ТАМ, ГДЕ СТРОИТСЯ СПИСОК КАМПАНИЙ КАБИНЕТА
    (own_campaign_ids), а не отсечением действий в конце. Разница
    принципиальная: отсечение в конце всё равно означало бы, что кабинет
    прочитан целиком, план построен по всем кампаниям, риск посчитан по всем
    кампаниям, — и от порядка сортировки по-прежнему зависело бы, какие
    кампании доживут до отправки. Ограничение на входе делает прогон по одной
    кампании ровно прогоном по одной кампании.

    Число кампаний — потолок на ПРОГОН, а не на кабинет: тем же доводом, что
    и лимит действий (MAX_ACTIONS_PER_RUN). «Первое применение на одной
    кампании» при потолке на кабинет означало бы четыре кампании на четырёх
    кабинетах.

    Именованный список кампаний потолком на кабинет не страдает вовсе:
    идентификаторы кампаний между кабинетами не пересекаются, и чужая
    кампания просто не найдётся в списке своего кабинета.
    """

    def __init__(self, only: Any = None, max_campaigns: Any = None) -> None:
        names = {str(c).strip() for c in (only or ())} - {""}
        self.only: Optional[Set[str]] = names or None
        self.max_campaigns: Optional[int] = (None if max_campaigns is None
                                             else int(max_campaigns))
        self.remaining: Optional[int] = self.max_campaigns

    @property
    def enabled(self) -> bool:
        return self.only is not None or self.max_campaigns is not None

    def select(self, campaign_ids: List[str]) -> List[str]:
        """Кампании кабинета → та их часть, которую прогону позволено трогать.

        Порядок входного списка сохраняется (own_campaign_ids отдаёт его
        отсортированным), поэтому выбор при --max-campaigns детерминирован:
        повторный прогон с тем же ограничением возьмёт те же кампании, а не
        случайные.
        """
        selected = list(campaign_ids)
        if self.only is not None:
            selected = [c for c in selected if str(c) in self.only]
        if self.remaining is not None:
            selected = selected[:max(self.remaining, 0)]
            self.remaining -= len(selected)
        return selected

    def report(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "only": sorted(self.only) if self.only else None,
            "max_campaigns": self.max_campaigns,
            "campaigns_left_in_run": self.remaining,
        }


def parse_campaign_scope(argv: List[str]) -> CampaignScope:
    """Аргументы прогона → ограничитель кампаний. Мусор — ValueError.

    Молчаливое игнорирование неразобранного аргумента здесь недопустимо:
    оператор, набравший --max-campaigns=one перед ПЕРВЫМ боевым прогоном,
    получил бы прогон без ограничения вообще, будучи уверенным в обратном.
    """
    only: Optional[List[str]] = None
    max_campaigns: Optional[int] = None
    for arg in argv:
        if arg.startswith("--") and not any(
                arg == known or arg.startswith(known)
                for known in KNOWN_ARGS):
            # Белый список, а не «разберём, что узнали»: неизвестный флаг —
            # почти всегда опечатка в ОГРАНИЧИТЕЛЕ, и молча продолжать
            # значит выполнить прогон шире, чем просил оператор.
            raise ValueError(UNKNOWN_ARG_REASON.format(
                arg=arg, known=", ".join(sorted(KNOWN_ARGS))))
        if arg.startswith(CAMPAIGNS_ARG):
            only = [p.strip() for p in arg[len(CAMPAIGNS_ARG):].split(",") if p.strip()]
            if not only:
                raise ValueError(BAD_CAMPAIGNS_ARG_REASON.format(arg=arg))
        elif arg.startswith(MAX_CAMPAIGNS_ARG):
            raw = arg[len(MAX_CAMPAIGNS_ARG):].strip()
            if not raw.isdigit() or int(raw) < 1:
                raise ValueError(BAD_MAX_CAMPAIGNS_ARG_REASON.format(arg=arg))
            max_campaigns = int(raw)
    return CampaignScope(only, max_campaigns)


# Множитель медианы базового CPA → абсолютный аварийный порог красной линии
# для кампаний без собственной базы (rollback.py::red_line_for, has_baseline=
# False). Медиана — типичный CPA портфеля, а не потолок для конкретной новой
# или непредсказуемой кампании: x2 даёт запас, прежде чем считать результат
# провалом, но не настолько широкий, чтобы автооткат никогда не сработал —
# тот же порядок величины, что и относительный потолок для кампаний с базой
# (RED_LINE_TOLERANCE = +40% в rollback.py — здесь просто нет базы для %).
ABSOLUTE_MAX_CPA_MULTIPLIER = 2.0

# Причина, по которой действие не применяется, когда абсолютный порог
# посчитать не из чего вообще (справочник базовых CPA пуст целиком).
NO_RED_LINE_REASON = "нет базового CPA ни у одной кампании — красная линия недостижима"

# Настройки читаются строго по кабинету (agent_db.load_latest_computed_settings).
# Пустой ответ — не «нечего делать»: он значит либо что Э0 по этому кабинету не
# проходил, либо что в таблице лежат строки СТАРОГО формата, записанные под общим
# идентификатором на все кабинеты сразу. Старые строки читать нельзя — в них
# выжили числа одного кабинета, перетершие остальные; прогон честно ничего не
# применит, но это обязано быть видно в отчёте, а не выглядеть как тишина.
NO_COMPUTED_REASON = (
    "нет вычисленных настроек кабинета {login} (object_level='account', "
    "object_id='{login}'): либо расчёт Э0 по этому кабинету не проходил, либо в "
    "таблице лежат строки старого формата под общим object_id — они не читаются "
    "намеренно, потому что схлопывали кабинеты в один набор"
)

# Сколько минут строка может простоять в статусе planned, прежде чем считаться
# зависшей. Прогон живёт минуты; всё, что старше, — след обрыва прошлого прогона
# ПОСЛЕ отправки запроса (см. writer/db.py::MARK_STALE_SQL).
#
# Прогон не просто печатает такую строку, а переводит её в статус 'stale'
# (writer_db.mark_stale_planned): отчёт показывает только ПЕРВОЕ обнаружение,
# дальше строка живёт как непроверенное изменение — видна сторожу отката
# (open_actions), оплачена риск-бюджетом (spent_risk) и защищена от повторной
# отправки. Печать без последствий оставляла её вечным шумом в каждом отчёте.
#
# Но пометка — это изменение состояния БОЕВОГО журнала, и делать его от имени
# репетиции нельзя: 'stale' входит и в FINAL_STATUSES, и в LIVE_STATUSES, то
# есть репетиция закрывала строку от повторной отправки и списывала за неё
# риск-бюджет, ничего никуда не отправив. Сторож в репетиции журнал не трогает
# сознательно (writer/client.py::journal_writes_allowed) — правило на журнал
# одно, и прямое применение подчиняется ему так же.
STALE_PLANNED_MINUTES = 60

# Почему в репетиции зависшие строки не помечены. В отчёте это обязано быть
# видно: «ноль находок» и «не искали» — разные состояния.
REHEARSAL_STALE_REASON = (
    "репетиция журнал не меняет: пометка 'stale' закрывает строку от повторной "
    "отправки и списывает за неё риск-бюджет — это утверждение о боевом "
    "кабинете, и делать его вправе только боевая запись (--prod --apply)"
)

# Комбинация «песочница + запись» (--apply без --prod). У сторожа
# (agent_e1_watchdog.SANDBOX_APPLY_REFUSAL) этот запрет уже есть; здесь —
# та же комбинация для прямого применения, а не отката, и последствие то же:
# боевые логины в песочнице недоступны, а строка о попытке всё равно уходит
# в БОЕВОЙ журнал (база одна, см. writer/client.py::journal_writes_allowed).
# При транспортной ошибке такая строка получает статус «исход неизвестен» —
# занимает риск-бюджет и попадает под наблюдение сторожа. Сторож пойдёт
# откатывать объект, которого в боевом кабинете никогда не существовало,
# израсходует на него все попытки отката и пометит неоткатываемым навсегда.
SANDBOX_APPLY_REFUSAL = (
    "запрещённая комбинация «песочница + запись»: боевые логины в песочнице "
    "недоступны, а строка о попытке применения всё равно уходит в БОЕВОЙ "
    "журнал (база одна). При транспортной ошибке она получает статус «исход "
    "неизвестен», занимает риск-бюджет и попадает под наблюдение сторожа — "
    "тот пойдёт откатывать объект, которого в боевом кабинете никогда не "
    "было, израсходует на него все попытки и пометит неоткатываемым "
    "навсегда. Нужна боевая запись — --prod --apply; нужна репетиция — без "
    "--apply"
)

# Сколько дней после отката сегмент кампании не трогаем.
#
# Зачем вообще: ключ идемпотентности содержит ПРОЦЕНТ, а процент
# пересчитывается на каждом прогоне по скользящему окну и дрейфует на пункт-
# другой. Применили тридцать, откатили, назавтра расчёт дал двадцать девять —
# это уже другой ключ, идемпотентность молчит, и цикл «применили → две недели
# ухудшенного показателя → откат» крутится вечно. Обратная связь от отката к
# планированию обязана опираться на ОБЪЕКТ И СЕГМЕНТ, а не на точное значение.
#
# Почему 60, а не «чуть больше окна наблюдения»:
#   * полный цикл применение → вердикт → откат укладывается в 15 дней
#     (agent_e1_watchdog: OBSERVATION_LAG_DAYS=1 + OBSERVATION_HORIZON_DAYS=14).
#     Кулдаун порядка этих же 15 дней цикл не разрывает, а только удлиняет:
#     доля времени, которое сегмент проводит под повтором вредного изменения,
#     осталась бы около половины;
#   * 60 дней — вчетверо больше полного цикла: та же доля падает примерно до
#     двадцати процентов, а число повторов по одному сегменту — с двух десятков
#     в год до шести;
#   * и главное — 60 вдвое больше тридцатидневного окна, из которого берутся
#     дневной расход и базовый CPA (cutoff в main()). К моменту, когда повтор
#     снова разрешён, и оценка риска, и красная линия посчитаны ЦЕЛИКОМ по дням
#     ПОСЛЕ отката. Иначе повтор судился бы по базе, испорченной тем самым
#     изменением, ради отмены которого откат и делался.
COOLDOWN_AFTER_ROLLBACK_DAYS = 60

# Кулдаун считается по ВЕРДИКТУ сторожа, а не по успешности возврата: пробитая
# красная линия делает сегмент вредным независимо от того, удалось ли откатить
# изменение. Неоткатанный пробой — случай ХУДШИЙ, а не лучший: изменение всё
# ещё живёт в кабинете, и добавлять к нему второе по тому же сегменту нельзя
# тем более.
COOLDOWN_REASON = (
    "сегмент признан вредным за последние {days} дн. (красная линия пробита; "
    "откат при этом мог и не удаться): повтор запрещён до конца кулдауна — "
    "проверка по объекту и сегменту, а не по проценту, потому что процент "
    "дрейфует между расчётами и обходил бы идемпотентность"
)

# Причина отказа для действия, чей ключ уже закрыт финальным статусом.
# Отсекается ДО отбора по лимиту действий: порядок обхода детерминирован, и
# накопившиеся закрытые ключи стабильно занимали начало списка, съедая лимит
# целиком, — прогон рапортовал «подготовлено пятьдесят, применено ноль».
ALREADY_FINAL_REASON = (
    "ключ действия уже закрыт финальным статусом журнала: повторная отправка "
    "исключена, слот лимита действий занимать нельзя"
)

# Причина отказа для сегмента, исчерпавшего попытки применения.
#
# Статус 'rejected' намеренно не финальный: API вернул 200 и отклонил элемент,
# в кабинете ничего не изменилось, и действие обязано быть переприменимо.
# Потолка попыток у этого пути не было — в отличие от отката, где он есть.
# Детерминированный отказ (неподдерживаемый ключ сегмента, неподходящий тип
# кампании, «объект уже существует») переотправлялся бы каждый прогон вечно,
# каждый раз занимая слот лимита и часть риск-бюджета: та же болезнь, ради
# которой введён отсев уже закрытых ключей, только через другой статус.
EXHAUSTED_ATTEMPTS_REASON = (
    "сегмент исчерпал попытки применения ({attempts} из {max_attempts}): "
    "отказ детерминированный, повтор его не исправит — строка ждёт разбора "
    "человеком. Счёт по сегменту, а не по ключу идемпотентности: процент "
    "дрейфует между расчётами и обходил бы счётчик на ключе"
)

# Предельный возраст расчёта Э0, дни. «Последний расчёт» не значит «свежий»:
# без верхней границы движок раскатает месячные коэффициенты, посчитанные по
# аудитории, которой больше нет. Неделя — потому что расчёт сжат к нулю по
# объёму наблюдений, и недельного дрейфа он ещё не замечает, а месячный уже
# описывает другой кабинет.
MAX_COMPUTED_AGE_DAYS = 7

STALE_COMPUTED_REASON = (
    "расчёт Э0 старше {max_days} дн. (calc_date={calc_date}, возраст {age} дн.): "
    "коэффициенты посчитаны по устаревшей аудитории и не применяются"
)

UNKNOWN_COMPUTED_DATE_REASON = (
    "в расчёте Э0 нет даты (calc_date): возраст коэффициентов неизвестен, "
    "а «неизвестно» — не «свежо»"
)

# Сколько действий показать поимённо в отчёте. Остальное — агрегатом по видам
# настроек: полный список из полусотни строк превращает отчёт в стену текста,
# а он читается глазами перед решением включать боевую запись.
PREVIEW_SAMPLE_LIMIT = 5

# Поле ответа bidmodifiers.get → (тип корректировки, ключ в форме плана).
# Устройство у Директа — три РАЗНЫХ типа корректировки, а не один мобильный;
# ключи в верхнем регистре, как в plan.DEVICE_TYPE_MAP, иначе diff не сойдётся.
_DEVICE_ADJUSTMENTS = (
    ("MobileAdjustment", "MOBILE_ADJUSTMENT", "MOBILE"),
    ("DesktopAdjustment", "DESKTOP_ADJUSTMENT", "DESKTOP"),
    ("TabletAdjustment", "TABLET_ADJUSTMENT", "TABLET"),
)

# Корректировка, несущая сразу несколько измерений (пол И возраст), — это НЕ
# сумма одномерных: «мужчины 25–34» и «мужчины» в Директе разные объекты с
# разными ставками. Такой факт нормализуется составным ключом, который заведомо
# не совпадает ни с одним ключом плана (в плане ключ всегда одномерный), и
# разнотипной меткой — чтобы план не сопоставился с ним по случайности.
COMPOSITE_KEY_SEPARATOR = "+"
COMPOSITE_TYPE = "COMPOSITE_ADJUSTMENT"

# Запись факта, в которой не оказалось коэффициента. Прежде отсутствующее
# значение проходило через `or 0` и превращалось в api_to_delta(0) = -100 —
# «подавить сегмент на сто процентов». Ноль в шкале Директа это НЕ нейтраль
# (нейтраль — 100), поэтому такая подмена не безобидна: -100 уезжал в
# previous_state действия set, и откат вместо возврата к прежней ставке
# выставил бы коэффициент 0, то есть полное подавление сегмента.
# Отсутствующее значение значит «запись негодна» и не порождает действий
# вообще — ни set (прошлое состояние неизвестно), ни add (объект в кабинете
# существует, второй такой же создавать нельзя).
UNUSABLE_ACTUAL_REASON = (
    "в ответе bidmodifiers.get нет коэффициента: прошлое состояние неизвестно, "
    "действие по этому объекту не строится (ноль — не нейтраль, а подавление)"
)


def _clients() -> List[Dict[str, Any]]:
    """Кабинеты прогона. Форма — та же, что у расчёта (agent_e0._direct_clients).

    Логин нормализуется ТОЙ ЖЕ функцией, что на записи настроек: он едет и в
    заголовок Client-Login запроса, и в load_latest_computed_settings как
    ключ object_id. Прежде условие проверяло обрезанное значение, а в список
    клало сырое — пробел по краям логина в переменной окружения разводил
    запись и чтение по разным ключам, и прогон молча рапортовал, что
    применять нечего.
    """
    raw = (os.environ.get("DIRECT_CLIENTS_JSON") or "").strip()
    out: List[Dict[str, Any]] = []
    if raw:
        for item in json.loads(raw):
            if not isinstance(item, dict):
                continue
            login = agent_db.normalize_login(item.get("login"))
            if login:
                out.append({"login": login})
    return out


def fetch_campaign_ids(client: WriteClient) -> List[int]:
    """Id всех кампаний ОДНОГО кабинета (форма — sync/agent/segments.py::fetch_campaign_ids).

    Постранично: Page.Limit/Offset, остановка когда страница короче лимита.
    """
    out: List[int] = []
    offset = 0
    while True:
        result = client.get("campaigns", {
            "SelectionCriteria": {},
            "FieldNames": ["Id"],
            "Page": {"Limit": CAMPAIGN_PAGE_LIMIT, "Offset": offset},
        })
        items = result.get("Campaigns") or []
        out += [int(c["Id"]) for c in items]
        if len(items) < CAMPAIGN_PAGE_LIMIT:
            break
        offset += CAMPAIGN_PAGE_LIMIT
    return out


def own_campaign_ids(client: WriteClient, daily_cost_by_campaign: Dict[str, float],
                     scope: Optional[CampaignScope] = None) -> List[str]:
    """Кампании ЭТОГО кабинета, пересечённые со справочником расходов.

    daily_cost_by_campaign построен по ВСЕМ кабинетам сразу — без пересечения
    с собственным списком кампаний агент опрашивал бы чужие Id чужим логином.

    scope — ограничитель первого боевого прогона. Он стоит ЗДЕСЬ, на входе:
    всё, что ниже по течению (чтение корректировок, diff, риск, отправка),
    работает уже с урезанным списком и про ограничение ничего не знает.
    """
    own = {str(i) for i in fetch_campaign_ids(client)}
    ids = sorted(own & set(daily_cost_by_campaign.keys()))
    return scope.select(ids) if scope is not None else ids


def _delta_or_none(raw: Any) -> Any:
    """Коэффициент из ответа API → дельта. None, если коэффициента нет.

    Отсутствие значения НЕ подменяется нулём: ноль в шкале Директа означает
    «ставка × 0», то есть максимальное подавление сегмента, а не «нейтраль»
    и не «неизвестно». Подмена уезжала в previous_state и делала откат
    оружием против той же кампании (см. UNUSABLE_ACTUAL_REASON).
    """
    if raw is None:
        return None
    try:
        return api_to_delta(raw)
    except (TypeError, ValueError):
        return None


def _normalize_actual(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Одна запись bidmodifiers.get → 0 или 1 нормализованная actual-запись.

    Три обязанности, все — про совпадение факта с планом:

    1. Единицы. API отдаёт 100-базный коэффициент (100 = нейтраль), план
       живёт в дельтах — здесь стоит обратная граница конверсии
       (units.api_to_delta), парная той, что в apply.to_api_call. Без неё
       diff сравнивал бы 130 с 30 и переписывал корректировку на каждом
       прогоне.
    2. Ключи. Форма ключа обязана совпадать с планом (plan.DEVICE_TYPE_MAP,
       верхний регистр): устройство — это ТРИ разных типа корректировки, и
       ключ "mobile" строчными не сойдётся с "MOBILE" из плана никогда,
       из-за чего diff вечно предлагал бы add там, где нужен set.
    3. Один объект — одна запись. Запись bidmodifiers.get это ОДИН физический
       объект в Директе с одним Id и одним коэффициентом. Раскладка её на
       несколько normalized-записей с ОДНИМ И ТЕМ ЖЕ Id выпускала из diff два
       изменения на один объект: второе затирало первое, оба списывали
       риск-бюджет, оба сохраняли прошлое состояние, снятое ДО первого, —
       откат вернул бы объект не туда, откуда агент его вывел.

    Многомерная корректировка («мужчины 25–34» — Gender И Age одновременно)
    сворачивается в ОДНУ запись с составным ключом (GENDER_MALE+AGE_25_34).
    Составной ключ не совпадает ни с одним одномерным ключом плана — и это
    правильно: коэффициент, посчитанный для всего мужского сегмента, к
    пересечению «мужчины 25–34» не относится, ставить его туда значит править
    не тот объект. Diff увидит, что одномерной корректировки в кабинете нет, и
    предложит добавить её отдельным объектом, не трогая многомерную —
    в Директе это разные объекты, и они сосуществуют штатно.
    """
    dimensions: List[Tuple[str, str, Any]] = []  # (тип, ключ, дельта или None)

    for api_field, direct_type, key in _DEVICE_ADJUSTMENTS:
        adjustment = item.get(api_field) or {}
        if adjustment:
            dimensions.append(
                (direct_type, key, _delta_or_none(adjustment.get("BidModifier"))))

    demo = item.get("DemographicsAdjustment") or {}
    if demo:
        percent = _delta_or_none(demo.get("BidModifier"))
        for value in (demo.get("Gender"), demo.get("Age")):
            if value:
                dimensions.append(("DEMOGRAPHICS_ADJUSTMENT", str(value), percent))

    regional = item.get("RegionalAdjustment") or {}
    if regional:
        dimensions.append(("REGIONAL_ADJUSTMENT", str(regional.get("RegionId") or ""),
                           _delta_or_none(regional.get("BidModifier"))))

    if not dimensions:
        return []

    # Негодность — свойство ВСЕЙ записи: это один физический объект Директа с
    # одним коэффициентом, и если коэффициента нет, негодны все её измерения.
    unusable = any(p is None for _, _, p in dimensions)

    if len(dimensions) == 1:
        direct_type, key, percent = dimensions[0]
        record: Dict[str, Any] = {"Id": item["Id"], "Type": direct_type, "key": key,
                                  "percent": percent}
    else:
        types = {t for t, _, _ in dimensions}
        record = {
            "Id": item["Id"],
            # Измерения одного типа (пол+возраст) сохраняют свой тип; разнотипная
            # комбинация типу не принадлежит вообще — своя метка, чтобы она не
            # сошлась с планом по чистой случайности.
            "Type": dimensions[0][0] if len(types) == 1 else COMPOSITE_TYPE,
            "key": COMPOSITE_KEY_SEPARATOR.join(k for _, k, _ in dimensions),
            "percent": dimensions[0][2],
            "composite": True,
        }

    if unusable:
        record["unusable"] = True
        record["unusable_reason"] = UNUSABLE_ACTUAL_REASON
    return [record]


def _unsupported_report(unsupported: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Неприменимая часть плана для отчёта прогона: сколько и почему.

    Без этого блока «регион временно не применяется» выглядело бы как
    «региона в данных нет» — и пауза жила бы незамеченной месяцами.
    """
    by_reason: Dict[str, int] = {}
    for row in unsupported:
        by_reason[row["reason"]] = by_reason.get(row["reason"], 0) + 1
    return {"count": len(unsupported), "by_reason": by_reason}


def run_verdict(account_reports: List[Dict[str, Any]]) -> str:
    """Итог всего такта записи одним кодом — для истории прогонов.

    blackbox.save_run берёт вердикт из report["verdict"], и до этой функции
    такт записи ложился в edu_agent_runs с ПУСТЫМ вердиктом: в логе отказ
    кабинета был виден, в истории — нет. На экране агента пустой вердикт
    неотличим от «прошло тихо», а это ровно то состояние, ради которого
    историю и смотрят.

    Кабинетный отчёт успешного пути вердикта не содержит вовсе (там нечего
    сообщать сверх чисел), поэтому отсутствие кода читается как работа, а не
    как ошибка. Порядок приоритетов — от того, что требует человека, к тому,
    что его не требует; смешанный прогон называется по худшему кабинету.
    """
    verdicts = [str(r.get("verdict") or "") for r in account_reports]
    if not verdicts:
        return "NOTHING_TO_DO"
    failed = [v for v in verdicts if v == "ACCOUNT_FAILED"]
    if failed:
        return "RED" if len(failed) == len(verdicts) else "PARTIAL_FAILURE"
    for code in ("STALE_COMPUTED_SETTINGS", "NO_COMPUTED_SETTINGS"):
        if code in verdicts:
            return code
    if all(v == "NOTHING_TO_DO" for v in verdicts):
        return "NOTHING_TO_DO"
    return "GREEN"


def action_label(action: Dict[str, Any]) -> str:
    """Короткая подпись действия: что и на сколько правится."""
    payload = action.get("payload") or {}
    kind_full = str(action.get("action_kind") or "")
    if kind_full.startswith("budget."):
        micros = (payload.get("WeeklySpendLimit")
                  or (payload.get("DailyBudget") or {}).get("Amount") or 0)
        unit = "нед" if kind_full == "budget.set" else "день"
        return (f"{action.get('direct_type')}:{action.get('key')} "
                f"→ {int(micros) // 1_000_000} ₽/{unit}")
    if kind_full == "campaign.suspend":
        return f"{action.get('direct_type')}:{action.get('key')} → пауза"
    percent = int(payload.get("BidModifier") or 0)
    kind = kind_full.split(".")[-1]
    return f"{action.get('direct_type')}:{action.get('key')} {percent:+d}% ({kind})"


def _risk_pricing(prepared: List[Dict[str, Any]],
                  caps: Dict[str, float]) -> Dict[str, Any]:
    """Что дала дельта-модель: сколько списано против прежней арифметики.

    Прежняя цена набора — сумма ПОТОЛКОВ затронутых объектов: старая модель
    брала за первое действие по кампании её расход за горизонт целиком, и это
    ровно cap. Отношение показывает, во сколько раз честная арифметика
    освободила бюджет риска; по нему же видно, если освобождение оказалось
    иллюзией — например, когда доля сегмента у всех действий неизвестна.
    """
    charged = round(sum(a["risk_rub"] for a in prepared), 2)
    touched = {risk_object(a) for a in prepared}
    old_model = round(sum(caps.get(o, 0.0) for o in touched), 2)
    by_basis: Dict[str, int] = {}
    for a in prepared:
        basis = str(a.get("risk_basis") or "—")
        by_basis[basis] = by_basis.get(basis, 0) + 1
    return {
        "charged_rub": charged,
        "old_model_rub": old_model,
        "cheaper_times": (round(old_model / charged, 1)
                          if charged > 0 else None),
        "objects_touched": len(touched),
        "by_basis": dict(sorted(by_basis.items(), key=lambda kv: -kv[1])[:PREVIEW_SAMPLE_LIMIT]),
    }


def actions_preview(
    actions: List[Dict[str, Any]], limit: int = PREVIEW_SAMPLE_LIMIT
) -> Dict[str, Any]:
    """Состав действий для отчёта прогона: сколько и какие именно.

    Без этого блока репетиция (--prod без --apply) показывала ровные нули и не
    содержала ни числа готовых действий, ни их состава — то есть главный
    артефакт для решения «включать ли боевую запись» не показывал, что именно
    было бы записано.

    Форма компактная и не растёт со числом действий: агрегат по видам настроек
    (их единицы) плюс несколько примеров с кампаниями. Полный список — в
    журнале действий, отчёт его не дублирует.
    """
    by_setting: Dict[str, int] = {}
    for action in actions:
        label = action_label(action)
        by_setting[label] = by_setting.get(label, 0) + 1
    return {
        "count": len(actions),
        "by_setting": dict(sorted(by_setting.items())),
        "sample": [f"кампания {a.get('object_id')}: {action_label(a)}"
                   for a in actions[:limit]],
        "sample_truncated": len(actions) > limit,
    }


def _tcpa_report(
    plan: Dict[str, Any], desired: Dict[str, Any],
    refused: Optional[List[Dict[str, Any]]] = None,
    planned_count: int = 0, not_found: Optional[List[str]] = None,
    cooled: Optional[List[Dict[str, Any]]] = None,
    limit: int = PREVIEW_SAMPLE_LIMIT,
) -> Dict[str, Any]:
    """Что прогон решил про цели CPA (Э3.5).

    Различаются те же виды молчания, что у бюджета: целей нет в расчёте,
    сдвиг мелкий, уверенность низкая или неизвестна, рычага у кампании нет
    (пакетная стратегия, нет носителя цели), кампанию уже трогали бюджетом
    или кулдауном.
    """
    return {
        "desired": len(desired),
        "small_shift": plan["small_shift"],
        "low_confidence": len(plan["low_confidence"]),
        "low_confidence_sample": plan["low_confidence"][:limit],
        "confidence_unknown": plan["confidence_unknown"],
        "refused": len(refused or []),
        "refused_sample": (refused or [])[:limit],
        "not_found": len(not_found or []),
        "cooldown": {"count": len(cooled or []), "sample": (cooled or [])[:limit]},
        "actions_planned": planned_count,
    }


def _budget_report(
    budget_plan: Dict[str, Any], desired: Dict[str, Any],
    refused: Optional[List[Dict[str, Any]]] = None,
    planned_count: int = 0, not_found: Optional[List[str]] = None,
    cooled: Optional[List[Dict[str, Any]]] = None,
    limit: int = PREVIEW_SAMPLE_LIMIT,
) -> Dict[str, Any]:
    """Что прогон решил про целевые бюджеты (Э3.3).

    Различаются четыре молчания, которые иначе неотличимы: сдвигов нет в
    расчёте, сдвиги есть но неуверенные/мелкие, сдвиг есть но рычага нет
    (пакетная стратегия, несвязывающий лимит), действие построено и пошло
    по конвейеру рельс.
    """
    out = {
        "desired": len(desired),
        "small_shift": budget_plan["small_shift"],
        "low_confidence": len(budget_plan["low_confidence"]),
        "cooldown": {"count": len(cooled or []),
                     "sample": (cooled or [])[:limit]},
        "low_confidence_sample": budget_plan["low_confidence"][:limit],
        "confidence_unknown": budget_plan["confidence_unknown"],
        "actions_planned": planned_count,
    }
    # Разведочные сдвиги — отдельной строкой. Они прошли планирование по
    # другому основанию (незнание, а не доказанная окупаемость), и слитые с
    # обычными в общий счётчик выглядели бы как уверенные решения агента.
    explored = budget_plan.get("exploration") or []
    if explored:
        out["exploration"] = {
            "count": len(explored),
            "rub": round(sum(float(r.get("exploration_rub") or 0.0)
                             for r in explored), 2),
            "sample": explored[:limit],
        }
    if refused is not None:
        by_reason: Dict[str, int] = {}
        for row in refused:
            by_reason[row["reason"]] = by_reason.get(row["reason"], 0) + 1
        out["refused"] = {"count": len(refused), "by_reason": by_reason,
                          "sample": refused[:limit]}
    if not_found:
        out["not_in_cabinet"] = not_found[:limit]
        out["not_in_cabinet_count"] = len(not_found)
    return out


def _switch_report(
    switch_plan: Dict[str, Any], desired: Dict[str, Any],
    refused: Optional[List[Dict[str, Any]]] = None,
    planned_count: int = 0, not_found: Optional[List[str]] = None,
    deferred_over_cap: int = 0,
    limit: int = PREVIEW_SAMPLE_LIMIT,
) -> Dict[str, Any]:
    """Что прогон решил про выключения кампаний (Э3.4).

    Кандидаты с их экономикой перечислены поимённо, а не счётчиком: их
    единицы (потолок — одно применение за прогон), и решение «остановить
    кампанию» человек обязан видеть с числами, по которым оно принято.
    """
    out = {
        "desired": len(desired),
        "candidates": [
            {"campaign_id": cid,
             "roi_share_of_lambda": round(m["roi_share"], 3),
             "roi_at_floor": round(m["roi_at_floor"], 3),
             "p_sign": m["p_sign"]}
            for cid, m in sorted(desired.items(),
                                 key=lambda kv: kv[1]["roi_share"])
        ][:limit],
        "low_confidence": len(switch_plan["low_confidence"]),
        "low_confidence_sample": switch_plan["low_confidence"][:limit],
        "confidence_unknown": switch_plan["confidence_unknown"],
        "actions_planned": planned_count,
        # Потолок выключений: сверх него — не отказ, ждёт следующего расчёта.
        "deferred_over_cap": deferred_over_cap,
        "max_per_run": switch.MAX_SUSPENDS_PER_RUN,
    }
    if refused is not None:
        by_reason: Dict[str, int] = {}
        for row in refused:
            by_reason[row["reason"]] = by_reason.get(row["reason"], 0) + 1
        out["refused"] = {"count": len(refused), "by_reason": by_reason,
                          "sample": refused[:limit]}
    if not_found:
        out["not_in_cabinet"] = not_found[:limit]
        out["not_in_cabinet_count"] = len(not_found)
    return out


def mark_stale_rows(login: str, journal_ok: bool) -> List[Dict[str, Any]]:
    """Помечает зависшие строки кабинета — или не трогает журнал в репетиции.

    Единственная точка, где прогон решает, менять ли состояние журнала:
    правило (journal_writes_allowed) общее со сторожем, и разъехаться им
    больше негде.
    """
    if not journal_ok:
        return []
    return writer_db.mark_stale_planned(STALE_PLANNED_MINUTES, account=login)


def _stale_report(rows: List[Dict[str, Any]], journal_ok: bool = True,
                  limit: int = PREVIEW_SAMPLE_LIMIT) -> Dict[str, Any]:
    """Зависшие строки для отчёта: сколько, какие, с какого времени.

    Здесь только ВПЕРВЫЕ обнаруженные — те, что этот прогон перевёл в статус
    'stale'. Уже помеченные не возвращаются mark_stale_planned и в отчёте не
    повторяются: отчёт читается глазами перед решением включать боевую
    запись, и вечная неизменная строка в нём быстро перестаёт читаться.

    В репетиции журнал не трогается вовсе, и отчёт говорит об этом прямо:
    иначе «ноль зависших строк» неотличимо от «мы их не искали».
    """
    return {
        "count": len(rows),
        "older_than_minutes": STALE_PLANNED_MINUTES,
        # Статус, в который прогон перевёл эти строки, — чтобы из отчёта было
        # видно, что с находкой что-то произошло, а не только напечаталось.
        "marked_status": "stale" if journal_ok else None,
        "journal_written": bool(journal_ok),
        "skipped_reason": None if journal_ok else REHEARSAL_STALE_REASON,
        "sample": [
            {"action_id": r.get("action_id"), "object_id": r.get("object_id"),
             "action_kind": r.get("action_kind"), "created_at": str(r.get("created_at"))}
            for r in rows[:limit]
        ],
    }


def segment_of(action: Dict[str, Any]) -> Tuple[str, str, str]:
    """Адрес действия для истории откатов: объект + сегмент, БЕЗ процента.

    Ровно это отличает обратную связь от идемпотентности: ключ идемпотентности
    привязан к значению, а вредным признаётся не значение, а трогание этого
    сегмента этой кампании.
    """
    return (str(action.get("object_id")), str(action.get("direct_type")),
            str(action.get("key")))


def split_by_cooldown(
    actions: List[Dict[str, Any]], cooled: Dict[Tuple[str, str, str], Any],
    cooldown_days: int = COOLDOWN_AFTER_ROLLBACK_DAYS,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Делит действия на разрешённые и запертые кулдауном.

    cooled — сегменты с вердиктом «вредно» (writer_db.harmful_segments):
    и откатанные, и те, чей откат не удался.
    """
    allowed: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    reason = COOLDOWN_REASON.format(days=cooldown_days)
    for action in actions:
        last = cooled.get(segment_of(action))
        if last is None:
            allowed.append(action)
        else:
            blocked.append({**action, "blocked_reason": reason,
                            "last_harmful_at": str(last)})
    return allowed, blocked


def split_by_attempts(
    actions: List[Dict[str, Any]], exhausted: Dict[Tuple[str, str, str], Any],
    max_attempts: int = writer_db.MAX_APPLY_ATTEMPTS,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Делит действия на живые и запертые исчерпанными попытками применения.

    exhausted — сегменты, накопившие потолок отказов (writer_db.
    exhausted_segments). Адрес сегмента тот же, что у кулдауна (segment_of):
    объект + вид + ключ, БЕЗ процента. По ключу идемпотентности счёт не
    работает вовсе — процент дрейфует между расчётами, ключ каждый прогон
    новый, и «третья попытка» никогда не наступает.

    Стоит ДО отбора по лимиту действий, тем же доводом, что кулдаун и отсев
    закрытых ключей: отсечённое здесь не занимает слотов прогона. Ровно этим
    вечная переотправка и была дорогой — она съедала лимит и риск-бюджет.
    """
    allowed: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    for action in actions:
        hit = exhausted.get(segment_of(action))
        if hit is None:
            allowed.append(action)
            continue
        attempts = (hit or {}).get("attempts") if isinstance(hit, dict) else hit
        blocked.append({**action,
                        "blocked_reason": EXHAUSTED_ATTEMPTS_REASON.format(
                            attempts=attempts, max_attempts=max_attempts),
                        "apply_attempts": attempts})
    return allowed, blocked


def split_by_final_keys(
    actions: List[Dict[str, Any]], final_keys: Any,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Делит действия на живые и уже закрытые финальным статусом журнала."""
    final_keys = set(final_keys or ())
    allowed = [a for a in actions if a["idempotency_key"] not in final_keys]
    blocked = [{**a, "blocked_reason": ALREADY_FINAL_REASON}
               for a in actions if a["idempotency_key"] in final_keys]
    return allowed, blocked


def computed_age_days(computed: List[Dict[str, Any]], today: date) -> Any:
    """Возраст расчёта в днях. None — даты нет, возраст неизвестен."""
    dates = []
    for row in computed:
        raw = row.get("calc_date")
        if isinstance(raw, datetime):
            dates.append(raw.date())
        elif isinstance(raw, date):
            dates.append(raw)
        elif isinstance(raw, str) and raw.strip():
            try:
                dates.append(date.fromisoformat(raw.strip()[:10]))
            except ValueError:
                continue
    if not dates:
        return None
    return (today - max(dates)).days


def computed_freshness_refusal(
    computed: List[Dict[str, Any]], today: date,
    max_age_days: int = MAX_COMPUTED_AGE_DAYS,
) -> Any:
    """Причина отказа применять расчёт из-за возраста, или None.

    Возраст неизвестен — тоже отказ: «неизвестно» не равно «свежо», а
    молчаливое применение недатированных коэффициентов ничем не отличается
    от применения месячных.
    """
    age = computed_age_days(computed, today)
    if age is None:
        return UNKNOWN_COMPUTED_DATE_REASON
    if age > max_age_days:
        latest = today - timedelta(days=age)
        return STALE_COMPUTED_REASON.format(
            max_days=max_age_days, calc_date=latest.isoformat(), age=age)
    return None


def absolute_max_cpa_from_baseline(baseline_cpa: Dict[str, float]) -> Any:
    """Абсолютный аварийный порог красной линии: медиана известных базовых
    CPA × ABSOLUTE_MAX_CPA_MULTIPLIER. None, если справочник пуст целиком —
    медианы не существует, порог из данных не выводится.
    """
    med = median(baseline_cpa.values())
    if med is None:
        return None
    return round(med * ABSOLUTE_MAX_CPA_MULTIPLIER, 2)


def build_red_line(
    action: Dict[str, Any], baseline_cpa: Dict[str, float], absolute_max_cpa: Any,
    baseline_window: Any = None, baseline_cpo: Optional[Dict[str, float]] = None,
    baseline_volume: Optional[Dict[str, Dict[str, float]]] = None,
) -> Any:
    """Красная линия для действия, или None, если её посчитать не из чего.

    У кампании есть собственный baseline_cpa (>0) — относительный порог,
    absolute_max_cpa не нужен (red_line_for уйдёт в относительную ветку и
    не тронет этот параметр, даже если он None). Нет — нужен absolute_max_cpa
    (медиана по справочнику, см. absolute_max_cpa_from_baseline); если и его
    нет (справочник baseline_cpa пуст целиком), у действия не будет
    работающей красной линии вообще — применять его нельзя, вызывающий код
    обязан исключить его до apply_actions, а не передавать дальше с тихим
    дефолт-плейсхолдером.

    red_line_for после правки по код-ревью не имеет дефолта для
    absolute_max_cpa — вызов без явного порога стал бы TypeError. Ветка
    "своей базы нет и абсолютного порога тоже нет" уже отсечена гардом выше,
    поэтому здесь всегда безопасно передать absolute_max_cpa как есть.
    """
    baseline = {"cpa": baseline_cpa.get(str(action["object_id"]), 0.0)}
    # Цена оплаты кампании на том же окне. Красная линия её не читает — по
    # оплатам не откатывают, они дозревают дольше наблюдения. Едет она ради
    # ВТОРОГО чекпоинта: через 35 дней сторож сверяет вердикт по заявкам с
    # деньгами (money_verdict), и база к тому времени берётся уже неоткуда.
    # Кампании без оплат за окно в справочнике нет — тогда поля не будет, и
    # сверка честно скажет «unknown» вместо выдуманного успеха.
    cpo = (baseline_cpo or {}).get(str(action["object_id"]))
    if cpo:
        baseline["cpo"] = cpo
    if baseline_window:
        # Границы окна базы едут в саму линию: по ним сторож считает сезонную
        # поправку порога (agent_e1_watchdog.seasonal_factor).
        baseline["window_from"], baseline["window_to"] = baseline_window
    # Темп базы едет в линию ради сверки ожидания с фактом: сторож знает лиды
    # окна наблюдения, а сколько их было ДО изменения — больше ниоткуда не
    # берётся, кампанию с тех пор трогали. Красная линия его не читает:
    # откатывают по цене, а не по объёму.
    volume = (baseline_volume or {}).get(str(action["object_id"])) or {}
    if volume.get("leads_per_day"):
        baseline["leads_per_day"] = volume["leads_per_day"]
    if baseline["cpa"] <= 0 and absolute_max_cpa is None:
        return None
    return red_line_for(action, baseline, absolute_max_cpa)


def _actual_modifiers(client: WriteClient, campaign_id: str) -> List[Dict[str, Any]]:
    """Свежее состояние корректировок кампании: между прогонами кабинет могли
    править руками, поэтому читаем заново на каждом прогоне, а не берём из журнала.

    Levels обязателен и лежит ВНУТРИ SelectionCriteria (probe задачи 1, факт
    подтверждён прогонами 32217815538 и др.) — без него запрос отвергается
    ошибкой 8000 «Отсутствует обязательный параметр Levels».
    """
    result = client.get("bidmodifiers", {
        "SelectionCriteria": {"CampaignIds": [int(campaign_id)], "Levels": ["CAMPAIGN"]},
        "FieldNames": ["Id", "CampaignId", "Type"],
        "MobileAdjustmentFieldNames": ["BidModifier"],
        "DesktopAdjustmentFieldNames": ["BidModifier"],
        "TabletAdjustmentFieldNames": ["BidModifier"],
        "DemographicsAdjustmentFieldNames": ["BidModifier", "Gender", "Age"],
        "RegionalAdjustmentFieldNames": ["BidModifier", "RegionId"],
    })
    out: List[Dict[str, Any]] = []
    for item in result.get("BidModifiers") or []:
        out += _normalize_actual(item)
    return out


def _actual_time_targeting(
    client: WriteClient, campaign_ids: List[int]
) -> Dict[str, Dict[str, Any]]:
    """Текущее расписание кампаний — ОДНИМ запросом на кабинет, не по кампании.

    Расписание живёт в самой кампании, поэтому читается через campaigns.get, а
    не через bidmodifiers. Постранично, как и остальные чтения: страница
    короче лимита — конец.

    Кампания без блока TimeTargeting в ответе — это «ровные сотни», а не
    «данных нет»: пустой словарь так и трактуется построителем расписания.
    """
    out: Dict[str, Dict[str, Any]] = {}
    ids = [int(c) for c in campaign_ids]
    if not ids:
        return out
    for start in range(0, len(ids), CAMPAIGN_PAGE_LIMIT):
        chunk = ids[start:start + CAMPAIGN_PAGE_LIMIT]
        result = client.get("campaigns", {
            "SelectionCriteria": {"Ids": chunk},
            "FieldNames": ["Id", "TimeTargeting"],
        })
        for item in result.get("Campaigns") or []:
            out[str(item.get("Id"))] = item.get("TimeTargeting") or {}
    return out


def _schedule_report(
    hours: List[Dict[str, Any]], items: List[str],
    targeting_by_campaign: Dict[str, Dict[str, Any]], campaign_ids: List[int],
) -> Dict[str, Any]:
    """Что прогон решил про почасовое расписание.

    Различать нужно три состояния, которые иначе сливаются в тишину: профиля
    нет вовсе (порогов не прошёл ни один час), профиль есть и где-то отличается
    от кабинета, профиль есть и везде уже стоит.
    """
    if not items:
        return {"significant_hours": 0,
                "reason": "ни один час не прошёл пороги значимости — расписание не трогаем"}

    up, down, neutral = describe_schedule(items)
    differs = sum(1 for cid in campaign_ids
                  if schedule_changed(targeting_by_campaign.get(str(cid)) or {}, items))
    return {
        "significant_hours": len(hours),
        "hours_up": up,
        "hours_down": down,
        "hours_neutral": neutral,
        "campaigns_differing": differs,
        "campaigns_already_matching": len(campaign_ids) - differs,
        "sample": items[0] if items else None,
    }


def run_account(
    login: str, sandbox: bool, dry_run: bool, today: str, ctx: Dict[str, Any]
) -> Dict[str, Any]:
    """Весь прогон по ОДНОМУ кабинету. Возвращает его отчёт.

    Вынесено из main() целиком, чтобы отказ одного кабинета можно было
    поймать снаружи одной точкой: раньше исключение на первом из четырёх
    кабинетов означало, что три остальных не обрабатывались вовсе — и не
    потому, что было нечего делать, а потому, что прогон умер.
    """
    daily_cost = ctx["daily_cost"]
    cost_28d_by_campaign = ctx.get("cost_28d_by_campaign") or {}
    baseline_cpa = ctx["baseline_cpa"]
    baseline_window = ctx.get("baseline_window")
    absolute_max_cpa = ctx["absolute_max_cpa"]
    holdout_ids = ctx["holdout_ids"]
    charged_risk = ctx["charged_risk"]
    wk = ctx["week_start"]
    lease = ctx.get("lease")

    # Клиент создаётся ДО первого решения о журнале: конструктор в сеть не
    # ходит (токен резолвится лениво), зато режим прогона дальше берётся из
    # одного места.
    client = WriteClient(login, sandbox=sandbox, dry_run=dry_run)
    # Право менять состояние журнала — по тому же правилу, что у сторожа.
    journal_ok = journal_writes_allowed(sandbox, dry_run)

    # Настройки — этого кабинета, а не общий набор на всех: числа посчитаны
    # по его аудитории и применимы только к его кампаниям.
    computed = agent_db.load_latest_computed_settings(login)
    # Возраст расчёта — рельса того же рода, что и остальные: применить
    # месячные коэффициенты хуже, чем не применить ничего.
    stale_computed = (computed_freshness_refusal(computed, date.fromisoformat(today))
                      if computed else None)
    plan = (plan_bid_modifiers(computed, thresholds=ctx["thresholds"])
           if not stale_computed
            else {"desired": [], "unsupported": [],
                  "low_confidence": [], "confidence_unknown": 0})
    desired = plan["desired"]
    # Значимые настройки, которые агент применить не умеет (нечисловой ключ
    # региона, устройство вне DESKTOP/MOBILE/TABLET), собираются с ОБОИХ
    # уровней ниже, когда прочитаны покампанийные строки. Они не подставляются
    # в чужой тип корректировки и не роняют применение — но и не пропадают
    # молча: причина видна в отчёте прогона.

    # Расписание считается ДО раннего выхода. Стой этот расчёт ниже, кабинет
    # без значимых корректировок возвращал бы «нечего делать» и молча уносил
    # с собой посчитанный почасовой профиль: у расписания свой механизм, и
    # отсутствие корректировок ничего о нём не говорит.
    #
    # Устаревшие настройки блокируют его так же, как корректировки: профиль
    # посчитан по тем же данным, и «данные протухли» не перестаёт быть правдой
    # оттого, что механизм применения другой.
    schedule_hours = plan_schedule(computed) if not stale_computed else []
    desired_items = schedule_items(schedule_hours) if schedule_hours else []

    # Рулинг 1: кампании только этого кабинета, не всего справочника расходов.
    # Здесь же применяется ограничитель прогона (CampaignScope): дальше по
    # течению про него никто не знает. Список читается ДО раннего выхода:
    # покампанийные строки (Э2.2) лежат под object_id=кампания, и без списка
    # кампаний кабинета их существование в принципе не проверить — ранний
    # выход «у кабинета нет корректировок» молча прятал бы личные значения.
    scope = ctx["campaign_scope"]
    campaign_ids = own_campaign_ids(client, daily_cost, scope)

    # Э2.2: личные значения кампаний. Приоритет — «кампания, если есть, иначе
    # кабинет», по каждому виду настройки целиком: у кампании с личным набором
    # кабинетные строки корректировок не применяются вовсе (набор считался тем
    # же расчётом и уже содержит всё, что кампания заслуживает; смешение двух
    # уровней дало бы кабинетный сегмент поверх личного). Свежесть личных
    # строк проверяется той же рельсой, что у кабинетных: устаревший личный
    # набор не «падает обратно» на свежий кабинетный молча — обе даты из
    # одного прогона Э0, а разъехались они только если Э0 упал посередине,
    # и тогда честнее применить кабинетный уровень с явным счётчиком отказов.
    campaign_computed = agent_db.load_latest_campaign_computed(campaign_ids)
    campaign_desired: Dict[str, List[Dict[str, Any]]] = {}
    campaign_unsupported_rows: List[Dict[str, Any]] = []
    campaign_stale_dropped = 0
    # Свежие покампанийные строки целиком — вход рычага бюджета (Э3.3):
    # budget_target лежит в тех же строках, что и корректировки, и той же
    # рельсой свежести отсеивается.
    fresh_campaign_computed: Dict[str, List[Dict[str, Any]]] = {}
    # Э2.3: неуверенные строки обоих уровней — счётчик и образцы в отчёт.
    low_confidence_rows: List[Dict[str, Any]] = list(plan["low_confidence"])
    confidence_unknown = int(plan["confidence_unknown"])
    for cid, rows in campaign_computed.items():
        if computed_freshness_refusal(rows, date.fromisoformat(today)):
            campaign_stale_dropped += 1
            continue
        fresh_campaign_computed[str(cid)] = rows
        camp_plan = plan_bid_modifiers(rows, thresholds=ctx["thresholds"])
        campaign_unsupported_rows += camp_plan["unsupported"]
        low_confidence_rows += [{**r, "campaign_id": str(cid)}
                                for r in camp_plan["low_confidence"]]
        confidence_unknown += int(camp_plan["confidence_unknown"])
        if camp_plan["desired"]:
            campaign_desired[str(cid)] = camp_plan["desired"]
    unsupported = _unsupported_report(
        plan["unsupported"] + campaign_unsupported_rows)
    confidence_report = {
        # Отсеянные по уверенности — решения «не делать», и они такие же
        # видимые, как сделанные: корректировки-монетки не применяются.
        "low_confidence": len(low_confidence_rows),
        "sample": low_confidence_rows[:5],
        # Строки без rel_error (расчёт до Э2.3): применяются как раньше,
        # но их количество обязано быть на виду до следующего прогона Э0.
        "unknown": confidence_unknown,
    }
    campaign_level_report = {
        "campaigns_with_own_values": len(campaign_desired),
        "fallback_account": len(campaign_ids) - len(campaign_desired),
        "stale_dropped": campaign_stale_dropped,
    }

    # Э3.3: целевые бюджеты. План считается ДО раннего выхода тем же доводом,
    # что расписание: у кампании может не быть ни одной значимой корректировки
    # и при этом быть уверенный сдвиг бюджета — «нечего делать» по
    # корректировкам ничего не говорит о бюджете. Ограничитель прогона уже
    # применён к campaign_ids, и сдвиги за его пределами не планируются.
    budget_plan = budget.plan_budget_moves(fresh_campaign_computed,
                                          thresholds=ctx["thresholds"])
    scoped_ids = {str(c) for c in campaign_ids}
    budget_desired = {cid: m for cid, m in budget_plan["desired"].items()
                      if cid in scoped_ids}
    # Кулдаун бюджета: кампании, чей лимит трогали за последние 14 дней, в
    # этот такт не трогаются. Правило «не больше ±20 % за 14 дней» держится
    # двумя рельсами: капом шага на записи (budget.clamp_write_step в
    # diff_budget) и этим кулдауном — по журналу применённых действий, а не
    # по расчёту.
    budget_desired, budget_cooled = budget.apply_cooldown(
        budget_desired,
        writer_db.recent_action_objects(
            (budget.BUDGET_KIND, budget.BUDGET_DAILY_KIND),
            int(ctx["config"]["budget_cooldown_days"]), account=login))

    # Э3.5: цели CPA — план ДО раннего выхода, тем же правилом, что бюджет.
    tcpa_plan = tcpa.plan_tcpa_moves(fresh_campaign_computed,
                                     thresholds=ctx["thresholds"])

    # Э3.6: минус-фразы. Кандидатов посчитал Э0 и разложил по кампаниям
    # строками computed; здесь они собираются обратно в фразы и режутся
    # капом такта: отсечённый трафик не вернуть, и такт обязан оставаться
    # различимым в наблюдении.
    # Порог отсечения — МЕДИАННЫЙ базовый CPA кабинета, той же формулой, что в
    # Э0 (agent_e0: baselines[len // 2] по load_baseline_cpa). Не копия числа,
    # а тот же расчёт на свежем окне CRM: кандидаты отбирались по нему, и класс
    # достоверности обязан судить их по нему же, а не по второму порогу.
    cut_baseline_cpa = median([v for v in baseline_cpa.values() if float(v) > 0])

    negatives_plan = negatives.plan_negatives(
        negatives.candidates_from_computed(fresh_campaign_computed))

    # Э3.7: площадки сети — тот же рычаг, что минус-фразы, на другом списке.
    placements_plan = placements.plan_placements(
        placements.candidates_from_computed(fresh_campaign_computed))

    # Э3.4: кандидаты на выключение — тем же правилом, что бюджет: план ДО
    # раннего выхода, ограничитель прогона уже в scoped_ids.
    switch_plan = switch.plan_switch_offs(fresh_campaign_computed,
                                          thresholds=ctx["thresholds"])
    switch_desired = {cid: m for cid, m in switch_plan["desired"].items()
                      if cid in scoped_ids}

    if (not desired and not desired_items and not campaign_desired
            and not budget_desired and not switch_desired):
        if stale_computed:
            verdict, reason = "STALE_COMPUTED_SETTINGS", stale_computed
        elif not computed and not campaign_computed:
            verdict, reason = "NO_COMPUTED_SETTINGS", NO_COMPUTED_REASON.format(login=login)
        else:
            verdict, reason = "NOTHING_TO_DO", "нет значимых корректировок"
        return {
            "account": login,
            "verdict": verdict,
            "reason": reason,
            "computed_settings": len(computed),
            "computed_age_days": computed_age_days(computed, date.fromisoformat(today)),
            # Расписание — и в коротком отчёте тоже: причина, по которой прогон
            # ничего не делает, обязана быть видна целиком. Без этого поля
            # «профиль не прошёл пороги» и «профиль есть, но корректировок нет»
            # выглядели бы одинаково.
            "schedule": _schedule_report(schedule_hours, desired_items, {}, []),
            "budget": _budget_report(budget_plan, budget_desired,
                                     cooled=budget_cooled),
            "switch": _switch_report(switch_plan, switch_desired),
            "tcpa": _tcpa_report(tcpa_plan, {}),
            "negatives": {"campaigns": 0, "phrases": 0,
                          "over_cap": negatives_plan["over_cap"],
                          "invalid": len(negatives_plan["invalid"]),
                          "cost_covered": negatives_plan["cost_covered"],
                          "refused": 0, "not_found": 0, "actions_planned": 0},
            "placements": {"campaigns": 0, "sites": 0,
                           "over_cap": placements_plan["over_cap"],
                           "invalid": len(placements_plan["invalid"]),
                           "cost_covered": placements_plan["cost_covered"],
                           "refused": 0, "not_found": 0, "actions_planned": 0},
            "unsupported": unsupported,
            "campaign_level": campaign_level_report,
            "confidence": confidence_report,
            # Ни одной кампании не тронуто — и это сказано явно тем же полем,
            # что и в полном отчёте, а не отсутствием поля.
            "campaign_scope": scope.report(),
            "campaigns_touched": 0,
            "stale_planned": _stale_report(mark_stale_rows(login, journal_ok),
                                           journal_ok),
        }

    planned: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    unusable_actual = 0

    # Почасовой профиль общий для кабинета (Метрика даёт его по счётчикам, не
    # по кампаниям), а вот текущее расписание у каждой кампании своё — поэтому
    # план считается один раз, а сравнение идёт по каждой.
    targeting_by_campaign = (_actual_time_targeting(client, campaign_ids)
                             if desired_items else {})

    for campaign_id in campaign_ids:
        # Самая длинная часть прогона: чтение состояния по каждой кампании, с
        # ретраями и таймаутом в две минуты на запрос. Именно здесь прогон
        # переживает срок аренды — поэтому аренда продлевается на каждом шаге
        # цикла, а не только берётся в начале.
        if lease is not None:
            lease.guard()
        actual = _actual_modifiers(client, campaign_id)
        unusable_actual += sum(1 for a in actual if a.get("unusable"))
        # Личный план кампании, если есть, иначе кабинетный (Э2.2).
        own_desired = campaign_desired.get(str(campaign_id))
        econ = campaign_expectation_context(campaign_id, daily_cost,
                                            fresh_campaign_computed)
        campaign_actions = list(
            diff_modifiers(own_desired if own_desired else desired,
                           actual, campaign_id, econ))
        if desired_items:
            campaign_actions += diff_schedule(
                desired_items, targeting_by_campaign.get(str(campaign_id)) or {},
                campaign_id, econ)
        for action in campaign_actions:
            ok, reason = check_action(action)
            if not ok:
                blocked.append({**action, "blocked_reason": reason})
                continue
            planned.append({**action, "account": login})

    # Э3.3: действия по бюджету. Состояние читается СВЕЖИМ (между прогонами
    # лимиты правят руками), previous_state берётся из этого чтения. Дальше
    # действия идут общим конвейером рельс: заповедник, кулдаун, потолок
    # попыток, закрытые ключи, лимит действий, красная линия, риск-бюджет.
    budget_state = (budget.fetch_budget_state(client, sorted(budget_desired))
                    if budget_desired else {})
    budget_spend_no_vat = {
        cid: m["cost_28d"] / budget.WEEKS_IN_WINDOW / budget.VAT
        for cid, m in budget_desired.items()
    }
    budget_actions, budget_refused = budget.diff_budget(
        budget_desired, budget_state, budget_spend_no_vat,
        max_write_step=float(ctx["config"]["max_write_step"]))
    budget_not_found = sorted(c for c in budget_desired if c not in budget_state)
    budget_planned_count = 0
    for action in budget_actions:
        ok, reason = check_action(action, cost_28d_by_campaign)
        if not ok:
            blocked.append({**action, "blocked_reason": reason})
            continue
        budget_planned_count += 1
        planned.append({**action, "account": login})

    # Э3.5: цель CPA. Состояние берётся из ТОГО ЖЕ чтения, что бюджет
    # (fetch_budget_state уже принёс BiddingStrategy) — второй поход в API за
    # тем же был бы лишним. Кампания, у которой этим тактом уже двигали
    # бюджет, целью не трогается: две денежные ручки разом делают исход
    # неразличимым, и красная линия не скажет, какая из них навредила.
    tcpa_desired = {cid: m for cid, m in tcpa_plan["desired"].items()
                    if cid in scoped_ids and cid not in budget_desired}
    # Кулдаун — общий механизм денежных ручек, живёт у бюджета: правило
    # «не чаще раза в 14 дней» одно на обе, и второй копии ему не нужно.
    tcpa_desired, tcpa_cooled = budget.apply_cooldown(
        tcpa_desired,
        writer_db.recent_action_objects(
            (tcpa.TCPA_KIND,),
            int(ctx["config"]["budget_cooldown_days"]), account=login))
    tcpa_state = (budget.fetch_budget_state(client, sorted(tcpa_desired))
                  if tcpa_desired else {})
    tcpa_actions, tcpa_refused = tcpa.diff_tcpa(tcpa_desired, tcpa_state)
    tcpa_not_found = sorted(c for c in tcpa_desired if c not in tcpa_state)
    tcpa_planned_count = 0
    for action in tcpa_actions:
        ok, reason = check_action(action, cost_28d_by_campaign)
        if not ok:
            blocked.append({**action, "blocked_reason": reason})
            continue
        tcpa_planned_count += 1
        planned.append({**action, "account": login})

    # Э3.6: применение минус-фраз. Список читается СВЕЖИМ и заменяется
    # целиком объединением прежних и новых фраз: между прогонами его правят
    # руками, и затирать чужие фразы рычаг не вправе.
    negatives_desired = {cid: phrases
                         for cid, phrases in negatives_plan["desired"].items()
                         if cid in scoped_ids}
    negatives_state = (negatives.fetch_negatives(client, sorted(negatives_desired))
                       if negatives_desired else {})
    # cut_conversions и baseline_cpa едут вместе с расходом, а не остаются
    # значениями по умолчанию: первое — сколько лидов отсечение теряет
    # (обещание рычага), второе — порог, по которому кандидат и выбран. Без
    # второго действие не может показать своё основание, и класс 0
    # («арифметика, риском не платит, вносится весь и сразу») не наступает ни
    # для одной минус-фразы, сколько бы их ни насчитал Э0.
    negatives_actions, negatives_refused = negatives.diff_negatives(
        negatives_desired, negatives_state,
        cut_cost=negatives_plan.get("cut_cost"),
        cut_conversions=negatives_plan.get("cut_conversions"),
        baseline_cpa=cut_baseline_cpa)
    negatives_not_found = sorted(c for c in negatives_desired
                                 if c not in negatives_state)
    negatives_planned_count = 0
    for action in negatives_actions:
        ok, reason = check_action(action)
        if not ok:
            blocked.append({**action, "blocked_reason": reason})
            continue
        negatives_planned_count += 1
        planned.append({**action, "account": login})

    # Э3.7: применение запретов площадок — как минус-фразы: свежее чтение,
    # объединение с прежним списком, кап такта.
    placements_desired = {cid: sites
                          for cid, sites in placements_plan["desired"].items()
                          if cid in scoped_ids}
    placements_state = (placements.fetch_excluded_sites(
        client, sorted(placements_desired)) if placements_desired else {})
    placements_actions, placements_refused = placements.diff_placements(
        placements_desired, placements_state,
        cut_cost=placements_plan.get("cut_cost"),
        cut_conversions=placements_plan.get("cut_conversions"),
        baseline_cpa=cut_baseline_cpa)
    placements_not_found = sorted(c for c in placements_desired
                                  if c not in placements_state)
    placements_planned_count = 0
    for action in placements_actions:
        ok, reason = check_action(action)
        if not ok:
            blocked.append({**action, "blocked_reason": reason})
            continue
        placements_planned_count += 1
        planned.append({**action, "account": login})

    # Э3.4: выключения. Состояние (State) читается свежим; потолок — своя
    # рельса ДО общего конвейера: пропущенное сверх него не «заблокировано»,
    # а отложено до следующего расчёта (пересчёт портфеля без выключенной
    # кампании может снять кандидатуру с остальных).
    switch_states = (switch.fetch_campaign_states(client, sorted(switch_desired))
                     if switch_desired else {})
    switch_actions, switch_refused = switch.diff_switch(switch_desired, switch_states)
    switch_not_found = sorted(c for c in switch_desired if c not in switch_states)
    switch_actions, switch_deferred = switch.cap_suspends(
        switch_actions, max_per_run=int(ctx["config"]["max_suspends_per_run"]))
    switch_planned_count = 0
    for action in switch_actions:
        ok, reason = check_action(action)
        if not ok:
            blocked.append({**action, "blocked_reason": reason})
            continue
        switch_planned_count += 1
        planned.append({**action, "account": login})

    allowed, in_holdout = check_holdout(planned, holdout_ids)
    blocked += [{**a, "blocked_reason": "заповедник"} for a in in_holdout]

    # Обратная связь от отката к планированию. Стоит ДО отбора по лимиту:
    # отсечённое здесь не занимает слотов прогона. Тот же довод — у отсева
    # уже закрытых ключей ниже.
    cooled = writer_db.harmful_segments(COOLDOWN_AFTER_ROLLBACK_DAYS, account=login)
    allowed, in_cooldown = split_by_cooldown(allowed, cooled)
    blocked += in_cooldown

    # Кулдаун ОБУЧЕНИЯ стратегии. Не второй кулдаун денежных ручек, а его
    # продолжение поперёк видов действий: тот же журнал, то же число дней
    # (learning.LEARNING_COOLDOWN_DAYS берётся из budget.BUDGET_COOLDOWN_DAYS),
    # но история читается по КЛАССУ влияния на обучение, а не по виду
    # действия. Денежный кулдаун выше (budget.apply_cooldown на стадии
    # desired) видит только собственный вид: бюджет не знает, что цель CPA
    # той же кампании перезапустила обучение три дня назад, а остановка
    # кампании не под ним вовсе. Действие, запертое там, сюда не доходит —
    # у одного действия ровно один запрет и одна формулировка причины.
    #
    # Место — там же, где кулдаун вредных сегментов, до отбора по лимиту
    # действий: отсечённое здесь не занимает слотов прогона.
    last_resets = writer_db.last_learning_reset(login)
    allowed, in_learning_cooldown = split_by_learning_cooldown(
        allowed, last_resets, today=date.fromisoformat(today))
    blocked += in_learning_cooldown

    # Потолок попыток применения — там же, где кулдаун, и по той же причине:
    # сегмент, по которому API отказывает детерминированно, обязан выпасть ДО
    # лимита действий, а не занимать его слот каждый прогон.
    exhausted = writer_db.exhausted_segments(account=login)
    allowed, out_of_attempts = split_by_attempts(allowed, exhausted)
    blocked += out_of_attempts

    # Пометка зависших строк стоит ДО чтения закрытых ключей. Порядок был
    # обратным, и это стоило слота: строка, зависшая в 'planned' с прошлого
    # прогона, на прогоне ОБНАРУЖЕНИЯ ещё не была закрыта финальным статусом
    # (её только сейчас переводят в 'stale'), поэтому final_status_keys её не
    # видел — действие доходило до отбора по лимиту, занимало слот и помечало
    # свой объект оплаченным риск-бюджетом. Теперь пометка происходит раньше,
    # и ключ уже закрыт к моменту чтения.
    stale = mark_stale_rows(login, journal_ok)

    already_final = writer_db.final_status_keys(a["idempotency_key"] for a in allowed)
    allowed, closed_keys = split_by_final_keys(allowed, already_final)
    blocked += closed_keys

    # Баланс такта: сокращение без адресата роста не применяется. Стоит в том
    # же ряду, что кулдауны, и по той же причине — до отбора по лимиту:
    # снятое здесь не должно занимать слот прогона. Но ПОСЛЕДНИМ в ряду, а не
    # сразу после кулдауна обучения: все гейты стоят ПОСЛЕ солвера, и доливка,
    # запертая потолком попыток или закрытым ключом, обязана быть уже
    # вычтенной — иначе её рубли считались бы назначенными, хотя в кабинет
    # они не поедут.
    cut_cost_by_kind = {
        negatives.NEGATIVE_KIND: negatives_plan.get("cut_cost") or {},
        placements.PLACEMENT_KIND: placements_plan.get("cut_cost") or {},
    }
    balance = tact_balance(**balance_inputs(
        allowed, budget_desired, cost_28d_by_campaign, cut_cost_by_kind))
    allowed, without_address = require_growth_address(allowed, balance)
    blocked += without_address

    # Порядок рельс: сначала отсекается всё, что применять нельзя или
    # незачем (лимит прогона, отсутствие красной линии), и только потом
    # считается бюджет. Обратный порядок списывал бы риск за действия,
    # которые дальше отваливаются, — объект помечался бы оплаченным, а
    # изменение по нему так и не уходило бы в кабинет.
    # Противоречия внутри собранного плана. Каждый рычаг считает своё и
    # по-своему; несовместимость двух законных решений видна только когда они
    # оказались в одном прогоне на одном объекте. Разбор стоит ДО лимита
    # прогона по той же причине, что и кулдауны: снятая конфликтная пара не
    # должна занимать слот, а применённая — стоить риска дважды и испортить
    # наблюдение обоим участникам.
    allowed, in_conflict = conflicts.resolve(allowed)
    blocked += [{**a, "blocked_reason": a.get("conflict_reason")}
                for a in in_conflict]

    allowed, over_cap = cap_actions(allowed, max_per_run=max(ctx["remaining_cap"], 0))

    # Красная линия ставится ВМЕСТЕ с действием: у каждого применённого
    # изменения заранее известно, при каком исходе оно считается провалом.
    # build_red_line возвращает None, если её посчитать не из чего — такое
    # действие не применяется, причина уходит в no_red_line, а не в тихий
    # дефолт-плейсхолдер.
    with_red_line: List[Dict[str, Any]] = []
    no_red_line: List[Dict[str, Any]] = []
    for a in allowed:
        red_line = build_red_line(a, baseline_cpa, absolute_max_cpa,
                                  baseline_window, ctx.get("baseline_cpo"),
                                  ctx.get("baseline_volume"))
        if red_line is None:
            no_red_line.append({**a, "blocked_reason": NO_RED_LINE_REASON})
            continue
        with_red_line.append({**a, "red_line": red_line})

    # Цена действия — его ДЕЛЬТА (доля сегмента, вырезанный трафик, сдвиг
    # лимита), а не расход всей кампании; потолок объекта не даёт сумме дельт
    # превысить его расход за горизонт. Разбор — sync/agent/writer/exposure.py.
    # Базовый расход и основание цены едут со строкой в журнал: первый читает
    # красная линия обвала, второе — человек, проверяющий модель.
    risks: Dict[str, float] = {}
    caps: Dict[str, float] = {}
    priced: List[Dict[str, Any]] = []
    for a in with_red_line:
        risks[a["idempotency_key"]] = action_risk(a, daily_cost)
        caps[risk_object(a)] = object_cap(a, daily_cost)
        baseline = object_daily_cost(a, daily_cost)
        priced.append({
            **a,
            "baseline_daily_rub": None if baseline == float("inf") else round(baseline, 2),
            "risk_basis": action_risk_basis(a, daily_cost),
            # Класс влияния на обучение едет в журнал ВМЕСТЕ со строкой —
            # одним местом на все виды действий, включая безопасные. Иначе
            # колонка была бы заполнена наполовину, и «безопасно» не
            # отличалось бы от «класс не приписан».
            "learning_impact": learning_impact(a),
        })
    # Бюджет читается заново для каждого кабинета: он общий на весь прогон,
    # а не на кабинет, и предыдущий клиент этого же прогона мог его уже
    # частично занять (spent_risk читает applied_at из журнала, куда
    # apply_actions уже успел записать применённые действия).
    weekly_left = (weekly_risk_limit(wk, daily_cost, ctx.get("config"))
                   - writer_db.spent_risk(wk))
    # Из двух потолков берётся меньший: остаток НЕДЕЛИ и доля СЕГОДНЯШНЕГО
    # дня. Второй общий на все кабинеты прогона, поэтому он не перечитывается,
    # а уменьшается по мере трат — ровно как лимит действий рядом.
    remaining = min(weekly_left, ctx["run_risk_remaining"])
    prepared, deferred = fit_into_budget(priced, risks, remaining,
                                         charged_risk, caps)
    ctx["remaining_cap"] -= len(prepared)
    ctx["run_risk_remaining"] -= sum(float(a.get("risk_rub") or 0.0) for a in prepared)

    # Реестр гипотез: разведочная ставка заводится ДО отправки в кабинет и
    # ровно здесь — риск-бюджет уже назвал её цену (risk_rub), а действие ещё
    # не ушло. Заводить раньше значило бы держать в очереди замыслы, которые
    # срежет бюджет; позже — потерять ставку, если отправка упадёт на середине.
    #
    # Идентификатор ставки выводится из action_id, а тот детерминирован по
    # ключу идемпотентности (writer_db.make_action_id), поэтому посмертная
    # запись сторожа ляжет в ЭТУ ЖЕ строку, а не заведёт вторую.
    bets = [experiments.open_bet(
                a, writer_db.make_action_id(a["idempotency_key"]), today)
            for a in prepared if experiments.is_bet(a)]
    if bets and not dry_run:
        agent_db.upsert_hypotheses(bets)

    report = apply_actions(client, prepared, writer_db, lease=lease)

    conflict_groups = [
        (reason, [a for a in in_conflict if a.get("conflict_reason") == reason])
        for reason in sorted(conflicts.by_reason(in_conflict))
    ]
    account_rejects = rejects.from_groups(conflict_groups + [
        (rejects.BUDGET, deferred),
        (rejects.RUN_CAP, over_cap),
        (rejects.NO_RED_LINE, no_red_line),
        (rejects.COOLDOWN, in_cooldown),
        (rejects.ATTEMPTS_EXHAUSTED, out_of_attempts),
        (rejects.HOLDOUT, in_holdout),
        (rejects.LEARNING_COOLDOWN, in_learning_cooldown),
        (rejects.CLOSED_KEY, closed_keys),
        (rejects.NO_GROWTH_ADDRESS, without_address),
    ], account=login, stage="e1", risks=risks)

    return {
        "account": login,
        # Строки отказов едут отдельным полем от их счётчиков: счётчики
        # читает человек в логе, строки — чёрный ящик.
        "_rejects": account_rejects,
        "sandbox": sandbox,
        "dry_run": dry_run,
        "own_campaigns": len(campaign_ids),
        # Ограничитель прогона и его остаток — в отчёте всегда, а не только
        # когда он включён: «ограничение не сработало» и «ограничения не
        # было» обязаны различаться при чтении отчёта первого боевого прогона.
        "campaign_scope": scope.report(),
        # Сколько кампаний прогон ФАКТИЧЕСКИ тронул: столько разных кампаний
        # среди действий, дошедших до отправки. Отдельным полем, потому что
        # ни одно из соседних чисел на этот вопрос не отвечает: own_campaigns
        # это сколько прочитано, prepared.count — сколько действий, а на одну
        # кампанию их приходится несколько.
        "campaigns_touched": len({str(a["object_id"]) for a in prepared}),
        "computed_settings": len(computed),
        "computed_age_days": computed_age_days(computed, date.fromisoformat(today)),
        # Почасовое расписание — отдельной строкой: оно не корректировка и в
        # счётчики desired/unsupported не попадает. Без этого «профиля нет» и
        # «профиль посчитан, но во всех кампаниях уже стоит» выглядели бы
        # одинаково — как молчание.
        "schedule": _schedule_report(schedule_hours, desired_items,
                                     targeting_by_campaign, campaign_ids),
        "budget": _budget_report(budget_plan, budget_desired, budget_refused,
                                 budget_planned_count, budget_not_found,
                                 cooled=budget_cooled),
        "switch": _switch_report(switch_plan, switch_desired, switch_refused,
                                 switch_planned_count, switch_not_found,
                                 deferred_over_cap=len(switch_deferred)),
        "tcpa": _tcpa_report(tcpa_plan, tcpa_desired, tcpa_refused,
                             tcpa_planned_count, tcpa_not_found,
                             cooled=tcpa_cooled),
        "negatives": {
            "campaigns": len(negatives_desired),
            "phrases": sum(len(v) for v in negatives_desired.values()),
            # Сколько кандидатов отложено капом такта и сколько отвергнуто
            # формой: молчание рычага обязано быть объяснимым.
            "over_cap": negatives_plan["over_cap"],
            "invalid": len(negatives_plan["invalid"]),
            "cost_covered": negatives_plan["cost_covered"],
            "refused": len(negatives_refused),
            "not_found": len(negatives_not_found),
            "actions_planned": negatives_planned_count,
        },
        "placements": {
            "campaigns": len(placements_desired),
            "sites": sum(len(v) for v in placements_desired.values()),
            "over_cap": placements_plan["over_cap"],
            "invalid": len(placements_plan["invalid"]),
            "cost_covered": placements_plan["cost_covered"],
            "refused": len(placements_refused),
            "not_found": len(placements_not_found),
            "actions_planned": placements_planned_count,
        },
        "desired": len(desired),
        # Э2.2: скольким кампаниям применялся личный план, а не кабинетный.
        "campaign_level": campaign_level_report,
        # Э2.3: отсев по уверенности в знаке эффекта.
        "confidence": confidence_report,
        "unsupported": unsupported,
        # Записи факта без коэффициента: по ним не строится ни одно действие,
        # и это состояние обязано быть видно, а не выглядеть как «в кабинете
        # ничего не настроено».
        "unusable_actual": {"count": unusable_actual,
                            "reason": UNUSABLE_ACTUAL_REASON if unusable_actual else None},
        "planned": len(planned),
        "blocked": len(blocked),
        # Отдельные счётчики, а не общая куча blocked: обратная связь от
        # отката и съеденный закрытыми ключами лимит — разные болезни с
        # разным лечением, и в отчёте их надо различать.
        "blocked_by_cooldown": {
            "count": len(in_cooldown),
            "cooldown_days": COOLDOWN_AFTER_ROLLBACK_DAYS,
            "reason": COOLDOWN_REASON.format(days=COOLDOWN_AFTER_ROLLBACK_DAYS)
                      if in_cooldown else None,
            "segments": sorted({f"{a['object_id']}:{a['direct_type']}:{a['key']}"
                                for a in in_cooldown})[:PREVIEW_SAMPLE_LIMIT],
        },
        "skipped_already_final": {
            "count": len(closed_keys),
            "reason": ALREADY_FINAL_REASON if closed_keys else None,
        },
        # Сегменты, которым движок больше не отправляет действия. Отдельным
        # счётчиком, а не в общей куче blocked: это единственные строки
        # прогона, которые не рассосутся сами и требуют разбора человеком.
        "blocked_by_attempts": {
            "count": len(out_of_attempts),
            "max_attempts": writer_db.MAX_APPLY_ATTEMPTS,
            "reason": (EXHAUSTED_ATTEMPTS_REASON.format(
                attempts="…", max_attempts=writer_db.MAX_APPLY_ATTEMPTS)
                if out_of_attempts else None),
            "segments": sorted({f"{a['object_id']}:{a['direct_type']}:{a['key']}"
                                for a in out_of_attempts})[:PREVIEW_SAMPLE_LIMIT],
        },
        # Что прогон делает с обучением стратегий. Счётчики — по тому, что
        # РЕАЛЬНО уходит в кабинет (prepared), а не по всему, что прошло
        # гейт: между гейтом и отправкой действие ещё режут лимит прогона и
        # риск-бюджет, и «перезапусков обучения» в отчёте обязано быть
        # столько, сколько их случится.
        "learning": {
            # Сколько перезапусков обучения прогон берёт на себя осознанно
            # и сколько — вслепую. Второе число отдельно: «мы не знаем» не
            # должно маскироваться под «мы знаем».
            "resets_applied": sum(1 for a in prepared
                                  if a.get("learning_impact") == "resets"),
            "unknown_applied": sum(1 for a in prepared
                                   if a.get("learning_impact") == "unknown"),
            "blocked_by_cooldown": len(in_learning_cooldown),
            "cooldown_days": LEARNING_COOLDOWN_DAYS,
            # Дата последнего сброса по каждой запертой кампании — из
            # журнала: без неё «заперто кулдауном» нечем проверить.
            "blocked_objects": sorted(
                {f"{a['object_id']}:{a['last_learning_reset_at']}"
                 for a in in_learning_cooldown})[:PREVIEW_SAMPLE_LIMIT],
        },
        # Баланс такта двумя числами, а не одним: на чём стоял гейт и что
        # реально уехало в кабинет. Между ними ещё режут лимит прогона и
        # риск-бюджет, и «такт сбалансирован» обязано считаться по
        # применённому, иначе отчёт хвалит план, а не результат.
        "balance": {
            "gate": {k: v for k, v in balance.items() if k != "freed_by_key"},
            "applied": {k: v for k, v in tact_balance(**balance_inputs(
                prepared, budget_desired, cost_28d_by_campaign,
                cut_cost_by_kind)).items() if k != "freed_by_key"},
            "blocked_without_address": len(without_address),
            "min_assigned_share": MIN_ASSIGNED_SHARE,
        },
        "deferred_by_risk": len(deferred),
        "deferred_by_cap": len(over_cap),
        # Отказы строками, а не только счётчиками: они уезжают в чёрный ящик
        # (sync/agent/blackbox.py), где на истории видно, какое намерение
        # упирается в одну и ту же стену изо дня в день. Один отказ — это
        # бюджет кончился; тридцать одинаковых — неверная модель.
        "rejects": rejects.by_reason(account_rejects),
        # Конфликты отдельной строкой отчёта: в общем счётчике отказов они
        # растворяются среди бюджетных, а читать их надо иначе — это не
        # «не хватило денег», а «план сам себе противоречил».
        "conflicts": conflicts.by_reason(in_conflict),
        "actions_left_in_run": max(ctx["remaining_cap"], 0),
        "no_red_line": {
            "count": len(no_red_line),
            "reason": NO_RED_LINE_REASON if no_red_line else None,
        },
        "remaining_risk_rub": round(remaining, 2),
        # Три числа, а не одно: сколько осталось у недели, сколько у дня и
        # сколько взято. Иначе «бюджет кончился» и «дневная доля выбрана»
        # выглядят одинаково, хотя первое ждёт понедельника, а второе — утра.
        "weekly_risk_left_rub": round(weekly_left, 2),
        "run_risk_left_rub": round(max(ctx["run_risk_remaining"], 0.0), 2),
        "risk_charged_rub": round(sum(a["risk_rub"] for a in prepared), 2),
        # Дельта-модель против прежней: во сколько раз дешевле обошёлся тот
        # же набор действий. Без этой строки переход проверить нечем — цена
        # в журнале не говорит, что она была бы другой при старой арифметике.
        "risk_pricing": _risk_pricing(prepared, caps),
        "absolute_max_cpa": absolute_max_cpa,
        # Состав того, что уходит (или ушло бы) в кабинет. В режиме
        # репетиции это единственное место, где он вообще виден.
        "prepared": actions_preview(prepared),
        "stale_planned": _stale_report(stale, journal_ok),
        "result": {k: v for k, v in report.items() if k != "details"},
        "units_left": client.units_left,
    }


def refusal(sandbox: bool, dry_run: bool) -> Optional[str]:
    """Причина не начинать прогон вовсе, или None.

    Зеркало agent_e1_watchdog.refusal: та же комбинация, тот же довод — до
    появления этой проверки только сторож отказывался стартовать в
    «песочница + запись», а прямое применение такой комбинации не отсекало
    вовсе и писало в боевой журнал строки о песочнице.
    """
    if sandbox and not dry_run:
        return SANDBOX_APPLY_REFUSAL
    return None


def main() -> int:
    sandbox = "--prod" not in sys.argv
    dry_run = "--apply" not in sys.argv
    today = date.today().isoformat()

    # Отказ ДО первого обращения к БД — тем же порядком, что и у сторожа:
    # боевой журнал не должен получить ни строчки от прогона, применение
    # которого физически относится к песочнице.
    refused = refusal(sandbox, dry_run)
    if refused:
        print(json.dumps({"verdict": "REFUSED", "sandbox": sandbox,
                          "dry_run": dry_run, "reason": refused},
                         ensure_ascii=False, indent=2))
        return 2

    # Ограничитель кампаний разбирается ДО обращения к БД, вместе с остальными
    # отказами: неразобранный аргумент означал бы прогон без ограничения — а
    # оператор, набравший его перед первым боевым применением, уверен в
    # обратном. Молчать тут нельзя.
    try:
        campaign_scope = parse_campaign_scope(sys.argv[1:])
    except ValueError as exc:
        print(json.dumps({"verdict": "REFUSED", "sandbox": sandbox,
                          "dry_run": dry_run, "reason": str(exc)},
                         ensure_ascii=False, indent=2))
        return 2

    writer_db.ensure_writer_tables()

    clients = _clients()
    if not clients:
        print(json.dumps({"verdict": "NOTHING_TO_DO", "reason": "нет кабинетов в DIRECT_CLIENTS_JSON"},
                         ensure_ascii=False, indent=2))
        return 0

    # Аренда на прогон. Два одновременных прогона на одном ключе создают в
    # кабинете ДВА объекта: оба читают факт до записи, оба видят, что
    # корректировки нет, оба шлют bidmodifiers.add — и Id первого теряется
    # навсегда, без строки журнала, без красной линии и без возможности отката.
    #
    # Аренды мало ВЗЯТЬ: её срок — час, а прогон по сотням кампаний живёт
    # дольше, и протухшая на ходу аренда пускает следующий прогон штатно.
    # Поэтому аренда продлевается по ходу прогона и перепроверяется перед
    # каждым изменяющим запросом (writer/db.py::RunLease).
    try:
        with writer_db.run_lock("agent_writer") as lease:
            return _run_all(clients, sandbox, dry_run, today, lease,
                            campaign_scope)
    except writer_db.RunLockBusy as exc:
        print(json.dumps({"verdict": "RUN_LOCKED", "reason": str(exc)},
                         ensure_ascii=False, indent=2))
        return 1
    except writer_db.RunLeaseLost as exc:
        # Аренда потеряна на ходу: с этого момента в кабинет мог начать писать
        # второй прогон. Прогон оборван намеренно — это не сбой одного
        # кабинета, а условие, при котором писать нельзя вообще.
        print(json.dumps({"verdict": "RUN_LEASE_LOST", "reason": str(exc)},
                         ensure_ascii=False, indent=2))
        return 1


def _run_all(clients: List[Dict[str, Any]], sandbox: bool, dry_run: bool,
             today: str, lease: Any = None,
             campaign_scope: Optional[CampaignScope] = None) -> int:

    # Режим автономии из панели настроек. Читается ПЕРВЫМ — до гейта данных и
    # до любой загрузки: "off" означает «агент не работает», и прогон, который
    # сначала выгружает полкабинета, а потом вспоминает, что ему запрещено,
    # тратит квоту API на решение, уже принятое человеком.
    #
    # Панель может только УЖЕСТОЧИТЬ то, что задано аргументами запуска:
    # suggest_only превращает боевую запись в репетицию, обратное невозможно.
    # Без этого правила настройка в базе поднимала бы репетицию до записи —
    # то есть строка в таблице решала бы за две галочки в workflow.
    try:
        stored_config = agent_db.load_agent_config()
    except Exception as exc:  # noqa: BLE001
        # Панель недоступна — работаем как раньше: кодовые дефолты, то есть
        # полный режим. Отказ базы не должен молча останавливать агента, но
        # обязан быть виден: иначе «настройки не прочитались» и «настройки
        # такие» выглядят в отчёте одинаково.
        stored_config = {"preset": None, "overrides": {},
                         "unavailable": f"{type(exc).__name__}: {exc}"[:200]}
        print(json.dumps({"verdict": "CONFIG_UNAVAILABLE",
                          "reason": stored_config["unavailable"]},
                         ensure_ascii=False, indent=2))
    active_config = agent_config.resolve(stored_config["preset"],
                                         stored_config["overrides"])
    autonomy = str(active_config["autonomy"])
    if autonomy == "off":
        print(json.dumps({
            "verdict": "AUTONOMY_OFF",
            "reason": "в панели настроек autonomy=off — агент не работает",
        }, ensure_ascii=False, indent=2))
        return 0
    if autonomy == "suggest_only" and not dry_run:
        print(json.dumps({
            "verdict": "AUTONOMY_SUGGEST_ONLY",
            "reason": "в панели настроек autonomy=suggest_only — прогон "
                      "понижен до репетиции: план считается и печатается, "
                      "в кабинет не уходит ничего",
        }, ensure_ascii=False, indent=2))
        dry_run = True

    # Гейт данных — ДО первого обращения к журналу и загрузок планирования:
    # e1 пишет в кабинет, а его оценка риска (дневной расход) и красная линия
    # (базовый CPA) считаны по витрине и источнику, которые до этой проверки
    # никто не проверял. Красный гейт запрещает ЗАПИСЬ, но не репетицию:
    # смотреть на плохие данные можно, писать по ним — нет. Отказ красным
    # кодом — чтобы кроновый прогон стал красным и дошёл письмом.
    gate = data_gate(date.fromisoformat(today))
    if gate["status"] != "GREEN":
        if not dry_run:
            print(json.dumps({"verdict": "DATA_GATE_RED", "data_gate": gate},
                             ensure_ascii=False, indent=2))
            return 1
        # Репетиция продолжается — но красный гейт обязан быть виден и в ней:
        # «ноль находок по плохим данным» и «данные в порядке» — разные
        # состояния. Зелёный гейт не печатается — тем же правилом, что и
        # уборка репетиционных строк ниже (шум без содержания).
        print(json.dumps({
            "verdict": "DATA_GATE",
            "status": gate["status"],
            "latest_fact_date": gate.get("latest_fact_date"),
            "reason": gate.get("reason") or None,
        }, ensure_ascii=False, indent=2))

    holdout_ids = set(agent_db.load_holdout_ids())
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    daily_cost = agent_db.load_daily_cost_by_campaign(cutoff, today)
    # База CPA обязана кончаться на границе зрелости CRM, а не сегодня.
    # Расход Директа приезжает вовремя, лиды — с отставанием 2-4 дня, и день
    # приходит целиком либо не приходит вовсе (agent_db.crm_maturity_date).
    # Считать базу до сегодня значит делить расход тридцати полных дней на
    # лиды двадцати шести-двадцати восьми: база завышена примерно на десятую
    # часть, и всегда в одну сторону. Из неё растёт порог отката (×1.4) —
    # то есть завышение делает сторож мягче ровно там, где он единственная
    # защита боевого кабинета. Наблюдаемый CPA сторож при этом считает по
    # обрезанному окну (agent_e1_watchdog.observation_window), так что
    # сравнивались величины разной полноты.
    crm_through = agent_db.crm_maturity_date()
    baseline_cpa = (
        agent_db.load_baseline_cpa(cutoff, crm_through.isoformat())
        if crm_through else {}
    )
    # База ЦЕНЫ ОПЛАТЫ по тому же окну — для второго чекпоинта
    # (agent_e1_watchdog.money_verdict): через 35 дней вердикт по заявкам
    # сверяется с деньгами, и сравнивать он обязан с базой, снятой тем же
    # способом и за тот же период. Снимается здесь, на применении, а не
    # сторожем задним числом: к моменту сверки окно базы уже недоступно —
    # кампанию с тех пор трогали, в том числе сам агент.
    baseline_cpo = (
        agent_db.load_baseline_cpo(cutoff, crm_through.isoformat())
        if crm_through else {}
    )
    # ОБЪЁМ базы по тому же окну — вторая половина точки отсчёта. Цена без
    # темпа не даёт сравнить прогноз с исходом: сторож посчитает лиды окна
    # наблюдения, а сколько их было до изменения, к тому моменту уже не
    # восстановить — кампанию трогали, в том числе сам агент.
    baseline_volume = (
        agent_db.load_baseline_volume(cutoff, crm_through.isoformat())
        if crm_through else {}
    )
    wk = week_start(today)

    # Абсолютный аварийный порог красной линии считается один раз на весь
    # прогон из уже загруженного baseline_cpa — те же данные, тот же приём
    # медианы, что и для неизвестного дневного расхода в risk.py. Справочник
    # пуст целиком (None) — абсолютный порог из данных не выводится: такие
    # действия ниже явно исключаются из применения, а не тихо получают
    # дефолт-плейсхолдер, никак не связанный с экономикой кабинета.
    absolute_max_cpa = absolute_max_cpa_from_baseline(baseline_cpa)

    # Лимит действий — на ПРОГОН, а не на кабинет: рельса ограничивает объём
    # изменений, которые человек способен проверить и осмысленно откатить, а
    # он не зависит от того, на сколько кабинетов эти изменения разложены.
    # Внутри цикла по четырём кабинетам потолок был вчетверо выше заявленного.
    remaining_cap = MAX_ACTIONS_PER_RUN
    # Сколько риска по каждому объекту уже списано. Заводится из журнала
    # НЕДЕЛИ, а не пустым: потолок объекта (его расход за горизонт замера)
    # общий на все прогоны недели, иначе каждый запуск начинал бы добор
    # заново. Окно и фильтр — те же, что у spent_risk: неделя плюс горизонт
    # замера назад, откатанные строки места не освобождают.
    charged_risk: Dict[str, float] = writer_db.charged_risk_by_object(wk)

    ctx: Dict[str, Any] = {
        "daily_cost": daily_cost,
        # Активный конфиг едет в контекст целиком: параметры разбираются по
        # месту применения, а не растаскиваются здесь по семи ключам — иначе
        # добавление ручки требовало бы правки в двух местах, и забытая
        # вторая правка выглядела бы как применённая настройка.
        "config": active_config,
        "config_source": stored_config,
        # Пороги уверенности по классам действий — один раз на прогон.
        "thresholds": thresholds_from_config(active_config),
        # Независимый расход 28 дней по кампаниям — для рельсы бюджета:
        # без него она проверяет арифметику построителя его же числом.
        # Средний дневной × 28: то же окно, что у Cost28dVat построителя,
        # и та же витрина, но своим путём.
        "cost_28d_by_campaign": {cid: value * 28.0
                                 for cid, value in daily_cost.items()},
        "baseline_cpa": baseline_cpa,
        "baseline_cpo": baseline_cpo,
        "baseline_volume": baseline_volume,
        # Окно, на котором снята база: едет в красную линию, по нему сторож
        # считает сезонную поправку порога.
        "baseline_window": (cutoff, crm_through.isoformat()) if crm_through else None,
        "absolute_max_cpa": absolute_max_cpa,
        "holdout_ids": holdout_ids,
        "charged_risk": charged_risk,
        "week_start": wk,
        "remaining_cap": remaining_cap,
        # Доля недельного риска на этот прогон. Считается ОДИН раз на весь
        # прогон: делить остаток заново на каждом кабинете значило бы выдать
        # четыре дневных доли за один день.
        "run_risk_remaining": paced_allowance(
            weekly_risk_limit(wk, daily_cost, active_config)
            - writer_db.spent_risk(wk),
            today, wk),
        # Ограничитель кампаний — один объект на весь прогон: его остаток
        # общий на все кабинеты, тем же доводом, что и лимит действий.
        # Пустой (ничего не ограничивает) — когда прогон запущен без
        # соответствующих аргументов.
        "campaign_scope": campaign_scope or CampaignScope(),
        "lease": lease,
    }

    # Уборка собственных строк репетиции: у них нет ни финального, ни живого
    # статуса, то есть ни один механизм журнала их не закрывает, — без срока
    # хранения они копятся вечно. За ними ничего не стоит: mutate в репетиции
    # в кабинет не уходил.
    purged = writer_db.purge_dry_run_actions(writer_db.DRY_RUN_RETENTION_DAYS)
    if purged:
        print(json.dumps({
            "verdict": "JOURNAL_MAINTENANCE",
            "dry_run_rows_purged": purged,
            "retention_days": writer_db.DRY_RUN_RETENTION_DAYS,
        }, ensure_ascii=False, indent=2))

    failed_accounts: List[Dict[str, Any]] = []
    # Чёрный ящик прогона: отчёты кабинетов и отказы копятся, чтобы уехать в
    # базу ОДНОЙ строкой прогона. Печать в лог остаётся как была — она для
    # человека здесь и сейчас, а база нужна через две недели, когда вопрос
    # звучит «почему он тогда так решил».
    run_id = blackbox.new_run_id()
    account_reports: List[Dict[str, Any]] = []
    run_rejects: List[Dict[str, Any]] = []
    for client_info in clients:
        login = client_info["login"]
        # Отказ ОДНОГО кабинета не отменяет остальные. Кабинетов четыре, и
        # непойманное исключение на первом означало, что три следующих не
        # обработаны вовсе — причём молча, потому что снаружи это выглядит
        # просто как упавший прогон. Кабинеты независимы: у каждого свой
        # токен-заголовок, свои кампании, свой расчёт; общее у них только
        # лимит действий и риск-бюджет, и оба уже посчитаны так, что
        # пропуск кабинета их не ломает.
        try:
            report = run_account(login, sandbox, dry_run, today, ctx)
        except writer_db.RunLeaseLost:
            # Единственное исключение, которое НЕ локализуется кабинетом:
            # аренду потерял весь прогон, и следующий кабинет писать тем более
            # не вправе.
            raise
        except Exception as exc:
            report = {
                "account": login,
                "verdict": "ACCOUNT_FAILED",
                "reason": f"{type(exc).__name__}: {exc}"[:400],
            }
            failed_accounts.append({"account": login, "error": report["reason"]})
        # Строки отказов снимаются с печатного отчёта: человеку в логе нужен
        # их расклад по причинам (он уже в отчёте), а поштучно они читаются
        # запросом к чёрному ящику.
        run_rejects += report.pop("_rejects", [])
        account_reports.append(report)
        print(json.dumps(report, ensure_ascii=False, indent=2))

    # Слепая доля расхода — та же величина, что печатает такт расчёта, и
    # считает её тот же код (agent/coverage.py::blind_share). Здесь она нужнее:
    # такт расчёта ею оговаривает свои оценки, а такт записи ею оговаривает
    # изменения в кабинете. «Изменили десять кампаний» без неё читается как
    # «взяли кабинет под управление», хотя пятая часть денег живёт вне поля
    # зрения агента и на неё эти изменения не влияют никак.
    #
    # Расход — СУММА за окно из фактов, не ctx["cost_28d_by_campaign"].
    # Последнее это средний дневной × 28, честный множитель цены ошибки и
    # негодный знаменатель доли: кампания, отработавшая 5 дней из 28, входит
    # туда своим темпом, растянутым на месяц. Замер 25.08.2026 показал цену
    # подмены — 9,45 % и 3 слепые кампании в такте записи против 6,18 % и 25
    # в такте расчёта, под одним именем и в один день.
    #
    # Отчётный слой не вправе уронить запись: витрина настроек недоступна —
    # видна причина, прогон продолжается.
    try:
        blind = blind_share(agent_db.load_cost_by_campaign(cutoff, today),
                            agent_db.load_campaign_settings_raw())
    except Exception as exc:  # noqa: BLE001
        blind = {"unavailable": f"{type(exc).__name__}: {exc}"[:200]}
    print(json.dumps({
        "verdict": "BLIND_SPEND",
        "window": [cutoff, today],
        "blind_spend": blind,
    }, ensure_ascii=False, indent=2))

    saved = blackbox.save_run(
        run_id, stage="e1", mode=blackbox.run_mode(sandbox, dry_run),
        report={"verdict": run_verdict(account_reports),
                "accounts": account_reports, "blind_spend": blind,
                "window": [cutoff, today], "failed_accounts": failed_accounts},
        rejects=run_rejects)
    # Итог записи печатается всегда, включая ошибку: молчащий чёрный ящик
    # хуже отсутствующего — он создаёт уверенность, что история пишется.
    print(json.dumps({"verdict": "BLACKBOX", **saved,
                      "rejects_by_reason": rejects.by_reason(run_rejects)},
                     ensure_ascii=False, indent=2))

    if failed_accounts:
        # Итоговая строка отдельно от кабинетных отчётов: иначе отказ первого
        # кабинета теряется в потоке успешных отчётов остальных трёх.
        print(json.dumps({
            "verdict": "PARTIAL_FAILURE",
            "failed_accounts": failed_accounts,
            "accounts_total": len(clients),
        }, ensure_ascii=False, indent=2))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
