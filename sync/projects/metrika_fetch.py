"""Запросы stat API Метрики для общего синка проектов: трафик, цели, покупки, товары."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from sync.bjorn.common import (
    dim_text,
    derive_source_detail,
    expand_metrica_dimension,
    metrica_stat_rows,
    money,
    normalize_campaign_id,
)

ATTRIBUTION = "automatic"
GOALS_PER_REQUEST = 10

# Имена и порядок — как в sync/meshnflesh_metrika.py (fetch_metrica_sources) плюс DirectClickOrder.
_SOURCE_DIMENSIONS = [
    "ym:s:<attribution>TrafficSource",
    "ym:s:<attribution>SourceEngineName",
    "ym:s:<attribution>UTMSource",
    "ym:s:<attribution>UTMMedium",
    "ym:s:<attribution>UTMCampaign",
    "ym:s:<attribution>DirectClickOrder",
]


@dataclass(frozen=True)
class TrafficRow:
    date: str
    traffic_source: str
    source_detail: str
    utm_source: str
    utm_medium: str
    utm_campaign: str
    direct_campaign_id: str
    visits: int
    bounce_rate: float
    page_depth: float


@dataclass(frozen=True)
class GoalRow:
    date: str
    goal_id: int
    traffic_source: str
    source_detail: str
    utm_source: str
    utm_medium: str
    utm_campaign: str
    direct_campaign_id: str
    reaches: int


@dataclass(frozen=True)
class PurchaseRow:
    order_id: str
    purchase_date: str
    traffic_source: str
    source_detail: str
    utm_source: str
    utm_medium: str
    utm_campaign: str
    direct_campaign_id: str
    purchases: int
    revenue: float


@dataclass(frozen=True)
class ProductRow:
    date: str
    product_id: str
    product_name: str
    quantity: int
    revenue: float


def _dims(leading: list[str]) -> str:
    return ",".join(expand_metrica_dimension(d, ATTRIBUTION) for d in [*leading, *_SOURCE_DIMENSIONS])


def build_params(
    counter: int, date_from: str, date_to: str, *, kind: str, goal_ids: list[int] | None = None
) -> dict[str, Any]:
    """Параметры запроса stat API; kind: traffic | goals | purchases | products."""
    if kind == "traffic":
        dimensions = _dims(["ym:s:date"])
        metrics = "ym:s:visits,ym:s:bounceRate,ym:s:pageDepth"
    elif kind == "goals":
        dimensions = _dims(["ym:s:date"])
        metrics = ",".join(f"ym:s:goal{g}reaches" for g in goal_ids or [])
    elif kind == "purchases":
        # clientID в измерениях ломает запросы (sync/bjorn/sync_purchases.py:35)
        dimensions = _dims(["ym:s:date", "ym:s:purchaseID"])
        metrics = "ym:s:ecommercePurchases,ym:s:ecommerceRevenue"
    elif kind == "products":
        dimensions = "ym:s:date,ym:s:productID,ym:s:productName"
        metrics = "ym:s:productPurchasedQuantity,ym:s:productPurchasedPrice"
    else:
        raise ValueError(f"неизвестный kind: {kind}")
    params: dict[str, Any] = {
        "ids": counter,
        "metrics": metrics,
        "dimensions": dimensions,
        "date1": date_from,
        "date2": date_to,
        "attribution": ATTRIBUTION,
        "accuracy": "full",
        "proposed_accuracy": "false",
        "lang": "ru",
    }
    if kind == "purchases":
        params["filters"] = "ym:s:ecommercePurchases>0"
    return params


def _name(dim: dict[str, Any]) -> str:
    return dim_text((dim or {}).get("name"))


def _direct_campaign_id(direct: dict[str, Any]) -> str:
    """Номер кампании Директа из DirectClickOrder {id, owner, name, direct_id}.

    direct_id ("N-<номер>") совпадает с utm_campaign, куда макрос {campaign_id} кладёт настоящий
    CampaignId, поэтому по нему идёт джойн с project_direct_daily. id — внутренний номер заказа
    Метрики и лишь иногда равен CampaignId (живая проба 17.10: id 218498117 при direct_id N-118498117).
    """
    direct_part = dim_text(direct.get("direct_id"))
    if direct_part.startswith("N-"):
        direct_part = direct_part[2:]
    if direct_part.isdigit():
        return direct_part
    element_id = dim_text(direct.get("id"))
    if element_id.isdigit():
        return element_id
    return normalize_campaign_id(_name(direct))


def _source_dims(dims: list[dict[str, Any]]) -> tuple[str, str, str, str, str, str]:
    """dims: TrafficSource, SourceEngineName, UTMSource, UTMMedium, UTMCampaign, DirectClickOrder."""
    traffic_source = _name(dims[0])
    utm_source = _name(dims[2])
    direct_id = _direct_campaign_id(dims[5] or {})
    return (
        traffic_source,
        derive_source_detail(_name(dims[1]), utm_source, traffic_source),
        utm_source,
        _name(dims[3]),
        normalize_campaign_id(_name(dims[4])),
        direct_id,
    )


def _count(value: Any) -> int:
    return int(round(float(value or 0)))


def parse_traffic(data: list[dict[str, Any]]) -> list[TrafficRow]:
    # После нормализации (utm_campaign, "None") разные строки Метрики могут слиться в один ключ PK
    merged: dict[tuple, list[float]] = {}
    for rec in data:
        dims = rec["dimensions"]
        key = (_name(dims[0]), *_source_dims(dims[1:7]))
        visits, bounce, depth = (float(m or 0) for m in rec["metrics"][:3])
        if visits <= 0:
            continue
        acc = merged.setdefault(key, [0.0, 0.0, 0.0])
        acc[0] += visits
        acc[1] += bounce * visits
        acc[2] += depth * visits
    rows = []
    for key, (visits, bounce_sum, depth_sum) in merged.items():
        weight = visits
        rows.append(
            TrafficRow(*key, int(round(visits)), round(bounce_sum / weight, 2), round(depth_sum / weight, 3))
        )
    return rows


def parse_goals(data: list[dict[str, Any]], goal_ids: list[int]) -> list[GoalRow]:
    merged: dict[tuple, int] = {}
    for rec in data:
        dims = rec["dimensions"]
        date = _name(dims[0])
        source = _source_dims(dims[1:7])
        for goal_id, value in zip(goal_ids, rec["metrics"]):
            reaches = _count(value)
            if reaches == 0:
                continue
            key = (date, int(goal_id), *source)
            merged[key] = merged.get(key, 0) + reaches
    return [GoalRow(*key, reaches) for key, reaches in merged.items()]


def parse_purchases(data: list[dict[str, Any]]) -> list[PurchaseRow]:
    # Покупка = (order_id, дата): тот же purchaseID в другой день — отдельная покупка, как в отчёте Метрики.
    # Строки с одинаковым ключом суммируются, измерения источника берутся от первой.
    merged: dict[tuple[str, str], PurchaseRow] = {}
    for rec in data:
        dims = rec["dimensions"]
        order_id = _name(dims[1])
        if not order_id:
            continue
        purchases, revenue = rec["metrics"][:2]
        row = PurchaseRow(order_id, _name(dims[0]), *_source_dims(dims[2:8]), _count(purchases), money(revenue))
        key = (order_id, row.purchase_date)
        prev = merged.get(key)
        if prev is None:
            merged[key] = row
        else:
            merged[key] = replace(
                prev, purchases=prev.purchases + row.purchases, revenue=money(prev.revenue + row.revenue)
            )
    return list(merged.values())


def parse_products(data: list[dict[str, Any]]) -> list[ProductRow]:
    merged: dict[tuple, list[float]] = {}
    for rec in data:
        dims = rec["dimensions"]
        quantity, revenue = (float(m or 0) for m in rec["metrics"][:2])
        if quantity == 0 and revenue == 0:
            continue
        key = (_name(dims[0]), _name(dims[1]), _name(dims[2]))
        acc = merged.setdefault(key, [0.0, 0.0])
        acc[0] += quantity
        acc[1] += revenue
    return [ProductRow(*key, int(round(q)), money(r)) for key, (q, r) in merged.items()]


def fetch_traffic(counter: int, token: str, date_from: str, date_to: str) -> list[TrafficRow]:
    params = build_params(counter, date_from, date_to, kind="traffic")
    return parse_traffic(metrica_stat_rows(params, token))


def fetch_goals(counter: int, token: str, date_from: str, date_to: str, goal_ids: list[int]) -> list[GoalRow]:
    goal_ids = list(dict.fromkeys(goal_ids))
    rows: list[GoalRow] = []
    for i in range(0, len(goal_ids), GOALS_PER_REQUEST):
        batch = goal_ids[i : i + GOALS_PER_REQUEST]
        params = build_params(counter, date_from, date_to, kind="goals", goal_ids=batch)
        rows.extend(parse_goals(metrica_stat_rows(params, token), batch))
    return rows


def fetch_purchases(counter: int, token: str, date_from: str, date_to: str) -> list[PurchaseRow]:
    params = build_params(counter, date_from, date_to, kind="purchases")
    return parse_purchases(metrica_stat_rows(params, token))


def fetch_products(counter: int, token: str, date_from: str, date_to: str) -> list[ProductRow]:
    params = build_params(counter, date_from, date_to, kind="products")
    return parse_products(metrica_stat_rows(params, token))
