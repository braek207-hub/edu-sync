# -*- coding: utf-8 -*-
"""
sync/agent/writer/negatives.py — Э3.6 (запись): минус-фразы кампании.

Самый дешёвый по риску рычаг из денежных: он не разгоняет ставки и не двигает
бюджет, а только отсекает трафик, который уже доказал, что не окупается
(agent/objects.minus_word_candidates). И самый неприятный при ошибке: цена
промаха здесь не «ставка выше», а «показов по фразе нет вовсе» — причём
навсегда, пока человек не вычистит список руками.

Отсюда устройство:

  * ФРАЗ ЗА ТАКТ НЕМНОГО. Список отсекает трафик мгновенно, а обратная связь
    (упал ли поток лидов) приходит через дни. Добавляя по горсти самых дорогих
    фраз, мы всегда можем связать провал с конкретным тактом.
  * СПИСОК ЗАМЕНЯЕТСЯ ЦЕЛИКОМ. В API NegativeKeywords — массив, а не набор
    операций «добавить»: действие обязано нести ОБЪЕДИНЕНИЕ прежнего списка и
    новых фраз, иначе правка стёрла бы то, что настроил человек.
  * ЛИМИТЫ КАБИНЕТА СЧИТАЮТСЯ ЗДЕСЬ. Директ ограничивает минус-фразу семью
    словами по 35 символов, а суммарную длину списка — 20 000 символов.
    Превышение — не ошибка одной фразы, а отказ всего запроса, поэтому
    бюджет символов проверяется до отправки.
  * ОПЕРАТОРЫ ЗАПРЕЩЕНЫ. Кавычки, «!», «+», квадратные скобки меняют смысл
    фразы, и в автоматическом рычаге им не место: агент отсекает то, что
    видел в отчёте, а не то, что «похоже».
"""

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple

from sync.agent.objects import CANDIDATE_WINDOW_DAYS
from sync.agent.writer import exposure, expectation

NEGATIVE_KIND = "negative.add"

# Ограничения Директа (ref-v5/campaigns/update): не больше 7 слов во фразе,
# 35 символов в слове, 20 000 символов суммарно на кампанию.
MAX_WORDS_PER_PHRASE = 7
MAX_WORD_CHARS = 35
MAX_TOTAL_CHARS = 20_000

# Сколько фраз добавляется за один такт. Меньше, чем хочется: обратная связь
# по отсечённому трафику приходит через дни, и такт обязан оставаться
# различимым в наблюдении.
MAX_PHRASES_PER_TICK = 10

# Операторы языка запросов Директа. Во фразе, пришедшей из отчёта поисковых
# запросов, их быть не может — значит это чужая правка или битые данные.
OPERATOR_CHARS = set('"!+[]<>|')

_SPACES = re.compile(r"\s+")

DISPLAY_CAMPAIGN_REASON = (
    "минус-фразы недоступны для этого типа кампании (не текстовая)"
)


def normalize_phrase(phrase: str) -> str:
    """Фраза в каноническом виде: нижний регистр, одиночные пробелы."""
    return _SPACES.sub(" ", str(phrase or "").strip()).lower()


def phrase_is_valid(phrase: str) -> Tuple[bool, str]:
    """Проходит ли фраза ограничения Директа и правила рычага."""
    normalized = normalize_phrase(phrase)
    if not normalized:
        return False, "пустая фраза"
    if OPERATOR_CHARS & set(normalized):
        return False, ("во фразе есть операторы языка запросов — "
                       "автоматический рычаг такие не ставит")
    words = normalized.split(" ")
    if len(words) > MAX_WORDS_PER_PHRASE:
        return False, (f"во фразе {len(words)} слов при пределе "
                       f"{MAX_WORDS_PER_PHRASE}")
    for word in words:
        if len(word) > MAX_WORD_CHARS:
            return False, (f"слово длиннее {MAX_WORD_CHARS} символов: "
                           f"{word[:40]}")
    return True, ""


