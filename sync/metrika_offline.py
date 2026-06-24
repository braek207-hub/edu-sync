"""Импорт офлайн-конверсий в Яндекс Метрику.

Каждый шаг воронки лида (соединение/сделка/оплата) — отдельная цель Метрики типа
«JavaScript-событие» (идентификаторы connection/deal/payment). Грузим по ClientID
(столбец «Yandex Client ID» листа «Лиды»), в счётчик, соответствующий ленду.

Корректность:
- каждая цель грузится по СВОЕМУ флагу → переход на следующий шаг только ДОБАВЛЯет
  цель, предыдущая не откатывается (Метрика конверсии накапливает);
- дедуп через журнал metrika_uploaded_conversions по (counter, clientId, target) →
  никогда не грузим повторно;
- источник флагов — те же колонки, что и дашборд (connect / Сделка / лист «Оплаты»).
"""

import csv
import io
import os
from typing import Any, Dict, List, Tuple

import requests

from sync.crm import crm_leads_sheets, crm_payments_sheets, re_match_orders, re_match_revenue
from sync.sheets import get_sheets_service, read_sheet
from sync.utils import normalize_campaign_id, pick_index_loose, to_datetime_ms, to_num

API_URL = "https://api-metrika.yandex.net/management/v1/counter/{counter}/offline_conversions/upload"

COUNTER_VUZ = "98627983"
COUNTER_VSE = "96526110"
COUNTER_PROVUZ = "95348914"


def _counter_for_land(land) -> str | None:
    """Счётчик Метрики по ленду (столбец «Ленд»). Подстрочное сопоставление —
    устойчивее строгого map_crm_land (значения вида vuz / vsekolledzhi_postupi /
    provuz_postupi). provuz проверяем ДО vuz (provuz содержит vuz)."""
    s = str(land or "").strip().lower()
    if not s:
        return None
    if "provuz" in s:
        return COUNTER_PROVUZ
    if "vsekolled" in s or s == "vse":
        return COUNTER_VSE
    if "vuz" in s:
        return COUNTER_VUZ
    return None

GOAL_CONNECTION = "connection"
GOAL_DEAL = "deal"
GOAL_PAYMENT = "payment"


def _cell(row: List[Any], idx: int) -> Any:
    if idx == -1 or idx >= len(row):
        return ""
    return row[idx]


def _event_ts(date_val) -> int | None:
    """Unix-секунды (UTC) для даты события. Полдень МСК — чтобы быть после визита того же дня."""
    ms = to_datetime_ms(date_val)
    if ms is None:
        return None
    return ms // 1000 + 12 * 3600


def _load_payments_by_lead(service, sid) -> Dict[str, Dict[str, Any]]:
    """leadId → {ts, revenue} из листов «Оплаты» (orders==1)."""
    out: Dict[str, Dict[str, Any]] = {}
    for sheet in crm_payments_sheets():
        try:
            values = read_sheet(service, sid, sheet)
        except Exception as e:  # noqa: BLE001
            print(f"Метрика: пропуск оплат [{sheet}]: {e}")
            continue
        if len(values) < 2:
            continue
        h = [str(x) for x in values[0]]
        i_orders = pick_index_loose(h, ["orders", "оплат"])
        if len(h) > 18 and re_match_orders(h[18]):
            i_orders = 18
        i_rev = pick_index_loose(h, ["выручка", "revenue", "сумма", "оборот"])
        if len(h) > 17 and re_match_revenue(h[17]):
            i_rev = 17
        i_lead = pick_index_loose(h, ["id лида в scrm", "lead id", "id лида"])
        i_date = pick_index_loose(h, ["date pay", "дата оплаты"])
        if i_lead == -1:
            continue
        for row in values[1:]:
            lead = normalize_campaign_id(_cell(row, i_lead))
            if not lead:
                continue
            paid = (round(to_num(_cell(row, i_orders))) == 1) if i_orders != -1 else True
            if not paid:
                continue
            rev = to_num(_cell(row, i_rev)) if i_rev != -1 else 0.0
            ts = _event_ts(_cell(row, i_date)) if i_date != -1 else None
            cur = out.setdefault(lead, {"ts": ts, "revenue": 0.0})
            cur["revenue"] += rev
            if ts and (not cur["ts"] or ts > cur["ts"]):
                cur["ts"] = ts
    return out


Conversion = Tuple[str, str, str, int, float, str]  # counter, clientId, target, ts, price, leadId


