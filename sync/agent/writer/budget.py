# -*- coding: utf-8 -*-
"""
sync/agent/writer/budget.py — рычаг бюджетов (Э3.3): целевые бюджеты Э3.2 →
действия campaigns.update.

Что меняется. Единственный бюджетный регулятор автостратегии Директа —
WeeklySpendLimit внутри блока BiddingStrategy кампании; у ручных стратегий
(HIGHEST_POSITION) — DailyBudget самой кампании. Замер по кабинетам
(probe_budget_lever, прогон 32653611789): 62 из 70 целевых кампаний несут
собственный WeeklySpendLimit ровно в одном канале (Search либо Network),
3 — ручные с DailyBudget, 4 сидят на пакетных стратегиях (лимит общий на
несколько кампаний — менять его от имени одной кампании нельзя).

Асимметрия рычага — свойство механики, а не упрощение кода:

  * СНИЖЕНИЕ бюджета лимитом достижимо всегда: лимит, поставленный ниже
    текущего расхода, связывает стратегию по построению.
  * ПОВЫШЕНИЕ лимитом достижимо только там, где текущий лимит связывает
    расход (кампания упирается в него). По замеру таких 9 из 62: у остальных
    лимит стоит в разы выше расхода, объём держит цель CPA/ставка, и
    повышение лимита не изменит ничего. Такие «вверх» уходят в отдельный
    счётчик not_applicable с причиной — это правда о рычаге, которую обязан
    видеть человек, а не тихий пропуск. Их рычаг — ставки/цели (Э4).

Единицы. Целевой бюджет Э3.2 (budget_target.value) — расход за 28 дней
С НДС, как все факты EDU (Reports API запрашивается с IncludeVAT=YES,
sync/agent/segments.py:265). Лимиты кабинета Директ хранит БЕЗ НДС в
микрорублях. Конверсия — здесь и только здесь: value / 4 недель / VAT → в
микрорубли с округлением до рубля.

previous_state несёт ВЕСЬ прочитанный блок BiddingStrategy (или DailyBudget),
а не только старый лимит: откат обязан вернуть кампанию ровно туда, откуда
её вывели, — тем же правилом, что у расписания (diff.diff_schedule).
"""

import copy
import hashlib
from typing import Any, Dict, List, Optional, Tuple

from sync.agent.confidence import assess
from sync.agent.writer import exposure

# Факты расхода с НДС, лимиты кабинета без. Ставка НДС РФ; менялась в 2019 —
# если изменится снова, конверсия обязана поехать за ней.
VAT = 1.2

# Недель в окне целевого бюджета: budget_target считан на 28 зрелых днях.
WEEKS_IN_WINDOW = 4.0

MICROS = 1_000_000

# Сдвиг меньше этого не стоит запроса и слота риск-бюджета: лимит дрейфует
# на проценты от пересчёта к пересчёту, и без порога один и тот же лимит
# переписывался бы каждую неделю на копейки.
MIN_SHIFT = 0.10

# «Уже стоит»: расхождение факта с планом меньше этого — не действие.
ALREADY_SET_TOLERANCE = 0.05

# Кап шага НА ЗАПИСИ: бюджет кампании меняется не больше чем на ±20 % за
# один такт. Портфель (Э3.2) зажат капом ×1.5/×0.5, но это кап РАСЧЁТА;
# без капа записи одна строка компьютеда двигала бы лимит сразу на треть,
# и автостратегии жили бы в вечном переобучении. База капа — РАСХОД
# кампании, а не текущий лимит: лимит, висящий в разы выше расхода, —
# декорация, и ±20 % от него не ограничивают ничего; менять «бюджет»
# кампании значит менять её расход.
MAX_WRITE_STEP = 0.20

# Вторая половина того же правила: кампания, чей бюджет трогали за
# последние 14 дней (по ЖУРНАЛУ применённых действий, а не по расчёту —
# расчёт передумывает каждый такт), в этот такт не трогается вовсе.
BUDGET_COOLDOWN_DAYS = 14

