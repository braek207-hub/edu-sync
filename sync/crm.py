import os
import re
from datetime import date as _date
from typing import Any, Dict, List, Tuple

from sync.classify import (
    detect_direction,
    map_crm_land,
    normalize_b24_dim,
    normalize_city_ip_segment,
    resolve_row_project,
)
from sync.sheets import get_sheets_service, read_sheet
from sync.utils import normalize_campaign_id, pick_index_loose, to_iso_date, to_num

CRM_LEADS_SHEET = "Лиды"
CRM_PAYMENTS_SHEET = "Оплаты"


def _sheet_names(env_key: str, default: str) -> List[str]:
    raw = os.environ.get(env_key, default)
    return [s.strip() for s in raw.split(",") if s.strip()]


def crm_leads_sheets() -> List[str]:
    return _sheet_names("CRM_LEADS_SHEETS", "Лиды,Лиды 2025")


def crm_payments_sheets() -> List[str]:
    return _sheet_names("CRM_PAYMENTS_SHEETS", "Оплаты,Оплаты 2025")


Dims = Tuple[str, str, str]


def _cell(row: List[Any], idx: int) -> Any:
    if idx == -1 or idx >= len(row):
        return ""
    return row[idx]


def _apply_meta(bucket: Dict[str, Any], raw_campaign: Any, land: str = "") -> None:
    name = str(raw_campaign or "").strip()
    if name and not bucket.get("campaign_name"):
        bucket["campaign_name"] = name
        bucket["direction"] = detect_direction(name)
    if land and bucket.get("project") == "unknown":
        bucket["project"] = map_crm_land(land)


def _log_spreadsheet_tabs(service, spreadsheet_id: str) -> None:
    meta = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
        .execute()
    )
    titles = [
        s["properties"]["title"]
        for s in meta.get("sheets", [])
        if s.get("properties", {}).get("title")
    ]
    print(f"CRM: листы в книге ({len(titles)}): {', '.join(titles[:20])}")


def _payment_flag(raw: Any) -> int:
    p_num = to_num(raw)
    if round(p_num) == 1:
        return 1
    s = str(raw or "").strip().lower()
    if s in ("1", "1.0", "1,0", "true", "да"):
        return 1
    return 0


_JUNK_STATUSES = ["дубл", "спам", "тест", "ошибк", "повтор"]


def _normalize_audience(raw: Any) -> str:
    """Колонка «Родитель» — флаг Да/Нет: Да → родитель, Нет → школьник.
    Поддержан и прямой текст (Родитель/Школьник/Ученик) на случай смены формата."""
    s = str(raw or "").strip().lower()
    if not s:
        return "unknown"
    if s in ("да", "yes", "true", "1", "1.0") or "родител" in s:
        return "parent"
    if s in ("нет", "no", "false", "0", "0.0") or "школьник" in s or "ученик" in s:
        return "pupil"
    return "unknown"


def _is_junk_status(raw: Any) -> bool:
    s = str(raw or "").strip().lower()
    return any(sub in s for sub in _JUNK_STATUSES)


