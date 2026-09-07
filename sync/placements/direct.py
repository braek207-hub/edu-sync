# -*- coding: utf-8 -*-
"""Доступ к Директу для чистильщика: отчёт площадок, чтение и запись запретов.

Отдельный клиент, а не заимствование sync/direct.py: тот заточен под
ежесуточную витрину расхода и ходит по своему набору кабинетов, а
чистильщику нужен внутридневной срез по площадкам у произвольного клиента.
"""

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple

API = "https://api.direct.yandex.com/json/v5/"
REPORTS = "https://api.direct.yandex.com/json/v5/reports"

# Отчёт формируется в очереди: HTTP 201/202 значит «ещё не готов».
MAX_POLL = 12
POLL_SLEEP = 10

# Ограничения Директа на список запрещённых площадок (ref-v5/campaigns).
MAX_EXCLUDED_SITES = 1000
MAX_SITE_CHARS = 255

# Запрет площадок живёт только у текстовых кампаний. У ЕПК, Мастера кампаний
# и медийных этого рычага нет вовсе — их пропускаем, а не пытаемся писать.
CLEANABLE_TYPES = frozenset({"TEXT_CAMPAIGN"})

# Пишем только в кампании, которые ещё живут. Архивную и сконвертированную
# Директ обновлять не даст вовсе, завершённая по дате уже не покажется —
# запрет в ней тратит такт впустую. Остановленная (OFF/SUSPENDED) чистится:
# клики сегодня у неё были, а вернётся она уже без этого мусора.
CLEANABLE_STATES = frozenset({"ON", "OFF", "SUSPENDED"})


class DirectError(RuntimeError):
    pass


def _headers(token: str, login: str) -> Dict[str, str]:
    return {
        "Authorization": "Bearer " + token,
        "Accept-Language": "ru",
        "Client-Login": login,
        "Content-Type": "application/json; charset=utf-8",
    }


def call(token: str, login: str, service: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API + service, data=data,
                                 headers=_headers(token, login))
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if "error" in body:
        err = body["error"]
        raise DirectError("%s [%s] %s: %s" % (
            service, err.get("error_code"), err.get("error_string"),
            err.get("error_detail")))
    return body.get("result", {})


def campaigns(token: str, login: str) -> List[Dict[str, Any]]:
    """Кампании кабинета с типом, состоянием и текущими запретами.

    ExcludedSites читается тем же вызовом, что и всё остальное: список в API
    заменяется ЦЕЛИКОМ, поэтому писать его можно только поверх свежего
    чтения — иначе запись затрёт запреты, поставленные человеком.
    """
    out: List[Dict[str, Any]] = []
    offset = 0
    while True:
        res = call(token, login, "campaigns", {
            "method": "get",
            "params": {
                "SelectionCriteria": {},
                "FieldNames": ["Id", "Name", "Type", "State", "Status",
                               "ExcludedSites"],
                "Page": {"Limit": 1000, "Offset": offset},
            },
        })
        chunk = res.get("Campaigns", [])
        out.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += len(chunk)
    return out


def placements_today(token: str, login: str) -> List[Dict[str, Any]]:
    """Площадки сети за сегодня: кампания, имя площадки, клики, расход.

    Окно — TODAY, а не скользящая неделя. С недельным окном топ по кликам
    навсегда занят накопленной историей (Дзен, ВК, mail.ru), и свежая
    мусорная площадка с полусотней кликов туда не пролезет — ровно тот
    случай, ради которого чистильщик и ходит ежечасно.

    Строки поисковой сети отбрасываются: «площадка» там означает саму
    выдачу Яндекса, запрещать её нечем и незачем.
    """
    body = {"params": {
        "SelectionCriteria": {"Filter": [{"Field": "Clicks",
                                          "Operator": "GREATER_THAN",
                                          "Values": ["0"]}]},
        "FieldNames": ["CampaignId", "Placement", "AdNetworkType", "Cost",
                       "Clicks", "Impressions"],
        "ReportName": "placement-cleaner-%s-%d" % (login, int(time.time())),
        "ReportType": "CUSTOM_REPORT",
        "DateRangeType": "TODAY",
        "Format": "TSV",
        "IncludeVAT": "YES",
        "IncludeDiscount": "NO",
    }}
    headers = dict(_headers(token, login))
    headers.update({"processingMode": "auto", "returnMoneyInMicros": "false",
                    "skipReportHeader": "true", "skipReportSummary": "true"})
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    text = None
    for attempt in range(1, MAX_POLL + 1):
        req = urllib.request.Request(REPORTS, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                if resp.status == 200:
                    text = resp.read().decode("utf-8")
                    break
        except urllib.error.HTTPError as err:
            raise DirectError("reports HTTP %s: %s"
                              % (err.code, err.read().decode("utf-8")[:400]))
        time.sleep(POLL_SLEEP)
    if text is None:
        raise DirectError("отчёт не сформировался за %d попыток" % MAX_POLL)

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        rec = dict(zip(header, line.split("\t")))
        if str(rec.get("AdNetworkType", "")).upper() != "AD_NETWORK":
            continue
        if not rec.get("Placement"):
            continue
        rows.append({
            "campaign_id": str(rec.get("CampaignId") or ""),
            "placement": rec["Placement"],
            "clicks": int(float(rec.get("Clicks") or 0)),
            "cost": float(rec.get("Cost") or 0.0),
            "impressions": int(float(rec.get("Impressions") or 0)),
        })
    return rows


def set_excluded_sites(token: str, login: str,
                       campaign_id: str, sites: List[str]) -> Tuple[bool, str]:
    """Записать полный список запрещённых площадок кампании.

    Возвращает (успех, сообщение). Ошибка одной кампании не должна ронять
    прогон: у кабинета их десятки, и остальные вычистить всё равно надо.
    """
    try:
        res = call(token, login, "campaigns", {
            "method": "update",
            "params": {"Campaigns": [{
                "Id": int(campaign_id),
                "ExcludedSites": {"Items": sites},
            }]},
        })
    except DirectError as err:
        return False, str(err)
    except urllib.error.URLError as err:
        return False, "сеть: %s" % err

    results = res.get("UpdateResults") or []
    if not results:
        return False, "Директ не вернул результат обновления"
    item = results[0]
    errors = item.get("Errors") or []
    if errors:
        return False, "; ".join("%s %s" % (e.get("Code"), e.get("Message"))
                                for e in errors)
    warnings = item.get("Warnings") or []
    note = "; ".join(w.get("Message", "") for w in warnings)
    return True, note
