# -*- coding: utf-8 -*-
"""
sync/agent/quality.py — качество когорты лидов как ранний тормоз доливки.

Механизм роста без контроля качества оптимизирует то, что видит: заявки.
Заявка при расширении охвата дешевеет, оплата — дорожает, и разрыв виден
только на денежном чекпоинте (35 дней). Средний ML-скор оплаты по лидам
кампании доступен на следующий день после лида и играет роль предохранителя:
доливка кампании, чья когорта портится, ставится на паузу до вердикта по
деньгам. Пауза роста, а не сокращение: прокси для того и ранний, что менее
точен, и резать по нему деньги значило бы судить о деньгах не по деньгам.

Знаменатель среднего — scored_leads, а не eff_leads. Скор требует client_id,
он проставлен не на всех лендах, лид без скора входит в sum_p_pay нулём.
Отношение sum_p_pay / eff_leads меряло бы долю скоренных лидов: кампания с
выросшей долей мобильного трафика показала бы «падение качества», не
изменившись ни на грамм. Само это отношение считается отдельно (coverage) и
печатается рядом — его падение означает поломку ingest'а поведения.

Родственный механизм на ДРУГОЙ оси — sync/agent/segment_quality.py: там
качество лида сравнивается между сегментами внутри кампании через мост
client_id × Метрика. Общего кода у них нет, но термин «качество лида» обязан
значить одно и то же: ожидаемые деньги на лид, а не количество лидов.
"""

from typing import Any, Dict, Iterable, List

# Относительное падение среднего скора, после которого доливка кампании
# останавливается.
QUALITY_DROP_LIMIT = 0.2
# Минимум лидов со скором в КАЖДОМ окне: на десятке лидов средний скор гуляет
# сильнее порога, и тормоз срабатывал бы на шуме, останавливая рост там, где
# его надо продолжать.
MIN_QUALITY_LEADS = 20
# Падение покрытия той же величины значит совсем другое — не «трафик стал
# хуже», а «часть лидов осталась без скора». Порог общий по одной причине:
# ниже него оба числа неотличимы от дневных колебаний состава лендов.
COVERAGE_DROP_LIMIT = 0.2

# Длина окна когорты. 28 дней — не меньше горизонта замера действия и не
# больше того, за что когорта успевает смениться: на семи днях средний скор
# гуляет вслед за днями недели, на девяноста доливка растворяется в истории.
QUALITY_WINDOW_DAYS = 28

REASON_THIN = "мало наблюдений"
REASON_DROP = "качество когорты упало"


def lead_quality(rows: Iterable[Dict[str, Any]], date_from: str,
                 date_to: str) -> Dict[str, Dict[str, float]]:
    """Средний скор оплаты на лид по кампаниям за окно.

    Взвешивание — по лидам, а не по дням: день с тремя лидами и день с
    тридцатью не равноправны, среднее из дневных средних дало бы вес
    случайности.
    """
    acc: Dict[str, Dict[str, float]] = {}
    for row in rows:
        day = str(row.get("fact_date") or "")
        if not (date_from <= day <= date_to):
            continue
        slot = acc.setdefault(str(row.get("campaign_id") or ""),
                              {"leads": 0.0, "scored_leads": 0.0, "sum_p_pay": 0.0})
        slot["leads"] += float(row.get("eff_leads") or 0.0)
        slot["scored_leads"] += float(row.get("scored_leads") or 0.0)
        slot["sum_p_pay"] += float(row.get("sum_p_pay") or 0.0)

    out: Dict[str, Dict[str, float]] = {}
    for campaign_id, slot in acc.items():
        scored, leads = slot["scored_leads"], slot["leads"]
        out[campaign_id] = {
            "leads": leads,
            "scored_leads": scored,
            "avg_p_pay": round(slot["sum_p_pay"] / scored, 4) if scored else 0.0,
            "coverage": round(scored / leads, 4) if leads else 0.0,
        }
    return out


