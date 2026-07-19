# -*- coding: utf-8 -*-
from sync.ecommerce_health import detect_ecommerce_drop

# Типичная неделя KZ: ~1.5% конверсии в заказ.
NORMAL = [(9000, 140), (9500, 150), (8800, 130), (9200, 145), (9100, 138)]


def test_flags_the_real_outage_of_2026_07_06():
    """6 июля: 8 882 визита и 19 заказов при норме 110-270 — синк промолчал."""
    msg = detect_ecommerce_drop(8882, 19, NORMAL)
    assert msg is not None
    assert "19" in msg


def test_flags_the_real_outage_of_2026_07_08():
    """8 июля: 9 598 визитов и 39 заказов — тот же класс провала."""
    assert detect_ecommerce_drop(9598, 39, NORMAL) is not None


def test_normal_day_is_silent():
    assert detect_ecommerce_drop(9000, 142, NORMAL) is None


def test_weak_but_plausible_day_is_silent():
    """Просадка вдвое бывает и по-настоящему — порог ловит обвал, а не колебание."""
    assert detect_ecommerce_drop(9000, 70, NORMAL) is None


def test_low_traffic_day_is_silent():
    """На малом трафике конверсия шумит — не поднимаем ложную тревогу."""
    assert detect_ecommerce_drop(300, 1, NORMAL) is None


def test_empty_baseline_is_silent():
    assert detect_ecommerce_drop(9000, 19, []) is None


def test_zero_visit_days_in_baseline_do_not_crash():
    """Дни без визитов (синк не отработал) не должны ронять расчёт делением на ноль."""
    assert detect_ecommerce_drop(9000, 140, [(0, 0), (9000, 140)]) is None


def test_baseline_median_ignores_one_bad_day():
    """Один провал в базе не должен опускать порог и прятать следующий провал."""
    with_outage = NORMAL + [(9000, 5)]
    assert detect_ecommerce_drop(8882, 19, with_outage) is not None


def test_known_borderline_2026_06_17_still_fires():
    """Канун распродажи 17.06.2026 срабатывает — и это осознанно оставлено.

    11 467 визитов / 51 заказ (CR 0.44%) против базовой ~0.92% — просадка в 2.1 раза,
    чуть за порогом. Сбор был исправен: 18.06 стартовала медийка и распродажа, покупки
    просто откладывали. Порог под этот день НЕ подгоняли — подгонка под ретроспективу
    ослабила бы детектор на будущих настоящих провалах. Тест фиксирует поведение, чтобы
    рефактор не «починил» его молча.
    """
    june_baseline = [
        (6932, 67), (6720, 56), (7082, 47), (6648, 58), (6801, 62), (6444, 70),
        (6608, 56), (6287, 64), (5875, 59), (4641, 50), (4600, 63), (4805, 54),
        (5707, 48), (7600, 41),
    ]
    assert detect_ecommerce_drop(11467, 51, june_baseline) is not None
