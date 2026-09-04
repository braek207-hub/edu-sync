# -*- coding: utf-8 -*-
"""
sync/agent/alerts.py — правила внутридневных тревог. Чистая логика, без БД и
без сети: на вход снимок расхода, на выход список тревог.

Почему источник — Reports API, а не витрины. Витрины EDU наливаются раз в
сутки (sync.yml, 04:50 МСК), поэтому почасовой сторож поверх них двадцать три
раза за день смотрел бы на те же вчерашние числа. Замер 04.09.2026 (проба
probe_intraday_spend, run 33857247004) показал, что отчёт за ТЕКУЩИЙ день
живой: в 12:13 МСК все 84 активные кампании четырёх кабинетов уже имели
расход, а видимая доля вчерашнего дня составила 0,233–0,304.

Два правила, и оба опираются на этот замер, а не на догадку.

  • CAMPAIGN_STOPPED — кампания вчера тратила заметно, сегодня к этому часу
    ноль. В замере таких было НОЛЬ при 84 активных кампаниях, то есть на
    нормальном дне правило молчит. Расписание показов ему не мешает: смотрится
    расход за весь сегодняшний день, а не за последний час, и кампания с
    показами 9–18 в 19:00 остаётся с ненулевым расходом.

  • ACCOUNT_COLLAPSE — кабинет открутил заметно меньшую долю вчерашнего дня,
    чем остальные кабинеты в тот же час. Контроль межкабинетный, а не
    исторический: истории почасовых долей у нас нет и появится она не раньше
    чем через недели, а разброс долей между кабинетами в замере оказался узким
    (0,233–0,304 при медиане 0,293), и половина медианы — запас втрое больше
    наблюдавшегося разброса.

Чего здесь нет намеренно. Тревоги по цене лида: лиды EDU приезжают из CRM с
лагом в 2–4 дня (память edu-crm-lag-no-maturation), и «CPA скакнул сегодня» —
это утверждение о данных, которых сегодня нет.
"""

from typing import Any, Dict, List, Optional

RULE_CAMPAIGN_STOPPED = "campaign_stopped"
RULE_ACCOUNT_COLLAPSE = "account_collapse"

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"

# Расход кампании за вчера, ниже которого её сегодняшнее молчание — не
# событие: это хвост открутки по остаткам, а не работавшая кампания.
MIN_YESTERDAY_COST = 300.0

# Расход кабинета за вчера, ниже которого доля дня — шум: у кабинета на десять
# тысяч рублей одна кампания способна сдвинуть долю вдвое.
MIN_ACCOUNT_YESTERDAY_COST = 20000.0

# Во сколько раз доля кабинета должна отстать от медианы остальных, чтобы это
# считалось обвалом. 0,5 при наблюдавшемся разбросе 0,233–0,304 (медиана
# 0,293, минимум = 0,79 медианы) — запас втрое больше разброса.
COLLAPSE_RATIO = 0.5

# Меньше скольких кабинетов делает межкабинетный контроль бессмысленным:
# медиана «остальных» из одного наблюдения — это не медиана.
MIN_PEERS = 3


def _median(values: List[float]) -> Optional[float]:
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _money(rub: float) -> str:
    """Рубли для человека. Отдельным помощником, а не в f-строке: замена
    разделителя разрядов внутри склеенного текста съедает и запятые самой
    фразы — так уже было поймано тестом.
    """
    return f"{round(float(rub or 0.0)):,}".replace(",", " ") + " ₽"