def _sync_leads_raw(
    headers: List[str],
    values: List[List[Any]],
    paid_by_lead_id: Dict[str, Dict[str, Any]] | None = None,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, "Dims"]]:
    """Построчные лиды с сегментами — как GAS readCrmRawFromVals_.

    payments_from_leads:
      1) если в листе «Лиды» есть колонка «Оплата»/«payment» — читаем флаг из неё
         (поле payment_flag: "1"/"да" → 1, иначе 0);
      2) если передан paid_by_lead_id — join по leadId из листа «Оплаты» (orders==1);
      при наличии обоих источников суммируем (обычно одно из двух).

    eff_leads: все лиды кроме junk-статусов (дубль/спам/тест/ошибка/повтор).
    audience: «Родитель» → "parent", «Школьник»/«Ученик» → "pupil", иначе "unknown".
    days_to_pay_sum / days_to_pay_count: сумма/счётчик дней от создания до оплаты.
    """
    li = {
        "date": pick_index_loose(
            headers, ["date created", "дата создания", "дата", "date"]
        ),
        "campaign": pick_index_loose(
            headers,
            [
                "utm campaign",
                "utm_campaign",
                "utmcampaign",
                "campaign",
                "кампания",
            ],
        ),
        "land": pick_index_loose(headers, ["ленд", "land"]),
        "connections": pick_index_loose(
            headers, ["connect", "количество соединений"]
        ),
        "date_connect": pick_index_loose(
            headers,
            [
                "б24 дата соединения",
                "дата соединения",
                "date connect",
                "connect date",
                "дата коннекта",
                "дата дозвона",
            ],
        ),
        "deals": pick_index_loose(
            headers, ["уникальные сделки", "сделка", "сделки", "deals", "deal"]
        ),
        "id": pick_index_loose(headers, ["id"]),
        "city_ip": pick_index_loose(
            headers, ["город (ip)", "город(ip)", "город ip"]
        ),
        "grad_year": pick_index_loose(
            headers, ["б24 год выпуска", "год выпуска"]
        ),
        "edu_level": pick_index_loose(
            headers,
            ["б24 уровень образования", "уровень образования", "класс/курс"],
        ),
        "payment_flag": pick_index_loose(headers, ["оплата", "payment", "payments"]),
        "status": pick_index_loose(headers, ["этап", "статус", "stage"]),
        "audience": pick_index_loose(headers, ["родитель", "школьник", "аудитория"]),
    }
    if li["date"] == -1:
        raise ValueError("CRM(Лиды): не найдена колонка даты")
    if li["campaign"] == -1:
        raise ValueError("CRM(Лиды): не найдена колонка utm campaign")

    agg: Dict[str, Dict[str, Any]] = {}
    lead_dims_by_id: Dict[str, Dims] = {}

    for row in values[1:]:
        date_iso = to_iso_date(_cell(row, li["date"]))
        if not date_iso:
            continue
        cid = normalize_campaign_id(_cell(row, li["campaign"]))
        land = (
            str(_cell(row, li["land"])).strip().lower() if li["land"] != -1 else ""
        )
        if not cid and land:
            cid = f"land:{land}"
        if not cid:
            continue

        city = (
            normalize_city_ip_segment(_cell(row, li["city_ip"]))
            if li["city_ip"] != -1
            else "rf"
        )
        grad = (
            normalize_b24_dim(_cell(row, li["grad_year"]))
            if li["grad_year"] != -1
            else "unknown"
        )
        edu = (
            normalize_b24_dim(_cell(row, li["edu_level"]))
            if li["edu_level"] != -1
            else "unknown"
        )
        lead_id = (
            normalize_campaign_id(_cell(row, li["id"])) if li["id"] != -1 else ""
        )
        if lead_id:
            lead_dims_by_id[lead_id] = (city, grad, edu)

        # audience — часть ключа сегмента
        aud = (
            _normalize_audience(_cell(row, li["audience"]))
            if li["audience"] != -1
            else "unknown"
        )

        key = f"{date_iso}|{cid}|{city}|{grad}|{edu}|{aud}"
        bucket = agg.setdefault(
            key,
            {
                "date": date_iso,
                "campaign_id": cid,
                "city_ip_segment": city,
                "b24_grad_year": grad,
                "b24_edu_level": edu,
                "audience": aud,
                "leads": 0,
                "eff_leads": 0,
                "connections": 0.0,
                "deals": 0.0,
                "payments_from_leads": 0,
                "revenue_from_leads": 0.0,
                "days_to_pay_sum": 0.0,
                "days_to_pay_count": 0,
                "project": "unknown",
                "direction": "other",
                "campaign_name": "",
                "land": land,
            },
        )
        _apply_meta(bucket, _cell(row, li["campaign"]), land)
        if land:
            bucket["project"] = resolve_row_project(None, bucket.get("campaign_name", ""), land)

        bucket["leads"] += 1

        # eff_leads: не считаем junk-статусы
        if li["status"] == -1 or not _is_junk_status(_cell(row, li["status"])):
            bucket["eff_leads"] += 1

        if li["connections"] != -1:
            bucket["connections"] += to_num(_cell(row, li["connections"]))
        elif li["date_connect"] != -1:
            if str(_cell(row, li["date_connect"]) or "").strip() not in ("", "0"):
                bucket["connections"] += 1
        if li["deals"] != -1:
            bucket["deals"] += to_num(_cell(row, li["deals"]))

        # payments_from_leads: join из «Оплаты» — авторитетный источник; колонка
        # «Оплата» в Лидах — только фолбэк, когда по leadId нет join-матча (иначе
        # был бы двойной счёт одной оплаты). days-to-pay считаем лишь по join'у.
        paid_via_join = False
        if paid_by_lead_id and lead_id:
            pm = paid_by_lead_id.get(lead_id)
            if pm:
                paid_via_join = True
                bucket["payments_from_leads"] += pm["count"]
                bucket["revenue_from_leads"] += pm["revenue"]
                # days-to-pay: earliest pay_date из paid_by_lead_id
                pay_date = pm.get("pay_date")
                if pay_date:
                    try:
                        days = (_date.fromisoformat(pay_date) - _date.fromisoformat(date_iso)).days
                        if days >= 0:
                            bucket["days_to_pay_sum"] += days
                            bucket["days_to_pay_count"] += 1
                    except (ValueError, TypeError):
                        pass
        if not paid_via_join and li["payment_flag"] != -1:
            bucket["payments_from_leads"] += _payment_flag(_cell(row, li["payment_flag"]))

    return agg, lead_dims_by_id