def plan_negatives(
    candidates: List[Dict[str, Any]],
    max_per_tick: int = MAX_PHRASES_PER_TICK,
) -> Dict[str, Any]:
    """Кандидаты расчёта → фразы к добавлению по кампаниям.

    Кандидат несёт список кампаний, в которых фраза жгла деньги: минусуется
    она именно там, а не по всему кабинету — в соседней кампании та же фраза
    может работать.

    Отбор — по расходу: за такт добавляются самые дорогие фразы, остальные
    ждут следующего. Счётчик over_cap показывает, сколько отложено, — иначе
    «добавили десять» неотличимо от «больше и не нашлось».
    """
    valid: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []
    for candidate in candidates:
        phrase = normalize_phrase(candidate.get("query"))
        ok, reason = phrase_is_valid(phrase)
        if not ok:
            invalid.append({"query": candidate.get("query"), "reason": reason})
            continue
        valid.append({**candidate, "phrase": phrase})

    valid.sort(key=lambda c: -float(c.get("cost") or 0.0))
    taken = valid[:max_per_tick]
    over_cap = len(valid) - len(taken)

    desired: Dict[str, List[str]] = {}
    # Сколько денег каждая кампания перестанет тратить, если фразы уедут в
    # минус: вход цены риска. Считается по той же выборке taken, что и сами
    # фразы, — иначе рычаг платил бы за трафик, который не отсекает.
    cut_cost: Dict[str, float] = {}
    # Сколько конверсий этот трафик всё-таки приносил: вход ОЖИДАНИЯ
    # (writer/expectation.py) и ОСНОВАНИЯ класса 0 (writer/tier.py). У
    # кандидата zero_conversions их нет по построению, у кандидата
    # cpa_above_limit они есть — и отсечение их теряет. Обещать «лидов не
    # потеряем» там, где режется конверсионный трафик, значило бы заранее
    # назначить наблюдению провал.
    cut_conversions: Dict[str, float] = {}
    # Кампании, у которых хоть одна вырезаемая фраза пришла строкой старого
    # формата. Их конверсии не измерены, и подставить сюда ноль нельзя:
    # правило трёх дало бы такой кампании класс 0 по данным, которых нет.
    unknown: set = set()
    for candidate in taken:
        split = candidate.get("cost_by_campaign") or {}
        conversions_split = candidate.get("conversions_by_campaign") or {}
        measured = candidate.get("conversions") is not None
        # Запасной путь для кандидата, у которого конверсии есть общим числом,
        # но не разложены по кампаниям: раскладываем ПО ДЕНЬГАМ. Делить
        # поровну значило бы приписать одинаковую потерю кампании с расходом
        # 100 ₽ и кампании с расходом 100 000 ₽.
        cost_total = sum(float(v) for v in split.values()) or float(
            candidate.get("cost") or 0.0)
        for campaign_id in candidate.get("campaigns") or []:
            if not campaign_id:
                continue
            phrases = desired.setdefault(str(campaign_id), [])
            if candidate["phrase"] not in phrases:
                phrases.append(candidate["phrase"])
            campaign_cost = float(split.get(str(campaign_id), 0.0))
            cut_cost[str(campaign_id)] = cut_cost.get(str(campaign_id), 0.0) + campaign_cost
            if not measured:
                unknown.add(str(campaign_id))
                continue
            if conversions_split:
                lost = float(conversions_split.get(str(campaign_id), 0.0))
            else:
                share = (campaign_cost / cost_total) if cost_total > 0 else 0.0
                lost = float(candidate.get("conversions") or 0.0) * share
            cut_conversions[str(campaign_id)] = (
                cut_conversions.get(str(campaign_id), 0.0) + lost)
    for phrases in desired.values():
        phrases.sort()

    return {
        "desired": desired,
        "cut_cost": {cid: round(v, 2) for cid, v in sorted(cut_cost.items())},
        "cut_conversions": {cid: round(v, 2)
                            for cid, v in sorted(cut_conversions.items())
                            if cid not in unknown},
        "unknown_conversions": sorted(unknown),
        "over_cap": over_cap,
        "invalid": invalid,
        "cost_covered": round(sum(float(c.get("cost") or 0.0) for c in taken), 2),
    }