BUDGET_COOLDOWN_REASON = (
    "бюджет кампании уже меняли за последние {days} дн. — кулдаун по "
    "журналу применённых действий: автостратегии не переобучаются каждый "
    "такт, шаг за {days} дн. ограничен ±{step:.0%}"
)

# Лимит связывает расход, если расход добирает до него хотя бы столько.
# Порог из replикации ручного правила «кампания упирается в бюджет»: недельный
# расход колеблется, и требовать 100 % — значит не признать связывающим даже
# лимит, который стратегия выбирает 19 недель из 20.
BINDING_SHARE = 0.9

BUDGET_KIND = "budget.set"          # автостратегия: WeeklySpendLimit
BUDGET_DAILY_KIND = "budget.set_daily"  # ручная стратегия: DailyBudget кампании

# Причины отказов — все явные: «рычага нет» и «данных нет» обязаны
# различаться в отчёте прогона.
NOT_APPLICABLE_UP_REASON = (
    "повышение бюджета лимитом недостижимо: текущий лимит не связывает расход "
    "(кампания тратит меньше {share:.0%} лимита), объём держит цель стратегии — "
    "рычаг этого сдвига появится вместе со ставками (Э4)"
)
PACKAGE_REASON = (
    "кампания на пакетной стратегии {strategy_id}: лимит общий на несколько "
    "кампаний, менять его от имени одной кампании нельзя"
)
NO_LIMIT_REASON = (
    "в стратегии кампании не задан WeeklySpendLimit и нет DailyBudget: "
    "установка нового ограничения — не перенос существующего, прошлое "
    "состояние «без лимита» откатом не восстанавливается (снятие лимита "
    "движку запрещено)"
)
TWO_CHANNEL_REASON = (
    "WeeklySpendLimit задан в обоих каналах (Search и Network): распределение "
    "целевого бюджета между ними неоднозначно"
)
NOT_TEXT_REASON = (
    "тип кампании не TEXT_CAMPAIGN: форма блока стратегии для записи не "
    "проверена, применение не планируется"
)
LOW_CONFIDENCE_REASON = (
    "уверенность в экономическом преимуществе сдвига (value против "
    "λ·marginal) ниже порога класса budget_shift"
)

# Пометка разведочного сдвига в отчёте. Она заменяет собой отказ по
# уверенности, а не прячет его: у разведки уверенности нет ПО ПОСТРОЕНИЮ —
# карман делится пропорционально незнанию (portfolio.exploration_bonus), то
# есть уходит ровно туда, где оценка хуже всего. Требовать от такой строки
# p_sign ≥ 0.90 значит требовать знания там, где деньги тратятся ради его
# получения; при этом разведочные применения обязаны быть отличимы от
# обычных — иначе «агент подлил вслепую» и «агент подлил уверенно» читаются
# в журнале одинаково.
EXPLORATION_WAIVED_REASON = (
    "разведка: уверенность не требуется — карман неопределённости идёт туда, "
    "где оценка хуже всего, и порог класса budget_shift к нему не применяется"
)


def _idempotency_key(campaign_id: str, kind: str, micros: int) -> str:
    raw = f"budget:{campaign_id}:{kind}:{micros}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def desired_weekly_micros(target_28d_with_vat: float) -> int:
    """Целевой расход 28 дней с НДС → недельный лимит кабинета в микрорублях.

    Округление до целого РУБЛЯ, а не микрорубля: лимит — управляющая ручка,
    и хвост в микрорублях делал бы ключ идемпотентности чувствительным к
    шуму плавающей точки.
    """
    rub = float(target_28d_with_vat) / WEEKS_IN_WINDOW / VAT
    return int(round(rub)) * MICROS


def clamp_write_step(target_micros: int, base_rub: float,
                     max_step: float = MAX_WRITE_STEP) -> int:
    """Целевой лимит, дожатый до ±max_step от базового РАСХОДА (₽ без НДС).

    Округление до целого рубля — тем же правилом, что desired_weekly_micros:
    лимит — управляющая ручка, ключ идемпотентности не должен дрожать от
    хвостов плавающей точки.
    """
    lo = int(round(float(base_rub) * (1.0 - max_step))) * MICROS
    hi = int(round(float(base_rub) * (1.0 + max_step))) * MICROS
    return min(max(int(target_micros), lo), hi)