def _sync_leads_by_land(headers: List[str], values: List[List[Any]]) -> Dict[str, Dict[str, Any]]:
    li = {
        "date": pick_index_loose(headers, ["дата", "date"]),
        "land": pick_index_loose(headers, ["ленд", "land", "проект"]),
        "leads": pick_index_loose(headers, ["лиды", "leads"]),
        "connections": pick_index_loose(headers, ["соединен", "connect"]),
        "deals": pick_index_loose(headers, ["уникальные сделки", "сделки", "deals"]),
    }
    if li["date"] == -1 or li["land"] == -1 or li["leads"] == -1:
        return {}, {}

    agg: Dict[str, Dict[str, Any]] = {}
    for row in values[1:]:
        date_iso = to_iso_date(_cell(row, li["date"]))
        land = str(_cell(row, li["land"])).strip().lower()
        if not date_iso or not land:
            continue
        cid = f"land:{land}"
        key = f"{date_iso}|{cid}|rf|unknown|unknown|unknown"
        n_leads = int(to_num(_cell(row, li["leads"])))
        agg[key] = {
            "date": date_iso,
            "campaign_id": cid,
            "city_ip_segment": "rf",
            "b24_grad_year": "unknown",
            "b24_edu_level": "unknown",
            "audience": "unknown",
            "leads": n_leads,
            "eff_leads": n_leads,  # нет колонки статуса — все лиды считаем эффективными
            "connections": int(
                to_num(_cell(row, li["connections"])) if li["connections"] != -1 else 0
            ),
            "deals": int(to_num(_cell(row, li["deals"])) if li["deals"] != -1 else 0),
            "payments_from_leads": 0,
            "revenue_from_leads": 0.0,
            "days_to_pay_sum": 0.0,
            "days_to_pay_count": 0,
            "project": map_crm_land(land),
            "direction": detect_direction(land),
            "campaign_name": land,
            "land": land,
        }
    return agg, {}


def merge_leads_agg(
    target: Dict[str, Dict[str, Any]],
    source: Dict[str, Dict[str, Any]],
) -> None:
    """Суммирует leads/connections/deals/eff_leads/days_to_pay при совпадении segment key."""
    for key, src in source.items():
        if key not in target:
            target[key] = dict(src)
            continue
        dst = target[key]
        dst["leads"] += src.get("leads", 0)
        dst["eff_leads"] = dst.get("eff_leads", 0) + src.get("eff_leads", 0)
        dst["connections"] += src.get("connections", 0)
        dst["deals"] += src.get("deals", 0)
        dst["revenue_from_leads"] = dst.get("revenue_from_leads", 0.0) + src.get("revenue_from_leads", 0.0)
        dst["payments_from_leads"] += src.get("payments_from_leads", 0)
        dst["days_to_pay_sum"] = dst.get("days_to_pay_sum", 0.0) + src.get("days_to_pay_sum", 0.0)
        dst["days_to_pay_count"] = dst.get("days_to_pay_count", 0) + src.get("days_to_pay_count", 0)
        if not dst.get("campaign_name") and src.get("campaign_name"):
            dst["campaign_name"] = src["campaign_name"]
        if dst.get("project") == "unknown" and src.get("project") not in (
            None,
            "",
            "unknown",
        ):
            dst["project"] = src["project"]
        if dst.get("direction") == "other" and src.get("direction") not in (
            None,
            "",
            "other",
        ):
            dst["direction"] = src["direction"]


def merge_lead_dims(
    target: Dict[str, Dims], source: Dict[str, Dims]
) -> None:
    target.update(source)