def merge_phrases(existing: List[str], added: List[str],
                  max_total_chars: int = MAX_TOTAL_CHARS) -> List[str]:
    """Объединение прежнего списка и новых фраз в пределах бюджета символов.

    Прежние фразы неприкосновенны: их ставил человек, и вытеснять их ради
    своих — не право рычага. Не поместившиеся новые фразы просто не едут:
    следующий такт добавит их, когда место освободится.
    """
    merged = [normalize_phrase(p) for p in (existing or [])]
    used = sum(len(p) for p in merged)
    for phrase in added or []:
        normalized = normalize_phrase(phrase)
        if not normalized or normalized in merged:
            continue
        if used + len(normalized) > max_total_chars:
            continue
        merged.append(normalized)
        used += len(normalized)
    return sorted(merged)


def cut_evidence(cost_rub: float, conversions: Optional[float],
                 baseline_cpa: Optional[float],
                 window_days: int) -> Optional[Dict[str, Any]]:
    """Основание, по которому отсечение судится классом достоверности.

    Класс 0 — «утверждение о прошлом»: за зрелое окно вырезаемый трафик не дал
    ни одной конверсии при расходе выше трёх цен конверсии. Судит об этом
    writer/tier._is_arithmetic, и судит РОВНО по этому полю: нет поля — нет и
    класса 0. Числа здесь те же, по которым кандидат и выбран
    (objects.minus_word_candidates: cost / 3 против cpa_limit), поэтому агент
    минусует и объясняет минусацию одним правилом, а не двумя.

    conversions=None едет в основание КАК None: «не измеряли» обязано остаться
    отличимым от «ноль». Ноль даёт право резать без риск-бюджета, неизвестность
    не даёт.

    Порога нет вовсе (baseline_cpa пуст) — основания нет: показать, что расход
    превысил три цены конверсии, нечем, и выдумывать порог здесь нельзя.

    Один общий вход на оба рычага-близнеца (площадки зовут его отсюда):
    разъехавшись, два одинаковых по смыслу правила судили бы один и тот же
    трафик по-разному в зависимости от того, где он показался.
    """
    if baseline_cpa is None or float(baseline_cpa) <= 0:
        return None
    return {
        "cost_rub": round(float(cost_rub), 2),
        "conversions": None if conversions is None else float(conversions),
        "baseline_cpa": float(baseline_cpa),
        "window_days": int(window_days),
    }