def apply_cooldown(desired, touched):
    """Сдвиги, очищенные от кампаний под кулдауном, и список отведённых.

    touched — object_id кампаний с применённым бюджетным действием за окно
    кулдауна (writer_db.recent_action_objects): кулдаун считается по факту
    журнала, а не по расчёту.
    """
    touched = {str(t) for t in (touched or ())}
    reason = BUDGET_COOLDOWN_REASON.format(days=BUDGET_COOLDOWN_DAYS,
                                           step=MAX_WRITE_STEP)
    cooled = [{"campaign_id": str(cid), "reason": reason}
              for cid in sorted(set(desired) & touched)]
    kept = {cid: m for cid, m in desired.items() if str(cid) not in touched}
    return kept, cooled


def _expected_leads_delta(rows: List[Dict[str, Any]],
                          key: str = "expected_leads_delta") -> Optional[float]:
    """Ожидание солвера: прирост лидов от этого сдвига.

    Число приходит готовой строкой budget_target/expected_leads_delta
    (portfolio.computed_rows). Формула кривой живёт там, где строится сама
    кривая, и повторять её здесь нельзя: две копии модели разъезжаются при
    первой же правке одной из них, а расхождение прогноза с исходом — ровно
    та величина, которую меряет петля обучения.

    None, когда строки нет: ноль означал бы прогноз «эффекта не будет», и
    петля зачла бы его как сбывшийся.
    """
    row = next((r for r in rows
                if str(r.get("setting_kind")) == "budget_target"
                and str(r.get("setting_key")) == key), None)
    if row is None or row.get("value") is None:
        return None
    return round(float(row["value"]), 2)


def _expectation_payload(move: Dict[str, Any]) -> Dict[str, float]:
    """Кусок payload с ожиданием солвера — или пустой, если ожидания нет.

    Ожидание едет вместе с действием: через 7–14 дней сторож положит рядом
    факт (agent_e1_watchdog.observed_leads_delta), и разница этих двух чисел —
    единственный способ узнать, что модель врёт систематически.

    Чисел два, и они не взаимозаменяемы. expected_leads_delta — СЫРОЕ
    ожидание модели, и меряется поправка именно против него. Заменить его
    калиброванным значило бы дать петле мерить собственную поправку: любая
    вошла бы в норму, а смещение сошлось бы к единице, ничего не исправив.
    expected_leads_delta_calibrated — то же с поправкой, для чтения и для
    проверки «сжимает ли такт объём».
    """
    expected = move.get("expected_leads_delta")
    if expected is None:
        return {}
    out = {"expected_leads_delta": round(float(expected), 2)}
    calibrated = move.get("expected_leads_delta_calibrated")
    if calibrated is not None:
        out["expected_leads_delta_calibrated"] = round(float(calibrated), 2)
    return out


def _exploration_payload(move: Dict[str, Any]) -> Dict[str, Any]:
    """Кусок payload с признаком разведочной ставки — или пустой.

    Признак объявлен в plan_budget_moves («едет ДАЛЬШЕ, в действие и в
    журнал»), но до 26.08.2026 никуда не ехал: diff_budget собирает payload из
    своих полей и про move["exploration"] не знал. Из-за этого разведочный
    сдвиг был неотличим от обычного везде, где начинается его собственная
    жизнь, — в журнале действий, в отчёте прогона и в реестре гипотез
    (sync/agent/experiments.is_bet читает именно этот признак). То есть
    единственное применение агента, у которого снят гейт уверенности, нельзя
    было ни пересчитать, ни судить отдельно.
    """
    if not move.get("exploration"):
        return {}
    return {
        "exploration": True,
        "exploration_rub": move.get("exploration_rub"),
        "confidence_waived": move.get("confidence_waived"),
    }


