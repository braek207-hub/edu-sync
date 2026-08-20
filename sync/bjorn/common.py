from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable
from urllib.parse import urlencode

import requests

# "<логин кабинета>_yd_...|<номер>|11:<id> → <id>
_EPROMO_CAMPAIGN_RE = re.compile(r'\|\d+:(\d+)$')
# "<id>-<имя кампании>" → <id>
_ID_PREFIX_RE = re.compile(r'^(\d{6,})[-–—\s]')


DATE_FMT = "%Y-%m-%d"
NOT_SET_CAMPAIGN_ID = "not_set"
NOT_SET_CAMPAIGN_NAME = "Не определено"
AD_SOURCE_NAME = "Переходы по рекламе"


def env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def env_optional(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value.strip()


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value, DATE_FMT).date()


def yesterday_utc() -> date:
    return datetime.utcnow().date() - timedelta(days=1)


def add_date_range_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="date_from", default="")
    parser.add_argument("--to", dest="date_to", default="")
    parser.add_argument(
        "--rewrite-days",
        type=int,
        default=int(env_optional("REWRITE_DAYS", "7")),
        help="Used when --from is not passed. Rewrites recent completed days.",
    )


def resolve_date_range(args: argparse.Namespace) -> tuple[str, str]:
    if args.date_from or args.date_to:
        if not args.date_from or not args.date_to:
            raise RuntimeError("Pass both --from and --to, or neither.")
        start = parse_iso_date(args.date_from)
        end = parse_iso_date(args.date_to)
    else:
        end = yesterday_utc()
        start = end - timedelta(days=max(args.rewrite_days - 1, 0))

    if start > end:
        raise RuntimeError(f"Invalid date range: {start} > {end}")
    return start.isoformat(), end.isoformat()


def clean_text(value: Any) -> str:
    return str(value or "").strip().lstrip("'").strip()


def parse_int(value: Any) -> int:
    text = clean_text(value).replace(" ", "").replace(",", ".")
    if not text or text in {"--", "null", "None"}:
        return 0
    return int(float(text))


def parse_money(value: Any) -> float:
    text = clean_text(value).replace(" ", "").replace(",", ".")
    if not text or text in {"--", "null", "None"}:
        return 0.0
    return float(text)