def _idempotency_key(campaign_id: str, phrases: List[str]) -> str:
    raw = "negatives:" + str(campaign_id) + ":" + "|".join(sorted(phrases))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def diff_negatives(
    desired: Dict[str, List[str]],
    actual_by_campaign: Dict[str, Dict[str, Any]],
    cut_cost: Optional[Dict[str, float]] = None,
    window_days: int = CANDIDATE_WINDOW_DAYS,
    cut_conversions: Optional[Dict[str, float]] = None,
    baseline_cpa: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Желаемые фразы × прочитанные списки кабинета → (действия, отказы).

    actual_by_campaign — {campaign_id: {"negative_keywords": [...],
    "campaign_type": ...}} из свежего чтения (fetch_negatives). Кампании без
    записи не порождают ни действия, ни отказа: их не оказалось в кабинете.

    baseline_cpa — цена конверсии, по которой кандидаты и отбирались. Нужна не
    рычагу, а классу достоверности: без неё отсечение не может показать, что
    вырезает расход выше трёх CPA, и приезжает в отбор ставкой вместо
    арифметики — то есть платит риском и стоит в очереди позади корректировок.
    """
    actions: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []

    for campaign_id in sorted(desired):
        state = actual_by_campaign.get(str(campaign_id))
        if state is None:
            continue
        if state.get("campaign_type") not in (None, "TEXT_CAMPAIGN"):
            refused.append({"campaign_id": campaign_id,
                            "reason": DISPLAY_CAMPAIGN_REASON})
            continue

        existing = [normalize_phrase(p)
                    for p in (state.get("negative_keywords") or [])]
        merged = merge_phrases(existing, desired[campaign_id])
        if merged == sorted(existing):
            continue

        # Цена риска — дневной расход отсекаемого трафика. Расход кандидатов
        # собран за окно наблюдения (CANDIDATE_WINDOW_DAYS), поэтому делится
        # на него: горизонт замера риска считает дни, а не окна.
        cut_window = float((cut_cost or {}).get(str(campaign_id), 0.0))
        cut_daily = cut_window / max(1, int(window_days))
        # Конверсии вырезаемого трафика — тем же делением на окно, что и
        # деньги: обещание и цена риска обязаны стоять на одном окне.
        # Отсутствие кампании в словаре означает «не измеряли» (см.
        # plan_negatives: unknown_conversions), и в ОСНОВАНИЕ оно едет как
        # None; ожиданию нечем заявить неизвестность, поэтому туда идёт ноль.
        lost_window = (cut_conversions or {}).get(str(campaign_id))
        lost_leads_daily = float(lost_window or 0.0) / max(1, int(window_days))
        evidence = cut_evidence(cut_window, lost_window, baseline_cpa, window_days)
        actions.append(expectation.attach({
            "action_kind": NEGATIVE_KIND,
            **({"evidence": evidence} if evidence else {}),
            "object_level": "campaign",
            "object_id": str(campaign_id),
            "exposure": exposure.traffic_cut_exposure(
                cut_daily, f"минус-фразы ({len([p for p in merged if p not in existing])})"),
            "direct_type": "NEGATIVE_KEYWORDS",
            "key": "campaign",
            "payload": {
                "CampaignId": int(campaign_id),
                "NegativeKeywords": {"Items": merged},
                # Для рельсы и отчёта: что именно добавлено этим действием.
                "AddedPhrases": [p for p in merged if p not in existing],
            },
            "previous_state": {
                "NegativeKeywords": {"Items": sorted(existing)},
            },
            "idempotency_key": _idempotency_key(str(campaign_id), merged),
        }, {"cut_conversions_per_day": lost_leads_daily}))
    return actions, refused


def fetch_negatives(client, campaign_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Текущие минус-фразы кампаний — свежим чтением, не из витрины.

    Между прогонами список правят руками, и previous_state обязан описывать
    то, что стояло В МОМЕНТ применения, — тем же правилом, что
    fetch_budget_state.
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
            "FieldNames": ["Id", "Type", "NegativeKeywords"],
        })
        for item in result.get("Campaigns") or []:
            out[str(item["Id"])] = {
                "negative_keywords": ((item.get("NegativeKeywords") or {})
                                      .get("Items") or []),
                "campaign_type": item.get("Type"),
            }
    return out


NEGATIVE_SETTING_KIND = "negative_phrase"

# Конверсии вырезаемого трафика едут ОТДЕЛЬНОЙ строкой, а не колонкой в
# строке расхода. Причина — обратная совместимость, и она здесь не
# формальность: в таблице лежат строки, записанные до 27.08.2026, у которых
# конверсий нет вовсе. Займи конверсии колонку support_n (сегодня она дублирует
# клики из raw_value), и старую строку стало бы нечем отличить от новой —
# «конверсий не измеряли» читалось бы как «конверсий ноль». Разница между этими
# двумя не косметическая: ноль даёт действию класс 0 (арифметика, риском не
# платит, вносится весь и сразу, writer/tier.py), а неизвестность не даёт.
# Отдельная строка снимает вопрос: её ОТСУТСТВИЕ и означает «не измеряли».
NEGATIVE_CONVERSIONS_KIND = "negative_phrase_conversions"


def computed_rows(candidates: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Кандидаты расчёта → строки edu_agent_computed_settings по кампаниям.

    Фраза попадает в строки КАЖДОЙ кампании, где она жгла деньги: минусовать
    её надо там, а не по всему кабинету — в соседней кампании та же фраза
    может работать. Расход в value, клики в raw_value: писателю нужны оба,
    чтобы отсортировать кандидатов и объяснить решение в отчёте.

    Второй строкой на ту же фразу едут её конверсии в этой кампании — включая
    честный ноль. Ноль пишется явно именно затем, чтобы отсутствие строки
    осталось за «не измеряли»: см. NEGATIVE_CONVERSIONS_KIND.
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for candidate in candidates:
        phrase = normalize_phrase(candidate.get("query"))
        if not phrase:
            continue
        split = candidate.get("cost_by_campaign") or {}
        conversions_split = candidate.get("conversions_by_campaign") or {}
        for campaign_id in candidate.get("campaigns") or []:
            if not campaign_id:
                continue
            # Доля расхода ЭТОЙ кампании, а не общий расход фразы: иначе при
            # обратной сборке (candidates_from_computed) расход складывается
            # сам с собой столько раз, в скольких кампаниях фраза живёт.
            rows = out.setdefault(str(campaign_id), [])
            rows.append({
                "setting_kind": NEGATIVE_SETTING_KIND,
                "setting_key": phrase,
                "value": float(split.get(str(campaign_id),
                                         candidate.get("cost") or 0.0)),
                "raw_value": int(candidate.get("clicks") or 0),
                "support_n": int(candidate.get("clicks") or 0),
                "reason": candidate.get("reason"),
            })
            rows.append({
                "setting_kind": NEGATIVE_CONVERSIONS_KIND,
                "setting_key": phrase,
                # Конверсии этой кампании; разреза по кампаниям нет — берётся
                # общее число фразы, и обратная сборка сложит его столько раз,
                # в скольких кампаниях фраза живёт. Это ЗАВЫШЕНИЕ потерь, то
                # есть ошибка в сторону осторожности: завышенные конверсии
                # снимают класс 0 и заставляют платить риском, заниженные —
                # наоборот, раздали бы бесплатные отсечения.
                "value": float(conversions_split.get(
                    str(campaign_id), candidate.get("conversions") or 0.0)),
                "raw_value": float(candidate.get("conversions") or 0.0),
                "support_n": int(candidate.get("clicks") or 0),
                "reason": candidate.get("reason"),
            })
    return out


def candidates_from_computed(
    computed_by_campaign: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Строки computed → кандидаты в том виде, в каком их ждёт plan_negatives.

    Расход фразы складывается по всем её кампаниям: отбор за такт идёт по
    деньгам, и фраза, размазанная по трём кампаниям, стоит столько же,
    сколько собранная в одной.

    conversions у кандидата — None, если строк NEGATIVE_CONVERSIONS_KIND по
    фразе нет ни одной. Это НЕ ноль: строка старого формата означает, что
    конверсии вырезаемого трафика не измерялись, а ноль означал бы измеренное
    отсутствие и давал бы действию класс 0 — право резать без риск-бюджета.
    """
    by_phrase: Dict[str, Dict[str, Any]] = {}
    for campaign_id, rows in computed_by_campaign.items():
        for row in rows:
            kind = str(row.get("setting_kind"))
            if kind not in (NEGATIVE_SETTING_KIND, NEGATIVE_CONVERSIONS_KIND):
                continue
            phrase = normalize_phrase(row.get("setting_key"))
            if not phrase:
                continue
            slot = by_phrase.setdefault(phrase, {
                "query": phrase, "cost": 0.0, "clicks": 0,
                "conversions": None, "campaigns": [],
                "cost_by_campaign": {},
                "conversions_by_campaign": {},
                "reason": row.get("reason"),
            })
            if kind == NEGATIVE_CONVERSIONS_KIND:
                conversions = float(row.get("value") or 0.0)
                slot["conversions"] = (slot["conversions"] or 0.0) + conversions
                slot["conversions_by_campaign"][str(campaign_id)] = (
                    slot["conversions_by_campaign"].get(str(campaign_id), 0.0)
                    + conversions)
                continue
            cost = float(row.get("value") or 0.0)
            slot["cost"] += cost
            slot["clicks"] += int(row.get("raw_value") or 0)
            # Расход фразы В ЭТОЙ кампании — множитель цены риска: минус-фраза
            # ставит под удар ровно тот трафик, который отсекает, а не расход
            # всей кампании (writer/exposure.py). Сумма по кампаниям здесь уже
            # разложена строками computed, собирать её обратно не нужно.
            slot["cost_by_campaign"][str(campaign_id)] = (
                slot["cost_by_campaign"].get(str(campaign_id), 0.0) + cost)
            if str(campaign_id) not in slot["campaigns"]:
                slot["campaigns"].append(str(campaign_id))
    return sorted(by_phrase.values(), key=lambda c: -c["cost"])
