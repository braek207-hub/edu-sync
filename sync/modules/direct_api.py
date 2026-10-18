# -*- coding: utf-8 -*-
"""Общий клиент Директа для модулей: отчёты, постраничное чтение, списки кампании."""

import hashlib
import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sync.direct_reports_limit import (MIN_POLL_SECONDS, is_rate_limited,
                                       poll_delay)
from sync.placements.direct import REPORTS, DirectError, _headers, call

# Потолок суммарного ожидания отчёта: дольше очередь Директа не стоит ждать в такте.
MAX_WAIT_SECONDS = 600
CHUNK = 10
PAGE_LIMIT = 1000

RESULT_KEYS = {"campaigns": "Campaigns", "keywords": "Keywords",
               "adgroups": "AdGroups", "ads": "Ads"}

Post = Callable[[str, bytes, Dict[str, str]], Tuple[int, Dict[str, str], str]]


def _http_post(url: str, body: bytes, headers: Dict[str, str]):
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            status, hdrs, raw = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        status, hdrs, raw = err.code, dict(err.headers or {}), err.read()
    # Директ отдаёт UTF-8 без charset в заголовке: кодировку задаём явно.
    return status, hdrs, raw.decode("utf-8", errors="replace")


def _report_name(prefix: str, params: Dict[str, Any]) -> str:
    # Хеш параметров (как segments._stamp_report_name): Директ связывает имя с
    # параметрами, смена параметров под тем же именем даёт ошибку 4000.
    # segments тянет тяжёлые импорты агента, поэтому приём повторён здесь.
    body = {k: v for k, v in params.items() if k != "ReportName"}
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:10]
    return "%s-%s" % (prefix, digest)


def _header(headers: Dict[str, str], name: str) -> Any:
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return v
    return None


def report_rows(token: str, login: str, params: dict, *, prefix: str,
                sleep=time.sleep, post: Post = _http_post,
                clock=time.monotonic) -> List[Dict[str, str]]:
    params = dict(params)
    params["ReportName"] = _report_name(prefix, params)
    body = json.dumps({"params": params}, ensure_ascii=False).encode("utf-8")
    headers = dict(_headers(token, login))
    headers.update({"processingMode": "auto", "returnMoneyInMicros": "false",
                    "skipReportHeader": "true", "skipReportSummary": "true"})

    # Бюджет считаем по стенным часам: медленные запросы тоже съедают время такта.
    start = clock()
    last_error = ""
    while True:
        try:
            status, hdrs, text = post(REPORTS, body, headers)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
                ConnectionError) as err:
            # Сеть моргнула: повтор под тем же именем, как и при 500.
            status, hdrs, text = None, {}, ""
            last_error = "сеть: %s" % err
            delay = MIN_POLL_SECONDS
        else:
            if status == 200:
                break
            if status in (201, 202):
                delay = poll_delay(_header(hdrs, "retryIn"), MIN_POLL_SECONDS)
            elif status == 500 or is_rate_limited(status, text):
                # 500 = очередь отчётов занята; имя не меняем, иначе встанем в очередь заново.
                delay = MIN_POLL_SECONDS
            else:
                raise DirectError("reports HTTP %s: %s" % (status, text[:400]))
        # Огромный retryIn не должен выводить за бюджет: паузу режем по остатку.
        remaining = MAX_WAIT_SECONDS - (clock() - start)
        if remaining <= 0:
            msg = "отчёт не готов за %d с" % MAX_WAIT_SECONDS
            raise DirectError(msg + (" (%s)" % last_error if last_error else ""))
        sleep(min(delay, remaining))

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    header = lines[0].split("\t")
    return [dict(zip(header, ln.split("\t"))) for ln in lines[1:]]


def get_all(token: str, login: str, service: str, criteria: dict,
            field_names: List[str], extra: Optional[dict] = None,
            chunk_key: Optional[str] = None, ids: Sequence = (),
            result_key: Optional[str] = None, call_fn=call) -> List[dict]:
    chunks: List[Optional[list]] = [None]
    if chunk_key:
        chunks = [list(ids[i:i + CHUNK]) for i in range(0, len(ids), CHUNK)]
    key = result_key or RESULT_KEYS.get(service) or service.capitalize()
    out: List[dict] = []
    for chunk in chunks:
        crit = dict(criteria)
        if chunk is not None:
            crit[chunk_key] = chunk
        offset = 0
        while True:
            params = {"SelectionCriteria": crit, "FieldNames": field_names,
                      "Page": {"Limit": PAGE_LIMIT, "Offset": offset}}
            params.update(extra or {})
            res = call_fn(token, login, service, {"method": "get", "params": params})
            page = res.get(key) or []
            out.extend(page)
            # LimitedBy есть только когда страница обрезана и данные остались.
            if "LimitedBy" not in res:
                break
            if not page:
                raise DirectError("%s.get: LimitedBy при пустой странице" % service)
            offset = res["LimitedBy"]
    return out


def keywords_by_campaign(token: str, login: str, campaign_ids: Sequence[int],
                         call_fn=call) -> Dict[str, List[str]]:
    rows = get_all(token, login, "keywords", {}, ["Id", "CampaignId", "Keyword"],
                   chunk_key="CampaignIds", ids=campaign_ids, call_fn=call_fn)
    out: Dict[str, List[str]] = {}
    for r in rows:
        kw = r.get("Keyword", "")
        if kw == "---autotargeting":
            continue
        out.setdefault(str(r["CampaignId"]), []).append(kw)
    return out


def update_campaign_list(token: str, login: str, campaign_id: int, field: str,
                         items: List[str], call_fn=call) -> dict:
    res = call_fn(token, login, "campaigns", {
        "method": "update",
        "params": {"Campaigns": [{"Id": int(campaign_id), field: {"Items": items}}]},
    })
    results = res.get("UpdateResults") or []
    if not results:
        raise DirectError("campaigns.update: Директ не вернул UpdateResults")
    return results[0]


def read_campaign_list(token: str, login: str, campaign_id: int, field: str,
                       call_fn=call) -> Optional[List[str]]:
    # Мастер кампаний API не показывает: пустой ответ означает «не видна», а не «список пуст».
    rows = get_all(token, login, "campaigns", {"Ids": [int(campaign_id)]},
                   ["Id", field], call_fn=call_fn)
    if not rows:
        return None
    return list((rows[0].get(field) or {}).get("Items") or [])