def merge_payments_agg(
    target: Dict[str, Dict[str, Any]],
    source: Dict[str, Dict[str, Any]],
) -> None:
    for key, src in source.items():
        if key not in target:
            target[key] = dict(src)
            continue
        dst = target[key]
        dst["payments"] += src.get("payments", 0)
        dst["revenue"] += src.get("revenue", 0.0)
        if dst.get("project") == "unknown" and src.get("project") not in (
            None,
            "",
            "unknown",
        ):
            dst["project"] = src["project"]
        if dst.get("direction") == "other" and src.get("direction") not in (
            None,
            "",
            "other",
        ):
            dst["direction"] = src["direction"]


def _parse_leads_sheet(
    headers: List[str],
    values: List[List[Any]],
    sheet_name: str,
    paid_by_lead_id: Dict[str, Dict[str, float]] | None = None,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dims]]:
    print(f"CRM Лиды [{sheet_name}]: заголовки = {headers[:12]}")
    lead_dims_by_id: Dict[str, Dims] = {}
    agg: Dict[str, Dict[str, Any]] = {}
    try:
        agg, lead_dims_by_id = _sync_leads_raw(headers, values, paid_by_lead_id)
        if agg:
            print(f"CRM Лиды [{sheet_name}]: режим построчный (utm campaign + сегменты)")
            return agg, lead_dims_by_id
    except ValueError:
        pass

    agg, lead_dims_by_id = _sync_leads_by_land(headers, values)
    if agg:
        print(f"CRM Лиды [{sheet_name}]: режим свод по Ленд (Дата + Ленд + Лиды)")
    return agg, lead_dims_by_id


def _load_all_lead_dims(
    service, spreadsheet_id: str
) -> Dict[str, Dims]:
    lead_dims_by_id: Dict[str, Dims] = {}
    for sheet_name in crm_leads_sheets():
        try:
            values = read_sheet(service, spreadsheet_id, sheet_name)
        except Exception as e:
            print(f"CRM Лиды [{sheet_name}]: пропуск lead_dims — {e}")
            continue
        if len(values) < 2:
            continue
        try:
            _, dims = _sync_leads_raw([str(x) for x in values[0]], values)
            merge_lead_dims(lead_dims_by_id, dims)
        except Exception:
            pass
    return lead_dims_by_id


def _load_paid_by_lead_id(service, spreadsheet_id: str) -> Dict[str, Dict[str, Any]]:
    """leadId → {count, revenue, pay_date} из листов «Оплаты» (orders==1).

    Источник: лист «Оплаты», колонка orders (==1) + выручка + «ID лида в SCRM»
    + «date pay»/«дата оплаты» (берём самую раннюю дату для окна конверсии).
    Используется для атрибуции оплат, выручки и дней до оплаты к дате создания лида.
    """
    paid: Dict[str, Dict[str, Any]] = {}
    for sheet_name in crm_payments_sheets():
        try:
            values = read_sheet(service, spreadsheet_id, sheet_name)
        except Exception as e:
            print(f"CRM Оплаты [{sheet_name}]: пропуск paid_by_lead — {e}")
            continue
        if len(values) < 2:
            continue
        headers = [str(x) for x in values[0]]
        i_orders = pick_index_loose(headers, ["orders", "оплат"])
        if len(headers) > 18 and re_match_orders(headers[18]):
            i_orders = 18
        i_rev = pick_index_loose(headers, ["выручка", "revenue", "сумма", "оборот"])
        if len(headers) > 17 and re_match_revenue(headers[17]):
            i_rev = 17
        i_lead = pick_index_loose(headers, ["id лида в scrm", "lead id", "id лида"])
        if i_lead == -1:
            print(f"CRM Оплаты [{sheet_name}]: нет колонки ID лида — оплаты к лидам не привязать")
            continue
        i_pay_date = pick_index_loose(headers, ["date pay", "дата оплаты"])
        for row in values[1:]:
            lead_id = normalize_campaign_id(_cell(row, i_lead))
            if not lead_id:
                continue
            cnt = (1 if round(to_num(_cell(row, i_orders))) == 1 else 0) if i_orders != -1 else 1
            if not cnt:
                continue
            rev = to_num(_cell(row, i_rev)) if i_rev != -1 else 0.0
            pay_date_iso = to_iso_date(_cell(row, i_pay_date)) if i_pay_date != -1 else ""
            agg = paid.setdefault(lead_id, {"count": 0.0, "revenue": 0.0, "pay_date": None})
            agg["count"] += cnt
            agg["revenue"] += rev
            # Сохраняем самую раннюю дату оплаты для расчёта окна конверсии
            if pay_date_iso:
                if agg["pay_date"] is None or pay_date_iso < agg["pay_date"]:
                    agg["pay_date"] = pay_date_iso
    total_c = sum(p["count"] for p in paid.values())
    total_r = sum(p["revenue"] for p in paid.values())
    print(f"CRM Оплаты→лиды: {len(paid)} лидов с оплатами, оплат {int(total_c)}, выручка {total_r:.0f}")
    return paid


