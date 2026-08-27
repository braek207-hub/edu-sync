# -*- coding: utf-8 -*-
"""
sync/agent/ideas/feedback_out.py — исход запущенной кампании возвращается
билдеру (Ф14, задача 19).

Петля замыкается здесь. Билдер уже дообучается на боевых поисковых запросах
(builder/feedback.py): расход без конверсий идёт в маркеры отказа, запросы с
конверсиями — в подтверждения, невиданное — в маски следующего сбора. Но про
кампанию он знает только клики и расход, а расход — не исход. Кампания,
вынесшая связки в отдельную РК, могла потратить ровно столько же и при этом
побить донорскую цену конверсии или провалить её; разница между этими двумя
случаями решает, повторять ли вынос вообще.

**Судим тем критерием, который выписан в наряде.** Метрика, порог и база
сравнения объявлены заранее (build_order.validate) и уехали билдеру вместе с
кампанией. Вердикт, посчитанный «своей формулой отчёта», был бы оценкой
задним числом — то есть не вердиктом.

**Молодая кампания — unknown, а не провал.** Горизонт наряда посчитан под
порог значимости (power.MIN_EXPECTED_PAYMENTS): столько оплат нужно, чтобы
решение на объекте вообще имело силу. Кампания, не дожившая до горизонта, не
«не справилась» — её нечем судить, и назвать это провалом значило бы закрывать
идеи по нетерпению. То же у молчащего объёма: не набралось оплат — inconclusive,
а не «хуже».

**Разбор фраз остаётся у ПОЛУЧАТЕЛЯ.** Отсюда уезжают сырые строки запросов;
что из них мусор, что победители и что маркер отказа, решает билдер своими
порогами (MIN_CLICKS_WASTE, MIN_MARKER_HITS). Вторая копия этих порогов здесь
разъехалась бы с ними на первой же правке, и агент отдавал бы разбор, которого
получатель не признаёт.

Модуль двухслойный, как и остальные: `report` — чистая функция от наряда и
фактов, `for_order` — тонкая обвязка, которая берёт то же самое из базы.
Кампания находится ПО ИМЕНИ — тем же ключом, которым держится идемпотентность
заливки (direct/upload.py ищет кампанию по Name, витрина фактов хранит
campaign_name).
"""

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

from sync.agent import db, power
from sync.agent.ideas import registry

VERDICT_IMPROVED = "improved"
VERDICT_WORSENED = "worsened"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICT_UNKNOWN = "unknown"

VERDICTS = (VERDICT_IMPROVED, VERDICT_WORSENED, VERDICT_INCONCLUSIVE,
            VERDICT_UNKNOWN)

REASON_NOT_LAUNCHED = (
    "наряд не уехал в кабинет: кампании нет, судить нечего")
REASON_NO_SPEND = (
    "кампания не откручивалась: наряд применён, показов не было — это не "
    "провал выноса, а невключённая кампания")
REASON_TOO_YOUNG = (
    "кампания моложе своего горизонта: срок назначен наряду под порог "
    "значимости, и вердикт раньше него был бы нетерпением, а не измерением")
REASON_NO_CONVERSIONS = (
    "полный горизонт пройден, деньги потрачены, ни одной конверсии: объёма "
    "для статистики не будет, и это факт, а не нехватка данных")
REASON_THIN = (
    f"за горизонт не набралось {power.MIN_EXPECTED_PAYMENTS:g} ожидаемых "
    "оплат: решение на объекте не имеет силы")
REASON_NO_RULE = (
    "у наряда нет критерия успеха: сравнивать факт не с чем")


