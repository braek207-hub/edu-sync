"""Task 5 (Фаза B) — session-aware cutoff в build_feature_rows. Per-visit сессии
(edu_visit_sessions, Task 3) с точным visit_ts позволяют учитывать same-day визиты
СТРОГО ДО заявки, а не отбрасывать весь день целиком (старый _before_lead_visits по
дате). См. docs/superpowers/sdd/2026-07-27-edu-ml-phase-b-logs-api/task-5-brief.md."""

from datetime import date, datetime

from sync.ml.features import _before_lead_sessions, _last_session, build_feature_rows


def test_sessions_include_same_day_before_lead():
    created_ts = datetime(2026, 3, 1, 15, 0, 0)
    sessions = {"c1": [
        {"visit_ts": datetime(2026, 3, 1, 10, 0, 0), "visit_duration": 200, "is_new_user": 0},  # тот же день, ДО → учесть
        {"visit_ts": datetime(2026, 3, 1, 16, 0, 0), "visit_duration": 300, "is_new_user": 0},  # ПОСЛЕ заявки → отбросить
    ]}
    lead = {"lead_id": "L", "client_id": "c1", "land": "vuz", "created_date": date(2026, 3, 1), "created_ts": created_ts}
    rows = build_feature_rows([lead], sessions, [date(2026, 9, 1)], date(2026, 7, 1))
    f = rows[0]["features"]
    assert f["f__missing_behavior"] == 0            # раньше было 1 (same-day отбрасывался)
    assert f["f__beh_avg_duration_sec"] == 200      # только визит ДО 15:00


def test_before_lead_sessions_degrades_to_date_without_created_ts():
    """Лиды без created_ts (легаси/ленды без точного времени) — деградация на
    сравнение по дате: same-day визит НЕ учитывается (порядок в рамках дня неизвестен),
    но day-1/before учитывается как раньше."""
    sessions = [
        {"visit_ts": datetime(2026, 3, 1, 10, 0, 0)},  # тот же день — не различить порядок → отбросить
        {"visit_ts": datetime(2026, 2, 28, 23, 59, 0)},  # день раньше → учесть
    ]
    before = _before_lead_sessions(sessions, created_ts=None, created_date=date(2026, 3, 1))
    assert len(before) == 1
    assert before[0]["visit_ts"] == datetime(2026, 2, 28, 23, 59, 0)


def test_sess_categorical_features_use_last_pre_lead_session():
    """sess_* категориальные — значение ПОСЛЕДНЕЙ pre-lead сессии (ближе всего к
    заявке); is_new_user/has_gclid — max (флаг «было хоть раз» за pre-lead окно)."""
    created_ts = datetime(2026, 3, 1, 15, 0, 0)
    sessions = {"c1": [
        {
            "visit_ts": datetime(2026, 2, 27, 9, 0, 0), "is_new_user": 1, "has_gclid": 0,
            "utm_source": "yandex", "direct_platform_type": "search", "phone_model": "iPhone",
        },
        {
            # позже, но всё ещё ДО заявки — должна победить эта сессия для категориальных
            "visit_ts": datetime(2026, 2, 28, 9, 0, 0), "is_new_user": 0, "has_gclid": 1,
            "utm_source": "google", "direct_platform_type": "context", "phone_model": "Samsung",
        },
    ]}
    lead = {"lead_id": "L", "client_id": "c1", "land": "vuz", "created_date": date(2026, 3, 1), "created_ts": created_ts}
    rows = build_feature_rows([lead], sessions, [date(2026, 9, 1)], date(2026, 7, 1))
    f = rows[0]["features"]
    assert f["f__sess_utm_source"] == "google"           # последняя pre-lead сессия
    assert f["f__sess_direct_platform_type"] == "context"
    assert f["f__sess_phone_model"] == "Samsung"
    assert f["f__sess_is_new_user"] == 1                 # max — было хоть раз
    assert f["f__sess_has_gclid"] == 1                   # max — было хоть раз


def test_last_session_deterministic_tiebreak_on_equal_visit_ts():
    """Без ORDER BY порядок строк из Postgres не гарантирован (двойная загрузка
    страницы в ту же секунду → одинаковый visit_ts) — тай-брейк по visit_id должен
    давать один и тот же результат независимо от порядка элементов в списке."""
    same_ts = datetime(2026, 3, 1, 10, 0, 0)
    a = {"visit_ts": same_ts, "visit_id": "100", "utm_source": "a_source"}
    b = {"visit_ts": same_ts, "visit_id": "200", "utm_source": "b_source"}

    forward = _last_session([a, b])
    reversed_ = _last_session([b, a])

    assert forward == reversed_ == b            # больший visit_id побеждает тай-брейк
    assert forward["utm_source"] == "b_source"
