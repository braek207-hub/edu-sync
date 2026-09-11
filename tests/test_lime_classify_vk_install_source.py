import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.lime import classify


def test_vk_install_attributed_sessions_stay_where_procontext_puts_them():
    """`vk-ads-(ex.-mytarget)` / (not set) — сессии приложения без UTM, подписанные
    AppMetrica источником УСТАНОВКИ. Расхода и кампании у них нет; по решению Павла
    (2026-09-11) лежат там же, где у PROCONTEXT, — в органике, а не в SMM paid.
    Ценность установок по кампаниям считается отдельно — «Заказы установок» (AppMetrica)."""
    assert classify("vk-ads-(ex.-mytarget)", "(not set)") == ("SMM (organic)", "Vk-ads-(ex.-mytarget)")


def test_vk_ads_utm_clicks_are_paid():
    assert classify("vk_ads", "cpc") == ("SMM paid", "VK.Ads")