def quality_drift(before: Dict[str, Dict[str, float]],
                  after: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, Any]]:
    """Падение среднего скора между окнами: до доливки и после.

    Ключи — кампании ВТОРОГО окна: кампания, исчезнувшая из выдачи, доливки
    всё равно не получит, а судить её не по чему.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for campaign_id, now in after.items():
        was = before.get(campaign_id) or {}
        base = float(was.get("avg_p_pay") or 0.0)
        current = float(now.get("avg_p_pay") or 0.0)
        drop = round((base - current) / base, 4) if base > 0 else 0.0

        coverage_base = float(was.get("coverage") or 0.0)
        coverage_now = float(now.get("coverage") or 0.0)
        coverage_drop = (round((coverage_base - coverage_now) / coverage_base, 4)
                         if coverage_base > 0 else 0.0)

        thin = (float(was.get("scored_leads") or 0.0) < MIN_QUALITY_LEADS
                or float(now.get("scored_leads") or 0.0) < MIN_QUALITY_LEADS)
        if thin:
            out[campaign_id] = {"drop": drop, "flagged": False,
                                "reason": REASON_THIN, "coverage_drop": coverage_drop}
            continue

        flagged = drop >= QUALITY_DROP_LIMIT
        out[campaign_id] = {"drop": drop, "flagged": flagged,
                            "reason": REASON_DROP if flagged else "",
                            "coverage_drop": coverage_drop}
    return out


def lead_quality_section(rows: Iterable[Dict[str, Any]],
                         before_from: str, before_to: str,
                         after_from: str, after_to: str) -> Dict[str, Any]:
    """Секция отчёта такта плюс готовая карта тормоза для growth_candidates.

    Секция печатается всегда, в том числе пустыми списками: отсутствие секции
    неотличимо от отсутствия падений, а различать их нужно — молчание тормоза
    и его поломка выглядят одинаково.
    """
    rows = list(rows)
    before = lead_quality(rows, before_from, before_to)
    after = lead_quality(rows, after_from, after_to)
    drift = quality_drift(before, after)

    flagged: List[Dict[str, Any]] = []
    coverage_alerts: List[Dict[str, Any]] = []
    for campaign_id, row in sorted(drift.items()):
        was, now = before.get(campaign_id, {}), after[campaign_id]
        if row["flagged"]:
            percent = round(row["drop"] * 100)
            flagged.append({
                "campaign_id": campaign_id,
                "avg_p_pay_before": was.get("avg_p_pay", 0.0),
                "avg_p_pay_after": now["avg_p_pay"],
                "drop": row["drop"],
                "scored_leads_before": was.get("scored_leads", 0.0),
                "scored_leads_after": now["scored_leads"],
                "note": (f"скор упал с {was.get('avg_p_pay', 0.0)} до "
                         f"{now['avg_p_pay']} (−{percent} %), рост приостановлен "
                         f"до денежного чекпоинта"),
            })
        # Покрытие судится по ВСЕМ лидам окна: обвал скоринга режет как раз
        # scored_leads, и порог по ним замолчал бы ровно в тот момент, ради
        # которого сигнал заведён.
        thin = (float((was or {}).get("leads") or 0.0) < MIN_QUALITY_LEADS
                or float(now.get("leads") or 0.0) < MIN_QUALITY_LEADS)
        if not thin and row["coverage_drop"] >= COVERAGE_DROP_LIMIT:
            coverage_alerts.append({
                "campaign_id": campaign_id,
                "coverage_before": was.get("coverage", 0.0),
                "coverage_after": now["coverage"],
                "coverage_drop": row["coverage_drop"],
                "note": ("покрытие скором упало с "
                         f"{was.get('coverage', 0.0)} до {now['coverage']}: "
                         "это про ingest поведения, а не про качество трафика"),
            })

    return {
        "window_before": [before_from, before_to],
        "window_after": [after_from, after_to],
        "flagged": flagged,
        "coverage_alerts": coverage_alerts,
        "drift": drift,
    }