def plan_budget_moves(
    computed_by_campaign: Dict[str, List[Dict[str, Any]]],
    thresholds: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """Строки budget_target → желаемые сдвиги по кампаниям.

    {"desired": {cid: {"target_28d", "cost_28d", "ratio", "p_sign"}},
     "low_confidence": [...], "confidence_unknown": N, "small_shift": N,
     "exploration": [...]}.

    Пороги значимости из plan_bid_modifiers здесь не действуют: support_n у
    budget_target — лиды окна, и он уже отработал в Э3.2 (ошибка решения
    rel_error выведена из него); фильтровать второй раз по числу лидов —
    судить решение чужой линейкой. Гейт уверенности — свой класс
    budget_shift (0.90 по умолчанию, порог берётся из панели настроек:
    бюджет обратим, но затрагивает всю кампанию).

    ЕДИНСТВЕННОЕ исключение из гейта уверенности — разведочный сдвиг (строка
    budget_target/exploration_rub): см. EXPLORATION_WAIVED_REASON. Снимается
    ровно один гейт и только он. Кап шага, кулдаун 14 дней, риск-бюджет,
    красная линия, заповедник, лимит действий за прогон и рельсы движка
    действуют на разведку в полном объёме: разведка — это право не знать, а
    не право не отвечать.
    """
    desired: Dict[str, Dict[str, Any]] = {}
    low_confidence: List[Dict[str, Any]] = []
    exploration: List[Dict[str, Any]] = []
    confidence_unknown = 0
    small_shift = 0

    for cid, rows in computed_by_campaign.items():
        row = next((r for r in rows
                    if str(r.get("setting_kind")) == "budget_target"
                    and str(r.get("setting_key")) == "target_28d"), None)
        if row is None:
            continue
        target = float(row.get("value") or 0.0)
        current = float(row.get("raw_value") or 0.0)
        if target <= 0 or current <= 0:
            continue
        ratio = target / current
        if abs(ratio - 1.0) < MIN_SHIFT:
            small_shift += 1
            continue
        # Разведочная надбавка, назначенная солвером именно этой кампании
        # (portfolio._apply_exploration). Читается ДО гейта уверенности:
        # именно от неё зависит, применим ли гейт вообще.
        explore_row = next((r for r in rows
                            if str(r.get("setting_kind")) == "budget_target"
                            and str(r.get("setting_key")) == "exploration_rub"), None)
        exploration_rub = (float(explore_row.get("value") or 0.0)
                           if explore_row is not None else 0.0)
        is_exploration = exploration_rub > 0
        # Уверенность — по экономическому отношению value/(λ·marginal), а не
        # по размеру шага: шаг раздут показателем кривой 1/(1−β), и
        # assess(target/current) выдавал «уверенно» на слепой истории (аудит
        # 2026-08-23, C2). Строка старого формата без отношения — уверенность
        # неизвестна: свежий прогон Э0 допишет её, ждать дешевле ложного
        # сдвига.
        roi_row = next((r for r in rows
                        if str(r.get("setting_kind")) == "budget_target"
                        and str(r.get("setting_key")) == "roi_vs_lambda"), None)
        roi_value = (float(roi_row["value"])
                     if roi_row is not None and float(roi_row.get("value") or 0.0) > 0
                     else None)
        if roi_value is None:
            # Экономического отношения нет — судить нечем. Для обычного сдвига
            # это отказ «уверенность неизвестна»; для разведочного нет и
            # предмета отказа: его основание не в отношении к λ, а в незнании.
            if not is_exploration:
                confidence_unknown += 1
                continue
            verdict = {"p_sign": None, "min_p_sign": None, "confident": None}
        else:
            verdict = assess(roi_value, roi_row.get("rel_error"),
                             "budget_shift", thresholds)
        if is_exploration:
            exploration.append({
                "campaign_id": str(cid), "ratio": round(ratio, 3),
                "exploration_rub": round(exploration_rub, 2),
                "p_sign": verdict["p_sign"], "min_p_sign": verdict["min_p_sign"],
                "reason": EXPLORATION_WAIVED_REASON,
            })
        elif verdict["confident"] is None:
            confidence_unknown += 1
        elif not verdict["confident"]:
            low_confidence.append({
                "campaign_id": str(cid), "ratio": round(ratio, 3),
                "roi_vs_lambda": round(roi_value, 4),
                "p_sign": verdict["p_sign"], "min_p_sign": verdict["min_p_sign"],
                "reason": LOW_CONFIDENCE_REASON,
            })
            continue
        desired[str(cid)] = {
            "target_28d": target,
            "cost_28d": current,
            "ratio": round(ratio, 4),
            "roi_vs_lambda": round(roi_value, 4) if roi_value is not None else None,
            "p_sign": verdict["p_sign"],
        }
        if is_exploration:
            # Признак едет ДАЛЬШЕ, в действие и в журнал: без него отчёт
            # прогона не смог бы сказать, сколько разведочных сдвигов
            # применено, а разбор через две недели — отличить разведку от
            # обычной доливки.
            desired[str(cid)]["exploration"] = True
            desired[str(cid)]["exploration_rub"] = round(exploration_rub, 2)
            desired[str(cid)]["confidence_waived"] = EXPLORATION_WAIVED_REASON
        expected = _expected_leads_delta(rows)
        if expected is not None:
            desired[str(cid)]["expected_leads_delta"] = expected
        calibrated = _expected_leads_delta(rows, "expected_leads_delta_calibrated")
        if calibrated is not None:
            desired[str(cid)]["expected_leads_delta_calibrated"] = calibrated
        # Кап записи, назначенный солвером именно этой кампании
        # (portfolio.write_step_for): строки нет — действует общий
        # MAX_WRITE_STEP. Политику капа держит солвер, рельса движка
        # (guardrails.BUDGET_RATIO_MAX) ловит его поверх независимо.
        step_row = next((r for r in rows
                         if str(r.get("setting_kind")) == "budget_target"
                         and str(r.get("setting_key")) == "write_step"), None)
        if step_row is not None and float(step_row.get("value") or 0.0) > 0:
            desired[str(cid)]["write_step"] = float(step_row["value"])

    return {"desired": desired, "low_confidence": low_confidence,
            "confidence_unknown": confidence_unknown,
            "small_shift": small_shift,
            # Разведочные сдвиги — отдельным списком, а не растворёнными в
            # desired: применение вслепую обязано быть пересчитываемым. Без
            # этого списка «агент применил 12 сдвигов» не отличалось бы от
            # «агент применил 9 уверенных и 3 разведочных», а через две недели
            # именно это различие и решает, чему приписать результат.
            "exploration": exploration}


def _limit_holder(block: Any) -> Optional[Dict[str, Any]]:
    """Вложенный словарь блока канала, несущий WeeklySpendLimit.

    Поиск по СОДЕРЖИМОМУ, а не по справочнику имён подблоков
    (AverageCpa/WbMaximumConversionRate/...): имя выводится из типа стратегии,
    и каждый новый тип требовал бы правки справочника, а забытая правка
    означала бы молчаливый пропуск. Ключ WeeklySpendLimit один на все типы.
    """
    if not isinstance(block, dict):
        return None
    for value in block.values():
        if isinstance(value, dict) and "WeeklySpendLimit" in value:
            return value
    return None


def read_weekly_limit(strategy: Dict[str, Any]) -> Tuple[Optional[str], Optional[int], str]:
    """Блок BiddingStrategy из campaigns.get → (канал, лимит в micros, причина отказа).

    Канал ровно один: лимит в обоих — отказ (распределять цель между каналами
    рычаг не умеет), ни в одном — отказ (см. NO_LIMIT_REASON).
    """
    holders = {}
    for channel in ("Search", "Network"):
        holder = _limit_holder(strategy.get(channel))
        if holder is not None and holder.get("WeeklySpendLimit") is not None:
            holders[channel] = holder
    if len(holders) > 1:
        return None, None, TWO_CHANNEL_REASON
    if not holders:
        return None, None, NO_LIMIT_REASON
    channel, holder = next(iter(holders.items()))
    try:
        return channel, int(holder["WeeklySpendLimit"]), ""
    except (TypeError, ValueError):
        return None, None, f"WeeklySpendLimit нечитаем: {holder['WeeklySpendLimit']!r}"


def strategy_with_limit(strategy: Dict[str, Any], channel: str, micros: int) -> Dict[str, Any]:
    """Копия блока BiddingStrategy с новым лимитом в названном канале.

    Блок уходит в update ЦЕЛИКОМ, как прочитан: BiddingStrategy в API
    заменяется структурой, а не сливается по полям, и пересборка из
    справочника потеряла бы соседние настройки (цель, BidCeiling,
    ExplorationBudget), которые ставил человек.
    """
    out = copy.deepcopy(strategy)
    holder = _limit_holder(out.get(channel))
    if holder is None:
        raise ValueError(f"в канале {channel} нет носителя WeeklySpendLimit")
    holder["WeeklySpendLimit"] = int(micros)
    return out


def diff_budget(
    desired: Dict[str, Dict[str, Any]],
    actual_by_campaign: Dict[str, Dict[str, Any]],
    weekly_spend_no_vat: Dict[str, float],
    binding_share: float = BINDING_SHARE,
    max_write_step: float = MAX_WRITE_STEP,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Желаемые сдвиги × прочитанное состояние кабинета → (действия, отказы).

    actual_by_campaign — результат fetch_budget_state: по кампании словарь
    {"strategy": BiddingStrategy | None, "daily_budget": DailyBudget | None,
     "package_id": ..., "campaign_type": ...}. Кампании без записи в нём не
    порождают ни действия, ни отказа — их не оказалось в кабинете (чужой
    логин, архив), и это состояние видно по счётчику not_found вызывающего.

    weekly_spend_no_vat — недельный расход кампании БЕЗ НДС (из cost_28d
    целевой строки): по нему решается, связывает ли текущий лимит расход.
    """
    actions: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []

    def _refuse(cid: str, reason: str) -> None:
        refused.append({"campaign_id": cid, "reason": reason})

    for cid in sorted(desired):
        move = desired[cid]
        state = actual_by_campaign.get(str(cid))
        if state is None:
            continue

        if state.get("package_id"):
            _refuse(cid, PACKAGE_REASON.format(strategy_id=state["package_id"]))
            continue
        if state.get("campaign_type") not in (None, "TEXT_CAMPAIGN"):
            _refuse(cid, NOT_TEXT_REASON)
            continue

        spend = float(weekly_spend_no_vat.get(str(cid)) or 0.0)
        target_micros = desired_weekly_micros(move["target_28d"])
        # Кап записи — не глобальная константа, а решение по кампании:
        # ±MAX_WRITE_STEP по умолчанию (столько можно, не сбивая обучение
        # стратегии), больше — только там, где солвер доказал недобор
        # трафика и запас окупаемости. Оставить здесь константу значило бы
        # тихо обнулять адресный шаг ×2 на последнем метре.
        # Дефолт капа — из панели настроек (max_write_step), а не из
        # константы модуля: ручка «потолок шага за одну запись» до 26.08.2026
        # никуда не передавалась и не меняла ничего. Адресный шаг солвера
        # (write_step) по-прежнему главнее — он назначен под доказанный
        # недобор конкретной кампании, и общая настройка его не срезает.
        step = float(move.get("write_step") or max_write_step)
        if spend > 0:
            # Кап применяется ДО проверки «уже стоит»: судить, стоит ли
            # лимит, надо о том значении, которое реально поедет в кабинет.
            target_micros = clamp_write_step(target_micros, spend, max_step=step)
        strategy = state.get("strategy")
        channel, current_micros, reason = (
            read_weekly_limit(strategy) if isinstance(strategy, dict)
            else (None, None, NO_LIMIT_REASON))
        if channel is None and reason == TWO_CHANNEL_REASON:
            # Двухканальный лимит — отказ сам по себе: DailyBudget у такой
            # кампании ниже не рассматривается, это была бы правка не того
            # регулятора.
            _refuse(cid, reason)
            continue

        if channel is not None and current_micros:
            up = move["ratio"] > 1.0
            if up and spend * MICROS < binding_share * current_micros:
                _refuse(cid, NOT_APPLICABLE_UP_REASON.format(share=binding_share))
                continue
            if abs(target_micros - current_micros) < ALREADY_SET_TOLERANCE * current_micros:
                continue
            actions.append({
                "action_kind": BUDGET_KIND,
                "object_level": "campaign",
                "object_id": str(cid),
                # Под ударом — разница между новым лимитом и фактическим
                # расходом, а не весь расход кампании: прежние деньги уже
                # тратились и не становятся сомнительными от сдвига потолка.
                "exposure": exposure.budget_exposure(
                    target_micros / MICROS / 7.0, spend / 7.0),
                # Тип и ключ адресуют кулдаун и счётчик попыток — как у
                # расписания: без них бюджет жил бы вне этих механизмов.
                "direct_type": "WEEKLY_SPEND_LIMIT",
                "key": channel.lower(),
                "payload": {
                    "CampaignId": int(cid),
                    "BiddingStrategy": strategy_with_limit(
                        strategy, channel, target_micros),
                    "WeeklySpendLimit": target_micros,
                    # Для рельсы _check_budget: расход того же окна, из
                    # которого посчитана цель. В API не уходит (to_api_call
                    # берёт из payload только BiddingStrategy).
                    "Cost28dVat": move["cost_28d"],
                    **_expectation_payload(move),
                    **_exploration_payload(move),
                },
                "previous_state": {
                    "BiddingStrategy": strategy,
                    "WeeklySpendLimit": current_micros,
                },
                "idempotency_key": _idempotency_key(cid, "weekly", target_micros),
            })
            continue

        daily = state.get("daily_budget") or {}
        daily_micros = daily.get("Amount")
        if daily_micros:
            target_daily_micros = int(round(target_micros / 7 / MICROS)) * MICROS
            if spend > 0:
                target_daily_micros = clamp_write_step(
                    target_daily_micros, spend / 7.0, max_step=step)
            up = move["ratio"] > 1.0
            if up and spend * MICROS / 7 < binding_share * int(daily_micros):
                _refuse(cid, NOT_APPLICABLE_UP_REASON.format(share=binding_share))
                continue
            if abs(target_daily_micros - int(daily_micros)) < ALREADY_SET_TOLERANCE * int(daily_micros):
                continue
            actions.append({
                "action_kind": BUDGET_DAILY_KIND,
                "object_level": "campaign",
                "object_id": str(cid),
                "exposure": exposure.budget_exposure(
                    target_daily_micros / MICROS, spend / 7.0),
                "direct_type": "DAILY_BUDGET",
                "key": "daily",
                "payload": {
                    "CampaignId": int(cid),
                    "DailyBudget": {**daily, "Amount": target_daily_micros},
                    "Cost28dVat": move["cost_28d"],
                    **_expectation_payload(move),
                    **_exploration_payload(move),
                },
                "previous_state": {"DailyBudget": daily},
                "idempotency_key": _idempotency_key(cid, "daily", target_daily_micros),
            })
            continue

        _refuse(cid, reason or NO_LIMIT_REASON)

    return actions, refused


def fetch_budget_state(client, campaign_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Текущее бюджетное состояние кампаний — свежим чтением, не из витрины.

    Между прогонами кабинет правят руками; previous_state обязан описывать
    то, что стояло В МОМЕНТ применения, — тем же правилом, что
    _actual_modifiers в agent_e1.
    """
    out: Dict[str, Dict[str, Any]] = {}
    ids = [int(c) for c in campaign_ids]
    if not ids:
        return out
    page = 1000
    for start in range(0, len(ids), page):
        chunk = ids[start:start + page]
        result = client.get("campaigns", {
            "SelectionCriteria": {"Ids": chunk},
            "FieldNames": ["Id", "Type", "DailyBudget"],
            "TextCampaignFieldNames": ["BiddingStrategy", "PackageBiddingStrategy"],
        })
        for item in result.get("Campaigns") or []:
            tc = item.get("TextCampaign") or {}
            pkg = tc.get("PackageBiddingStrategy") or {}
            out[str(item.get("Id"))] = {
                "campaign_type": item.get("Type"),
                "strategy": tc.get("BiddingStrategy"),
                "daily_budget": item.get("DailyBudget"),
                "package_id": pkg.get("StrategyId"),
            }
    return out
