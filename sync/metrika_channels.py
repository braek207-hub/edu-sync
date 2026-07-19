# -*- coding: utf-8 -*-
"""Маппинг источников Яндекс.Метрики → КАНОНИЧЕСКАЯ таксономия каналов дашборда.

Общий для всех синков, которые читают Метрику напрямую (GCC — счётчик Залива,
KZ — гео-срез счётчика LIME). Ничего специфичного для региона тут нет.

⚠️ Канон задаёт `sync.lime.classify()` — по нему живут RU и KZ из витрины PROCONTEXT.
Этот модуль ОБЯЗАН выдавать те же строки подканалов, иначе один и тот же канал в разных
регионах называется по-разному и при группировке через все регионы распадается на две
строки. Так и было до 2026-07-19: витрина писала «SEO Google», Метрика — «SEO Google,
search results»; витрина «Реферал», Метрика «Referral».

Канон подканалов (sync/lime.py):
  SEO           → «SEO Google» | «SEO Yandex» | «SEO Others»
  SEM           → «Google.Adwords» | «Яндекс.Директ»
  SMM paid      → «Meta Ads» | «TikTok Ads» | «Snapchat Ads» — этих веток в classify() НЕТ:
                  в RU Meta/TikTok/Snapchat не крутят, поэтому канон их не знает. Имена
                  подканалов заданы здесь и продублированы в SPEND_METRIC_MAP и map_tw_source
                  (sync/gcc_*). Появится Meta в RU — ветки надо добавить в classify() и свести.
  SMM (organic) → имя сети с заглавной | «Others»
  CRM           → «Mindbox» | «Email»
  Referrals     → домен реферера | «Реферал»
  Direct        → «Direct»
  Internal      → «Internal»
"""

# Поисковики: Метрика возвращает подробные имена («Google, search results»,
# «Google: mobile app», «Yandex Smart Camera», «Yandex.Images»). Канон знает три
# ступени — Google, Yandex, всё остальное. Матчим по вхождению, а не по равенству.
_SEO_CANON = (("google", "SEO Google"), ("yandex", "SEO Yandex"))

# Соцсети, которые канон называет по имени сети (sync/lime.py classify).
_SOCIAL_KNOWN = ("vkontakte", "instagram", "telegram", "facebook", "youtube",
                 "dzen", "tiktok", "snapchat", "pinterest", "vk", "tg")


def _seo_subchannel(source_engine: str | None) -> str:
    engine = (source_engine or "").lower()
    for needle, name in _SEO_CANON:
        if needle in engine:
            return name
    return "SEO Others"


def _social_subchannel(source_engine: str | None) -> str:
    engine = (source_engine or "").strip()
    low = engine.lower()
    for known in _SOCIAL_KNOWN:
        if known in low:
            return engine.capitalize() if engine else "Others"
    return "Others"


def map_metrika_channel(
    traffic_source_id: str | None,
    source_engine: str | None,
    utm_source: str | None = None,
) -> tuple[str, str, str]:
    """Маппинг Яндекс.Метрики traffic_source + sourceEngine → канон дашборда.

    Args:
        traffic_source_id: из dimensions[1].id (напр. "ad", "organic", "direct", None)
        source_engine: из dimensions[2].name (напр. "Google Ads", "Instagram", None)
        utm_source: метка utm_source визита — нужна только чтобы отличить рассылку
            Mindbox от прочей почты (канон различает «Mindbox» и «Email»).

    Returns:
        (channel, subchannel, traffic_type) где traffic_type ∈ {"Платный", "Бесплатный"}
    """
    source_id = (traffic_source_id or "").lower().strip()
    engine = (source_engine or "").lower().strip()
    utm = (utm_source or "").lower().strip()

    # === AD (платный трафик) ===
    if source_id == "ad":
        if "google" in engine:
            return "SEM", "Google.Adwords", "Платный"
        if any(x in engine for x in ("instagram", "facebook", "meta")):
            return "SMM paid", "Meta Ads", "Платный"
        if "yandex" in engine:
            return "SEM", "Яндекс.Директ", "Платный"
        if "tiktok" in engine:
            return "SMM paid", "TikTok Ads", "Платный"
        if "snapchat" in engine:
            return "SMM paid", "Snapchat Ads", "Платный"
        # Площадка не опознана (реклама без меток) — но это ТОЧНО платный трафик.
        return "SEM", (source_engine or "Ad"), "Платный"

    # === ORGANIC (SEO) ===
    if source_id == "organic":
        return "SEO", _seo_subchannel(source_engine), "Бесплатный"

    # === DIRECT ===
    if source_id == "direct":
        return "Direct", "Direct", "Бесплатный"

    # === SOCIAL / MESSENGER (бесплатный социал) ===
    if source_id in ("social", "messenger"):
        return "SMM (organic)", _social_subchannel(source_engine), "Бесплатный"

    # === REFERRAL ===
    if source_id == "referral":
        # Домен реферера информативнее, но Метрика отдаёт его не всегда: без движка
        # в наборе измерений весь реферальный трафик приходит безымянным. Канон
        # витрины в этом случае называет подканал «Реферал» — совпадаем с ним.
        return "Referrals", (source_engine or "Реферал"), "Бесплатный"

    # === EMAIL (CRM) ===
    if source_id == "email":
        # Mindbox (междунар. бренд Maestra) — ESP самого LIME. Витрина различает
        # «Mindbox» и «Email», различаем и мы, иначе клики рассылки и заказы из неё
        # встают в разные подканалы.
        if "mindbox" in utm or "maestra" in utm:
            return "CRM", "Mindbox", "Бесплатный"
        return "CRM", "Email", "Бесплатный"

    # === INTERNAL ===
    if source_id == "internal":
        return "Internal", "Internal", "Бесплатный"

    # === DEFAULT ===
    return "Others", (source_engine or "Unknown"), "Бесплатный"
