"""20.10 визиты EDU упали: Метрика отказала окну 21.08–19.09 (пик приёмной) — «Запрос
слишком сложный». Отказное окно должно делиться пополам, а не валить весь синк."""
from datetime import date, timedelta

import pytest

from sync import edu_visits


def _run(monkeypatch, too_complex):
    ok = []

    def fake_fetch(counter, d1, d2, token, keep):
        a, b = date.fromisoformat(d1), date.fromisoformat(d2)
        if too_complex(a, b):
            raise edu_visits.QueryTooComplex("query_error")
        ok.append((a, b))
        return [{"visit_date": d1}]

    monkeypatch.setattr(edu_visits, "fetch_edu_client_visits", fake_fetch)
    monkeypatch.setattr(edu_visits, "upsert_edu_visit_behavior", lambda rows: len(rows))
    n = edu_visits._sync_window(date(2026, 8, 21), date(2026, 9, 19), "t", set())
    return n, ok


def test_complex_window_is_halved_until_it_passes(monkeypatch):
    n, ok = _run(monkeypatch, lambda a, b: (b - a).days > 10)
    assert n == len(ok) == 4
    assert ok[0][0] == date(2026, 8, 21) and ok[-1][1] == date(2026, 9, 19)
    for (_, prev_end), (next_start, _) in zip(ok, ok[1:]):
        assert next_start == prev_end + timedelta(days=1)


def test_single_day_still_too_complex_raises(monkeypatch):
    with pytest.raises(edu_visits.QueryTooComplex):
        _run(monkeypatch, lambda a, b: True)