def _alert(rule: str, severity: str, account: str, subject: str,
           text: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
    return {"rule": rule, "severity": severity, "account": account,
            "subject": subject, "text": text, "evidence": evidence}


def stopped_campaigns(account: str,
                      today: Dict[str, Dict[str, Any]],
                      yesterday: Dict[str, Dict[str, Any]],
                      min_yesterday_cost: float = MIN_YESTERDAY_COST
                      ) -> List[Dict[str, Any]]:
    """Кампании, вчера тратившие, сегодня молчащие."""
    out: List[Dict[str, Any]] = []
    for campaign_id, row in (yesterday or {}).items():
        cost_yday = float(row.get("cost") or 0.0)
        if cost_yday < min_yesterday_cost:
            continue
        if float((today or {}).get(campaign_id, {}).get("cost") or 0.0) > 0.0:
            continue
        name = str(row.get("name") or campaign_id)
        out.append(_alert(
            RULE_CAMPAIGN_STOPPED, SEVERITY_HIGH, account, str(campaign_id),
            f"{name} сегодня не открутила ни рубля, вчера — {_money(cost_yday)}.",
            {"yesterday_cost": round(cost_yday, 2), "today_cost": 0.0}))
    out.sort(key=lambda a: -float(a["evidence"]["yesterday_cost"]))
    return out


def collapsed_accounts(shares: Dict[str, Dict[str, float]],
                       ratio: float = COLLAPSE_RATIO,
                       min_yesterday_cost: float = MIN_ACCOUNT_YESTERDAY_COST,
                       min_peers: int = MIN_PEERS) -> List[Dict[str, Any]]:
    """Кабинеты, отставшие по доле дня от остальных.

    shares: логин → {"share": доля вчерашнего дня к этому часу,
                     "today_cost": ..., "yesterday_cost": ...}.

    Медиана считается по ОСТАЛЬНЫМ кабинетам, а не по всем: кабинет, попавший
    в собственный контроль, тянет медиану к себе и тем прячет собственный
    обвал — тем сильнее, чем крупнее кабинет.
    """
    usable = {login: row for login, row in (shares or {}).items()
              if float(row.get("yesterday_cost") or 0.0) >= min_yesterday_cost
              and row.get("share") is not None}
    out: List[Dict[str, Any]] = []
    for login, row in usable.items():
        peers = [float(other["share"]) for name, other in usable.items()
                 if name != login]
        if len(peers) < min_peers - 1:
            continue
        median = _median(peers)
        if not median:
            continue
        share = float(row["share"])
        if share >= median * ratio:
            continue
        out.append(_alert(
            RULE_ACCOUNT_COLLAPSE, SEVERITY_HIGH, login, login,
            f"Кабинет {login} к этому часу открутил {round(share * 100)} % "
            f"вчерашнего дня, остальные — {round(median * 100)} %.",
            {"share": round(share, 4), "peers_median": round(median, 4),
             "today_cost": round(float(row.get("today_cost") or 0.0), 2),
             "yesterday_cost": round(float(row.get("yesterday_cost") or 0.0), 2)}))
    out.sort(key=lambda a: float(a["evidence"]["share"]))
    return out


def alert_key(alert: Dict[str, Any], day_msk: str) -> str:
    """Ключ дедупа: правило + предмет + день.

    День, а не час: кампания, вставшая утром, будет молчать до вечера, и
    почасовой сторож без этого ключа прислал бы восемь одинаковых сообщений.
    Назавтра ключ другой — если не починили, человек услышит снова.
    """
    return f"{alert['rule']}:{alert['account']}:{alert['subject']}:{day_msk}"


# Сколько тревог называется поимённо в одном сообщении. Остальные — счётчиком:
# список из тридцати кампаний человек не читает, а «и ещё 27» показывает и
# главное, и масштаб.
SHOWN = 5


def summary(alerts: List[Dict[str, Any]], hour_msk: int) -> str:
    """Сообщение человеку. Молчание при пустом списке — забота вызывающего:
    тревога без события хуже отсутствия сторожа, потому что учит не смотреть.
    """
    high = [a for a in alerts if a["severity"] == SEVERITY_HIGH]
    lines = [f"Тревога агента · {hour_msk:02d}:00 МСК",
             f"Событий: {len(alerts)}"
             + (f", срочных {len(high)}." if len(high) != len(alerts) else ".")]
    for alert in alerts[:SHOWN]:
        lines.append("· " + str(alert["text"])[:220])
    if len(alerts) > SHOWN:
        lines.append(f"…и ещё {len(alerts) - SHOWN}.")
    lines.append("Проверь кабинет: сторож только смотрит, сам он ничего "
                 "не включает и не выключает.")
    return "\n".join(lines)
