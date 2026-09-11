import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.lime import classify


def test_vk_install_attributed_sessions_are_paid_vk_ads():
    """AppMetrica подписывает сессии без UTM именем источника установки «VK Ads (ex. myTarget)»;
    PROCONTEXT отдаёт его как `vk-ads-(ex.-mytarget)` с medium (not set). Это платные
    VK-установки, а не органика — как `yandex.direct / (not set)` уже идёт в SEM."""
    assert classify("vk-ads-(ex.-mytarget)", "(not set)") == ("SMM paid", "VK.Ads")
    assert classify("vkads", "(not set)") == ("SMM paid", "VK.Ads")


def test_organic_vk_stays_organic():
    assert classify("vk", "social") == ("SMM (organic)", "Vk")
    assert classify("vkontakte", "social") == ("SMM (organic)", "Vkontakte")