def _parse_payments_sheet(
    headers: List[str],
    values: List[List[Any]],
    lead_dims_by_id: Dict[str, Dims],
    sheet_name: str,
) -> Dict[str, Dict[str, Any]]:
    pi = {
        "pay_date": pick_index_loose(headers, ["date pay", "дата оплаты"]),
        "campaign": pick_index_loose(
            headers, ["кампания", "utm campaign", "utm_campaign", "utm_campaign"]
        ),
        "revenue": pick_index_loose(headers, ["выручка", "revenue", "сумма", "оборот"]),
        "orders": pick_index_loose(headers, ["orders", "оплат"]),
        "land": pick_index_loose(headers, ["ленд", "land"]),
        "lead_id": pick_index_loose(
            headers, ["id лида в scrm", "lead id", "id лида"]
        ),
    }
    if len(headers) > 17 and re_match_revenue(headers[17]):
        pi["revenue"] = 17
    if len(headers) > 18 and re_match_orders(headers[18]):
        pi["orders"] = 18

    if pi["pay_date"] == -1:
        raise ValueError(f'CRM(Оплаты [{sheet_name}]): не найдена колонка "date pay"')
    if pi["campaign"] == -1:
        raise ValueError(f"CRM(Оплаты [{sheet_name}]): не найдена колонка campaign")
    if pi["revenue"] == -1:
        raise ValueError(f'CRM(Оплаты [{sheet_name}]): не найдена колонка "Выручка"')

    agg: Dict[str, Dict[str, Any]] = {}

    for row in values[1:]:
        date_iso = to_iso_date(_cell(row, pi["pay_date"]))
        if not date_iso:
            continue
        cid = normalize_campaign_id(_cell(row, pi["campaign"]))
        if not cid:
            continue
        land = (
            str(_cell(row, pi["land"])).strip().lower() if pi["land"] != -1 else ""
        )
        lead_id = (
            normalize_campaign_id(_cell(row, pi["lead_id"]))
            if pi["lead_id"] != -1
            else ""
        )
        if lead_id and lead_id in lead_dims_by_id:
            city, grad, edu = lead_dims_by_id[lead_id]
        else:
            city, grad, edu = "rf", "unknown", "unknown"

        key = f"{date_iso}|{cid}|{city}|{grad}|{edu}"
        bucket = agg.setdefault(
            key,
            {
                "date": date_iso,
                "campaign_id": cid,
                "city_ip_segment": city,
                "b24_grad_year": grad,
                "b24_edu_level": edu,
                "payments": 0,
                "revenue": 0.0,
                "project": "unknown",
                "direction": "other",
            },
        )
        _apply_meta(bucket, _cell(row, pi["campaign"]), land)
        if land:
            bucket["project"] = resolve_row_project(
                None, str(_cell(row, pi["campaign"])), land
            )

        if pi["orders"] != -1:
            p_num = to_num(_cell(row, pi["orders"]))
            bucket["payments"] += 1 if round(p_num) == 1 else 0
        else:
            bucket["payments"] += 1
        bucket["revenue"] += to_num(_cell(row, pi["revenue"]))

    print(f"CRM Оплаты [{sheet_name}]: {len(agg)} сегментированных строк")
    return agg


def _enrich_project_from_direct(agg: Dict[str, Dict[str, Any]]) -> None:
    try:
        from sync.crm_lite import _meta_from_direct_stats

        meta = _meta_from_direct_stats()
        for bucket in agg.values():
            m = meta.get(bucket["campaign_id"])
            if not m:
                continue
            if m.get("project") and m["project"] != "unknown":
                bucket["project"] = m["project"]
            if m.get("direction") and m["direction"] != "other":
                bucket["direction"] = m["direction"]
            if m.get("campaign_name"):
                bucket["campaign_name"] = m["campaign_name"]
    except Exception as e:
        print(f"CRM Лиды: meta Direct пропущена: {e}")