def _number(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _day(value: Any) -> Optional[date]:
    if isinstance(value, date):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def report(order: Dict[str, Any], facts: Sequence[Dict[str, Any]], *,
           applied_on: Any, today: Any,
           queries: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """Наряд + дневные факты его кампании → отчёт билдеру.

    facts — строки витрины (edu_agent_facts) за окно наблюдения: расход,
    эффективные лиды и ожидаемые оплаты по дням. Агрегат считается здесь, а
    не запросом: окно у каждого наряда своё, отсчитанное от его собственного
    применения, и общий SUM по кампании смешал бы наряды между собой.

    applied_on — день, когда наряд уехал (журнал записей, applied_at). None
    означает, что кампании в кабинете нет вовсе: вердикт unknown, и это не
    оценка выноса.
    """
    started = _day(applied_on)
    now = _day(today) or date.today()
    rule = dict(order.get("success_rule") or {})
    horizon = int(_number(order.get("horizon_days")) or 0)

    rows = [row for row in facts or () if row]
    cost = sum(_number(row.get("cost")) or 0.0 for row in rows)
    leads = sum(_number(row.get("eff_leads")) or 0.0 for row in rows)
    payments = sum(_number(row.get("sum_p_pay")) or 0.0 for row in rows)
    days_live = len({_text(row.get("fact_date"))[:10] for row in rows
                     if _text(row.get("fact_date"))})
    campaign_id = next((_text(row.get("campaign_id")) for row in rows
                        if _text(row.get("campaign_id"))), None)

    verdict, reason = _verdict(rule, horizon=horizon, days_live=days_live,
                              started=started, cost=cost, leads=leads,
                              payments=payments)

    return {
        # Обратный адрес. Без idea_id вердикт некуда вернуть: реестр закрывает
        # идею по нему, а не по имени кампании.
        "order_id": _text(order.get("order_id")),
        "idea_id": _text(order.get("idea_id")) or None,
        "account": _text(order.get("account")),
        # Уровень на диске билдера: тем же слагом он собирал кампанию, им же
        # адресует находки в словарь ниши.
        "level_slug": _text(order.get("level_slug")),
        "campaign_name": _text(order.get("campaign_name")),
        "campaign_id": campaign_id,
        "verdict": verdict,
        "reason": reason,
        "horizon_days": horizon,
        "days_live": days_live,
        # Окно обязательно: «цена конверсии 1 200 ₽» без отрезка — число без
        # смысла, и получатель не смог бы сопоставить его со своим отчётом.
        "window": _window(started, horizon, now),
        "baseline": {
            "metric": _text(rule.get("metric")),
            "op": _text(rule.get("op")) or "<=",
            "threshold": _number(rule.get("threshold")),
            "comparison": _text(rule.get("comparison")),
        },
        "fact": {
            "cost_rub": round(cost, 2),
            "eff_leads": round(leads, 2),
            "cpa_rub": round(cost / leads, 2) if leads > 0 else None,
            "expected_payments": round(payments, 2),
        },
        # Сырьё для builder.analyse — без классификации: пороги мусора и
        # маркеров живут у него.
        "queries": [{"query": _text(q.get("query")),
                     "clicks": int(_number(q.get("clicks")) or 0),
                     "cost_rub": round(_number(q.get("cost_rub")) or 0.0, 2),
                     "conversions": int(_number(q.get("conversions")) or 0)}
                    for q in queries or () if _text(q.get("query"))],
    }


def _window(started: Optional[date], horizon: int,
            now: date) -> Dict[str, Optional[str]]:
    """Отрезок, за который посчитан факт: от применения на горизонт наряда.

    Конец обрезается сегодняшним днём: окно, уходящее в будущее, читалось бы
    как «данных нет», хотя их просто ещё не случилось.
    """
    if started is None:
        return {"from": None, "to": None}
    end = started + timedelta(days=max(0, horizon - 1))
    return {"from": started.isoformat(), "to": min(end, now).isoformat()}


def _verdict(rule: Dict[str, Any], *, horizon: int, days_live: int,
             started: Optional[date], cost: float, leads: float,
             payments: float):
    """Исход по критерию наряда. Порядок проверок — от адреса к статистике.

    Он не переставляем. «Рано судить» обязано стоять ВЫШЕ любой оценки цены:
    первые дни новая кампания дороже по построению — стратегия выходит из
    обучения, — и вердикт, посчитанный до горизонта, систематически ругал бы
    удачные выносы. А «расход без единой конверсии» стоит ВЫШЕ порога объёма:
    объёма там нет и не будет, но это исход, а не нехватка данных.
    """
    if started is None:
        return VERDICT_UNKNOWN, REASON_NOT_LAUNCHED
    if days_live <= 0 or cost <= 0:
        return VERDICT_UNKNOWN, REASON_NO_SPEND
    if horizon > 0 and days_live < horizon:
        return VERDICT_UNKNOWN, REASON_TOO_YOUNG
    if leads <= 0:
        return VERDICT_WORSENED, REASON_NO_CONVERSIONS
    if payments < power.MIN_EXPECTED_PAYMENTS:
        return VERDICT_INCONCLUSIVE, REASON_THIN

    threshold = _number(rule.get("threshold"))
    if threshold is None or threshold <= 0:
        return VERDICT_UNKNOWN, REASON_NO_RULE

    cpa = cost / leads
    op = _text(rule.get("op")) or "<="
    beaten = cpa <= threshold if op in ("<=", "<") else cpa >= threshold
    if beaten:
        return VERDICT_IMPROVED, (
            f"цена конверсии {round(cpa)} ₽ против донорской "
            f"{round(threshold)} ₽ за {days_live} дн.")
    return VERDICT_WORSENED, (
        f"цена конверсии {round(cpa)} ₽ выше донорской {round(threshold)} ₽ "
        f"за {days_live} дн.: вынос не окупил переезда")


# ------------------------------------------------------------ сборка из базы


def for_order(order_id: str, *, today: Optional[str] = None,
              account: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Отчёт по наряду из базы. None — наряда с таким order_id в реестре нет.

    Наряд берётся из колонки action открытой или закрытой идеи, а не из файла
    на диске: файл — то, что уехало билдеру, а правда о том, чем агент
    распорядился, живёт в реестре.
    """
    idea = _idea_of(order_id, account=account)
    if idea is None:
        return None
    order = ((idea.get("action") or {}).get("payload") or {}).get("order") or {}
    if not order:
        return None

    applied_on = _applied_on(idea)
    horizon = int(_number(order.get("horizon_days")) or 0)
    window = _window(_day(applied_on), horizon, _day(today) or date.today())
    facts, queries = [], []
    if window["from"]:
        facts = _facts_of(_text(order.get("campaign_name")),
                          window["from"], window["to"])
        queries = _queries_of([_text(row.get("campaign_id")) for row in facts],
                              window["from"], window["to"])
    return report(order, facts, applied_on=applied_on, today=today or date.today(),
                  queries=queries)


def _idea_of(order_id: str, *, account: Optional[str]) -> Optional[Dict[str, Any]]:
    """Идея, чей наряд несёт этот order_id.

    Реестр ищет по ВСЕМ статусам, а не среди открытых: вердикт нужен и тогда,
    когда идея уже закрыта, — иначе билдер не узнал бы исход как раз у тех
    выносов, которые дожили до конца горизонта.
    """
    return registry.find_by_order(_text(order_id), account=account)


def _applied_on(idea: Dict[str, Any]) -> Optional[str]:
    """День, когда наряд уехал: applied_at строки журнала по ключу действия.

    Не created_at идеи и не updated_at: идея заводится расчётным тактом и
    переписывается каждым прогоном генератора, а окно наблюдения отсчитывается
    от МОМЕНТА ПРИМЕНЕНИЯ. Разойдись эти две даты — и вердикт судил бы
    кампанию по дням, которых у неё не было.
    """
    from sync.agent.writer import db as writer_db

    key = _text((idea.get("action") or {}).get("idempotency_key"))
    if not key:
        return None
    row = writer_db.find_action_by_key(key)
    applied = (row or {}).get("applied_at")
    return applied.date().isoformat() if hasattr(applied, "date") else _text(applied) or None


def _facts_of(campaign_name: str, date_from: str,
              date_to: str) -> List[Dict[str, Any]]:
    """Дневные факты кампании ПО ИМЕНИ — тем же ключом, что у заливки.

    Id новой кампании агенту неизвестен: её заводил другой репозиторий, и
    обратно к нам он приезжает только в витрине. Имя же выписано в наряде и
    проверено валидатором на совпадение с order_id.
    """
    name = _text(campaign_name)
    if not name:
        return []
    return db._fetch_dicts(
        """
        SELECT campaign_id, fact_date, cost, eff_leads, sum_p_pay
          FROM edu_agent_facts
         WHERE campaign_name = %s AND fact_date BETWEEN %s AND %s
         ORDER BY fact_date
        """,
        (name, date_from, date_to),
    )


def _queries_of(campaign_ids: Sequence[str], date_from: str,
                date_to: str) -> List[Dict[str, Any]]:
    """Поисковые запросы кампании за окно — сырьё дообучения билдера."""
    ids = sorted({_text(c) for c in campaign_ids if _text(c)})
    if not ids:
        return []
    rows = db._fetch_dicts(
        """
        SELECT query, SUM(clicks) AS clicks, SUM(cost) AS cost_rub,
               SUM(conversions) AS conversions
          FROM edu_agent_search_queries
         WHERE campaign_id = ANY(%s) AND window_from >= %s AND window_to <= %s
         GROUP BY query
         ORDER BY SUM(cost) DESC
        """,
        (ids, date_from, date_to),
    )
    return [dict(row) for row in rows]