def parse_optional_float(value: Any) -> float | None:
    text = clean_text(value).replace(" ", "").replace(",", ".")
    if not text or text in {"--", "null", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def expand_metrica_dimension(template: str, attribution: str) -> str:
    return template.replace("<attribution>", attribution)


def money(value: Any) -> float:
    dec = Decimal(str(parse_money(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(dec)


def normalize_source(source: str) -> str:
    text = clean_text(source)
    return text or "Не определено"


# UTM source → display label, used as fallback when SourceEngine is null.
# Covers cases SourceEngine doesn't catch (email/CRM, social ads, etc.)
_UTM_SOURCE_LABELS: dict[str, str] = {
    # Яндекс.Директ — all cabinet/tagging variants
    "yandex.direct":         "Яндекс.Директ",
    "yandex.direct.epromo":  "Яндекс.Директ",
    "ya.direct":             "Яндекс.Директ",
    "ya.direct.epromo":      "Яндекс.Директ",
    "ya":                    "Яндекс.Директ",
    "rush_yandex":           "Яндекс.Директ",
    # Яндекс (organic/untagged)
    "yandex":                "Яндекс",
    "yandexsmartcamera":     "Яндекс",
    # Google
    "google":                "Google",
    # VK
    "vk":                    "ВКонтакте",
    "vkontakte":             "ВКонтакте",
    "vk_ads":                "VK Реклама",
    "clb_vk_ads":            "VK Реклама",
    # Instagram
    "ig":                    "Instagram",
    "inst":                  "Instagram",
    "insta":                 "Instagram",
    "instagram":             "Instagram",
    # Facebook
    "fb":                    "Facebook",
    "facebook":              "Facebook",
    # Telegram
    "tg":                    "Telegram",
    "telegram":              "Telegram",
    # Others
    "ok":                    "Одноклассники",
    "tiktok":                "TikTok",
    "youtube":               "YouTube",
    "yt":                    "YouTube",
    "mindbox":               "Mindbox",
    "perplexity":            "Perplexity",
    "chatgpt.com":           "ChatGPT",
    "offline":               "Offline",
}

# For paid traffic: social network names from SourceEngine → ad platform names
_PAID_SOCIAL_UPGRADE: dict[str, str] = {
    "ВКонтакте":     "VK Реклама",
    "instagram.com": "Instagram Реклама",
    "Facebook":      "Facebook Реклама",
    "Одноклассники": "Одноклассники Реклама",
}

# Normalize Metrica SourceEngine values to clean display labels
_SOURCE_ENGINE_NORMALIZE: dict[str, str] = {
    "яндекс: директ":               "Яндекс.Директ",
    "яндекс.директ: не определено":  "Яндекс.Директ",
    "другая социальная сеть: определено по меткам": "Другая соцсеть",
    # "Другая реклама: определено по меткам" is handled in derive_source_detail
    # because UTMSource can identify the actual ad system (e.g. yandex.direct).
}

# UTM sources that confirm Yandex Direct, used to fix "Другая реклама" classification
_YANDEX_DIRECT_UTM: frozenset[str] = frozenset({
    "yandex.direct", "yandex.direct.epromo",
    "ya.direct", "ya.direct.epromo", "ya", "rush_yandex",
})


def normalize_source_engine(value: str) -> str:
    """Normalize ym:s:<attr>SourceEngine values to clean display labels."""
    v = clean_text(value)
    if not v:
        return ""
    return _SOURCE_ENGINE_NORMALIZE.get(v.lower(), v)


def derive_source_detail(source_engine_raw: str, utm_source_raw: str, source: str = "") -> str:
    """Determine source_detail.

    Priority:
      1. Metrica SourceEngine — covers all traffic types natively.
         Exception: "Другая реклама: определено по меткам" → try UTMSource to identify ad system.
      2. UTM source fallback — only for non-direct traffic (email/CRM like Mindbox).
         Direct traffic (Прямые заходы) keeps empty source_detail.
    """
    engine_raw_lo = clean_text(source_engine_raw).lower()

    # "Другая реклама: определено по меткам" = Metrica knows it's paid by UTM tags
    # but didn't identify the ad system. Use UTMSource to resolve.
    if engine_raw_lo == "другая реклама: определено по меткам":
        utm = clean_text(utm_source_raw).lower()
        if utm in _YANDEX_DIRECT_UTM or utm.startswith("yandex"):
            return "Яндекс.Директ"
        if utm:
            return _UTM_SOURCE_LABELS.get(utm, clean_text(utm_source_raw))
        return "Другая реклама"

    engine = normalize_source_engine(source_engine_raw)

    # For paid traffic: upgrade social network labels to ad platform labels
    if engine and is_paid_source(source):
        engine = _PAID_SOCIAL_UPGRADE.get(engine, engine)

    if engine:
        return engine

    # UTM fallback — skip for direct/internal traffic (no referrer = no meaningful sub-source)
    src_lo = source.lower()
    if "прямые" in src_lo or "внутренние" in src_lo:
        return ""

    src = clean_text(utm_source_raw)
    if src:
        return _UTM_SOURCE_LABELS.get(src.lower(), src)
    return ""


def is_paid_source(source: str) -> bool:
    s = clean_text(source).lower()
    return "реклам" in s or "direct" in s or s in {"paid", "ad", "ads", "cpc"} or "cpc" in s


def normalize_campaign_id(value: Any) -> str:
    text = clean_text(value)
    if not text or text in {"--", "null", "(not set)", "Не определено"}:
        return ""
    # "name|code|11:ID" → ID  (epromo second account format)
    m = _EPROMO_CAMPAIGN_RE.search(text)
    if m:
        return m.group(1)
    # "<id>-<имя кампании>" → <id>  (geo-appended utm format)
    m = _ID_PREFIX_RE.match(text)
    if m:
        return m.group(1)
    return text


def fact_campaign_id(source: str, campaign_id: str) -> str:
    normalized = normalize_campaign_id(campaign_id)
    if normalized:
        return normalized
    return NOT_SET_CAMPAIGN_ID if is_paid_source(source) else ""


def campaign_name_or_default(source: str, campaign_id: str, campaign_name: str = "") -> str:
    name = clean_text(campaign_name)
    if name:
        return name
    cid = normalize_campaign_id(campaign_id)
    if cid:
        return cid
    return NOT_SET_CAMPAIGN_NAME if is_paid_source(source) else ""


def infer_source_type(campaign_name: str) -> str:
    text = clean_text(campaign_name).lower()
    if "рся" in text or "network" in text:
        return "rsya"
    if "поиск" in text or "search" in text:
        return "search"
    if "мк" in text or "master" in text:
        return "mc"
    return ""


def chunked(items: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


class SupabaseRest:
    def __init__(self) -> None:
        self.url = env_required("SUPABASE_URL").rstrip("/")
        self.key = env_required("SUPABASE_SERVICE_KEY")
        self.base_headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 4,
    ) -> requests.Response:
        url = f"{self.url}/rest/v1/{path}"
        if params:
            url += "?" + urlencode(params, doseq=True)

        merged_headers = dict(self.base_headers)
        if headers:
            merged_headers.update(headers)

        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=merged_headers,
                    data=json.dumps(json_body, ensure_ascii=False).encode("utf-8") if json_body is not None else None,
                    timeout=60,
                )
                if response.status_code < 500 and response.status_code != 429:
                    response.raise_for_status()
                    return response
                response.raise_for_status()
            except Exception as exc:
                last_error = exc
                if attempt == retries - 1:
                    break
                time.sleep(min(2 ** attempt, 20))
        raise RuntimeError(f"Supabase request failed: {method} {path}: {last_error}")

    def delete_date_range(self, table: str, date_from: str, date_to: str) -> None:
        self.request(
            "DELETE",
            table,
            params={"date": [f"gte.{date_from}", f"lte.{date_to}"]},
            headers={"Prefer": "return=minimal"},
        )

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str, chunk_size: int = 500) -> None:
        if not rows:
            print(f"{table}: no rows to upsert")
            return
        for part in chunked(rows, chunk_size):
            self.request(
                "POST",
                table,
                params={"on_conflict": on_conflict},
                json_body=part,
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )
        print(f"{table}: upserted {len(rows)} rows")

    def select_date_range(self, table: str, date_from: str, date_to: str, select: str = "*") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        limit = 1000
        offset = 0
        while True:
            response = self.request(
                "GET",
                table,
                params={
                    "select": select,
                    "date": [f"gte.{date_from}", f"lte.{date_to}"],
                    "order": "date.asc",
                    "limit": limit,
                    "offset": offset,
                },
            )
            page = response.json()
            rows.extend(page)
            if len(page) < limit:
                break
            offset += limit
        return rows