def sync_crm_leads() -> int:
    service = get_sheets_service()
    spreadsheet_id = os.environ["GOOGLE_SHEETS_ID"]

    sheet_names = crm_leads_sheets()
    print(f"CRM Лиды: листы = {sheet_names}")

    # Признак оплаты лида берём из листа «Оплаты» (orders==1) и приписываем к дате
    # создания лида по leadId — в самом листе «Лиды» колонки оплаты нет.
    paid_by_lead_id = _load_paid_by_lead_id(service, spreadsheet_id)

    lead_dims_by_id: Dict[str, Dims] = {}
    agg: Dict[str, Dict[str, Any]] = {}

    for sheet_name in sheet_names:
        try:
            values = read_sheet(service, spreadsheet_id, sheet_name)
        except Exception as e:
            print(f"CRM Лиды [{sheet_name}]: ошибка чтения — {e}")
            continue
        if len(values) < 2:
            print(f"CRM Лиды [{sheet_name}]: пустой лист или только заголовок")
            continue
        headers = [str(x) for x in values[0]]
        sheet_agg, sheet_dims = _parse_leads_sheet(headers, values, sheet_name, paid_by_lead_id)
        if not sheet_agg:
            continue
        merge_leads_agg(agg, sheet_agg)
        merge_lead_dims(lead_dims_by_id, sheet_dims)

    if not agg:
        _log_spreadsheet_tabs(service, spreadsheet_id)
        raise ValueError(f"CRM(Лиды): нет строк после разбора листов {sheet_names}")

    _enrich_project_from_direct(agg)

    rows = []
    for v in agg.values():
        rows.append(
            {
                "date": v["date"],
                "campaign_id": v["campaign_id"],
                "project": v["project"],
                "direction": v["direction"],
                "city_ip_segment": v["city_ip_segment"],
                "b24_grad_year": v["b24_grad_year"],
                "b24_edu_level": v["b24_edu_level"],
                "audience": v.get("audience", "unknown"),
                "leads": int(v["leads"]),
                "eff_leads": int(v.get("eff_leads", v["leads"])),
                "connections": int(v["connections"]),
                "deals": int(v["deals"]),
                "payments_from_leads": int(v.get("payments_from_leads", 0)),
                "revenue_from_leads": float(v.get("revenue_from_leads", 0.0)),
                "days_to_pay_sum": float(v.get("days_to_pay_sum", 0.0)),
                "days_to_pay_count": int(v.get("days_to_pay_count", 0)),
            }
        )

    print(f"CRM Лиды: {len(rows)} сегментированных строк")
    from sync.db import replace_crm_leads

    n = replace_crm_leads(rows)
    print(f"CRM Лиды: записано {n} строк в crm_leads")
    return n


def sync_crm_payments() -> int:
    service = get_sheets_service()
    spreadsheet_id = os.environ["GOOGLE_SHEETS_ID"]

    sheet_names = crm_payments_sheets()
    print(f"CRM Оплаты: листы = {sheet_names}")

    lead_dims_by_id = _load_all_lead_dims(service, spreadsheet_id)
    agg: Dict[str, Dict[str, Any]] = {}

    for sheet_name in sheet_names:
        try:
            values = read_sheet(service, spreadsheet_id, sheet_name)
        except Exception as e:
            print(f"CRM Оплаты [{sheet_name}]: ошибка чтения — {e}")
            continue
        if len(values) < 2:
            print(f"CRM Оплаты [{sheet_name}]: пустой лист")
            continue
        headers = [str(x) for x in values[0]]
        try:
            sheet_agg = _parse_payments_sheet(
                headers, values, lead_dims_by_id, sheet_name
            )
        except ValueError as e:
            print(f"CRM Оплаты [{sheet_name}]: {e}")
            continue
        merge_payments_agg(agg, sheet_agg)

    if not agg:
        print("CRM Оплаты: нет строк после разбора всех листов")
        return 0

    rows = [
        {
            "date": v["date"],
            "campaign_id": v["campaign_id"],
            "project": v["project"],
            "direction": v["direction"],
            "city_ip_segment": v["city_ip_segment"],
            "b24_grad_year": v["b24_grad_year"],
            "b24_edu_level": v["b24_edu_level"],
            "payments": int(v["payments"]),
            "revenue": round(float(v["revenue"]), 2),
        }
        for v in agg.values()
    ]

    print(f"CRM Оплаты: {len(rows)} сегментированных строк")
    from sync.db import replace_crm_payments

    n = replace_crm_payments(rows)
    print(f"CRM Оплаты: записано {n} строк в crm_payments")
    return n


def re_match_revenue(header: str) -> bool:
    return bool(re.search(r"выруч|revenue|сумма", str(header), re.I))


def re_match_orders(header: str) -> bool:
    return bool(re.search(r"orders|оплат", str(header), re.I))
