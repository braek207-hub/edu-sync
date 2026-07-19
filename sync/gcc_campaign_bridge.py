# -*- coding: utf-8 -*-
"""sync/gcc_campaign_bridge.py — свести кампанию Метрики с кампанией Triple Whale.

Проблема: одна и та же кампания приходит в трёх видах.
  Метрика (визиты) : "Instagram_Feed-CPO_SUMMER_SALE_KSA"  — {плейсмент}-{имя}
  TW-заказы        : "120249879465530017"                  — числовой id
  TW-расход        : "120249879465530017"                  — числовой id
Расход и заказы сходятся между собой, а визиты — нет, поэтому мерж по кампаниям
разваливался: заказов в строку с визитами попадало 52%.

Мост даёт `ads_table` того же SQL-эндпоинта TW: там лежат ОБЕ стороны — campaign_id
и campaign_name. Строим справочник имя→id и переводим метку Метрики в id.

У Google Ads проблемы нет: он пишет в utm_campaign сразу id (ValueTrack), такие метки
пропускаем как есть.
"""
import re

DICT_SQL = (
    "SELECT campaign_id, campaign_name FROM ads_table "
    "WHERE event_date BETWEEN @startDate AND @endDate "
    "AND campaign_name != '' GROUP BY campaign_id, campaign_name"
)


def _norm(text: str | None) -> str:
    """Ключ сравнения: регистр и краевые пробелы не значимы."""
    return (text or "").strip().upper()


def build_campaign_index(db_rows: list[dict]) -> dict[str, str]:
    """Справочник {ИМЯ КАМПАНИИ → campaign_id} из строк ads_table.

    Одно имя может встретиться с разными id (кампанию пересоздали) — берём первый
    попавшийся: для мержа важно попасть в ту же строку, что заказы и расход, а они
    ссылаются на действующий id. Коллизии редки и на сумму не влияют.
    """
    index: dict[str, str] = {}
    for row in db_rows:
        name = _norm(row.get("campaign_name"))
        cid = (row.get("campaign_id") or "").strip()
        if name and cid:
            index.setdefault(name, cid)
    return index


def resolve_campaign(utm_campaign: str | None, index: dict[str, str]) -> str | None:
    """Метка utm_campaign Метрики → campaign_id, если кампанию удалось опознать.

    Порядок:
      1. Пустая метка → None.
      2. Уже число → это id (так пишет Google Ads через ValueTrack) → возвращаем как есть.
      3. Точное совпадение с именем из справочника.
      4. Метка ЗАКАНЧИВАЕТСЯ именем кампании → отрезаем префикс плейсмента.
         Берём САМОЕ ДЛИННОЕ подходящее имя: иначе "CPO_NEW IN_W" перехватило бы
         метку кампании "CPO_NEW IN_W_23". Суффиксом, а не split('-'), потому что
         формат префикса разный ("Instagram_Feed-", "an-", "Facebook_Desktop_Feed-"),
         а в самих именах встречается тире ("CPO_Catalog_All – NEW").
      5. Не опознали → None; вызывающий оставляет исходную метку, чтобы не терять срез.
    """
    raw = (utm_campaign or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return raw

    key = _norm(raw)
    exact = index.get(key)
    if exact:
        return exact

    best_name = ""
    for name in index:
        if len(name) > len(best_name) and key.endswith(name):
            # Граница слева: имя должно начинаться после разделителя, а не с середины
            # слова, иначе "..._SALE_KSA" совпало бы с несвязанным "SALE_KSA".
            head = key[: -len(name)]
            if not head or re.search(r"[-_\s]$", head):
                best_name = name
    return index.get(best_name) if best_name else None


def fetch_campaign_index(api_key: str, shop: str, date_from: str, date_to: str) -> dict[str, str]:
    """Справочник имя→id за период. Импорт внутри — чтобы модуль оставался чисто
    вычислительным и тесты не тянули сеть."""
    from sync.gcc_tw_ads import tw_sql

    return build_campaign_index(tw_sql(api_key, shop, DICT_SQL, date_from, date_to))


def bridge_metrika_campaign(utm_campaign: str | None, index: dict[str, str]) -> str | None:
    """То же, что resolve_campaign, но при неудаче возвращает исходную метку.

    Терять метку нельзя: даже неопознанная кампания остаётся осмысленным срезом
    трафика, просто не сойдётся с деньгами.
    """
    resolved = resolve_campaign(utm_campaign, index)
    if resolved:
        return resolved
    return (utm_campaign or "").strip() or None