def _collect_conversions(service, sid) -> List[Conversion]:
    pay = _load_payments_by_lead(service, sid)
    convs: List[Conversion] = []
    skipped_no_cid = 0
    skipped_land: Dict[str, int] = {}  # ленды с ClientID, но без счётчика — диагностика
    for sheet in crm_leads_sheets():
        try:
            values = read_sheet(service, sid, sheet)
        except Exception as e:  # noqa: BLE001
            print(f"Метрика: пропуск лидов [{sheet}]: {e}")
            continue
        if len(values) < 2:
            continue
        h = [str(x) for x in values[0]]
        i_cid = pick_index_loose(h, ["yandex client id", "client id", "clientid"])
        i_land = pick_index_loose(h, ["ленд", "land"])
        i_lead = pick_index_loose(h, ["id"])
        i_connect = pick_index_loose(h, ["connect", "количество соединений"])
        i_conn_date = pick_index_loose(h, ["б24 дата соединения", "дата соединения", "date connect"])
        i_deal = pick_index_loose(h, ["уникальные сделки", "сделка", "сделки", "deals", "deal"])
        i_created = pick_index_loose(h, ["date created", "дата создания"])
        if i_cid == -1 or i_land == -1:
            print(f"Метрика [{sheet}]: нет колонки ClientID/Ленд — пропуск листа")
            continue
        land_dist: Dict[str, int] = {}
        camp_sample = []
        for r in values[1:]:
            lv = str(_cell(r, i_land)).strip().lower() or "(пусто)"
            land_dist[lv] = land_dist.get(lv, 0) + 1
        i_camp = pick_index_loose(h, ["utm campaign", "utm_campaign", "campaign", "кампания"])
        camp_sample = [str(_cell(r, i_camp)).strip() for r in values[1:6]] if i_camp != -1 else []
        print(
            f"Метрика [{sheet}]: land col='{h[i_land] if i_land < len(h) else '?'}' (idx {i_land}); "
            f"распределение лендов (топ): {sorted(land_dist.items(), key=lambda x: -x[1])[:10]}; "
            f"utm_campaign idx={i_camp} сэмпл={camp_sample}"
        )
        for row in values[1:]:
            cid = str(_cell(row, i_cid)).strip()
            if not cid or cid == "0":
                skipped_no_cid += 1
                continue
            land = _cell(row, i_land)
            counter = _counter_for_land(land)
            if not counter:
                key = str(land).strip().lower()[:30] or "(пусто)"
                skipped_land[key] = skipped_land.get(key, 0) + 1
                continue
            lead = normalize_campaign_id(_cell(row, i_lead)) if i_lead != -1 else ""
            created_ts = _event_ts(_cell(row, i_created)) if i_created != -1 else None

            if i_connect != -1 and round(to_num(_cell(row, i_connect))) == 1:
                ts = _event_ts(_cell(row, i_conn_date)) if i_conn_date != -1 else None
                convs.append((counter, cid, GOAL_CONNECTION, ts or created_ts, 0.0, lead))
            if i_deal != -1 and round(to_num(_cell(row, i_deal))) == 1:
                convs.append((counter, cid, GOAL_DEAL, created_ts, 0.0, lead))
            pm = pay.get(lead) if lead else None
            if pm:
                convs.append((counter, cid, GOAL_PAYMENT, pm["ts"] or created_ts, pm["revenue"], lead))

    convs = [c for c in convs if c[3]]  # без валидного ts не грузим
    by_counter_cnt: Dict[str, int] = {}
    by_target_cnt: Dict[str, int] = {}
    for c in convs:
        by_counter_cnt[c[0]] = by_counter_cnt.get(c[0], 0) + 1
        by_target_cnt[c[2]] = by_target_cnt.get(c[2], 0) + 1
    print(f"Метрика: собрано {len(convs)} конверсий, пропущено {skipped_no_cid} лидов без ClientID")
    print(f"  по целям: {by_target_cnt}")
    print(f"  по счётчикам: {by_counter_cnt}")
    if skipped_land:
        top = sorted(skipped_land.items(), key=lambda x: -x[1])[:8]
        print(f"  ленды без счётчика (с ClientID): {top}")
    return convs


def _upload(counter: str, rows: List[Tuple[str, str, int, float]], token: str, dry_run: bool) -> bool:
    """rows: [(clientId, target, ts, price)]."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ClientId", "Target", "DateTime", "Price", "Currency"])
    for cid, target, ts, price in rows:
        w.writerow([cid, target, ts, (f"{price:.2f}" if price else ""), ("RUB" if price else "")])
    data = buf.getvalue().encode("utf-8")
    if dry_run:
        sample = rows[0] if rows else None
        print(f"  [DRY] счётчик {counter}: {len(rows)} строк, пример: {sample}")
        return True
    url = API_URL.format(counter=counter) + "?client_id_type=CLIENT_ID"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"OAuth {token}"},
            files={"file": ("conversions.csv", data, "text/csv")},
            timeout=120,
        )
    except requests.exceptions.RequestException as e:
        print(f"  счётчик {counter}: сетевая ошибка: {e}")
        return False
    if resp.status_code in (200, 201):
        print(f"  счётчик {counter}: принято {len(rows)} строк (HTTP {resp.status_code})")
        return True
    print(f"  счётчик {counter}: ОШИБКА {resp.status_code}: {resp.text[:400]}")
    return False


def sync_metrika_offline() -> None:
    token = os.environ.get("YM_TOKEN", "").strip()
    if not token:
        print("Метрика офлайн: YM_TOKEN не задан — пропуск")
        return
    dry_run = os.environ.get("METRIKA_DRY_RUN", "").strip().lower() in ("1", "true", "yes")

    service = get_sheets_service()
    sid = os.environ["GOOGLE_SHEETS_ID"]
    from sync.db import load_uploaded_conversion_keys, record_uploaded_conversions

    convs = _collect_conversions(service, sid)
    uploaded = load_uploaded_conversion_keys()
    new = [c for c in convs if (c[0], c[1], c[2]) not in uploaded]
    print(f"Метрика офлайн: новых {len(new)} из {len(convs)} (дедуп {len(convs) - len(new)}), dry_run={dry_run}")
    if not new:
        return

    by_counter: Dict[str, List[Conversion]] = {}
    for c in new:
        by_counter.setdefault(c[0], []).append(c)

    recorded: List[Tuple[str, str, str, int]] = []
    for counter, items in by_counter.items():
        rows = [(c[1], c[2], c[3], c[4]) for c in items]
        if _upload(counter, rows, token, dry_run) and not dry_run:
            recorded.extend((c[0], c[1], c[2], c[3]) for c in items)
    if recorded:
        record_uploaded_conversions(recorded)
        print(f"Метрика офлайн: записано в журнал {len(recorded)}")
