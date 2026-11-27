from datetime import date

from google_health_claude.config import READ_SCOPES
from google_health_claude.fetch.datatypes import LIST_SPECS, ROLLUP_SPECS


def spec(name):
    return next(s for s in LIST_SPECS if s.data_type == name)


def test_daily_filter_has_both_spellings():
    snake, camel = spec("daily-heart-rate-variability").filter_candidates(date(2026, 11, 25))
    assert snake == (
        'daily_heart_rate_variability.date >= "2026-11-25" '
        'AND daily_heart_rate_variability.date < "2026-11-26"'
    )
    assert camel.startswith('dailyHeartRateVariability.date >= "2026-11-25"')


def test_sleep_is_filtered_by_wake_up_day():
    snake, camel = spec("sleep").filter_candidates(date(2026, 12, 31))
    assert 'sleep.interval.civil_end_time >= "2026-12-31"' in snake
    assert 'sleep.interval.civil_end_time < "2027-01-01"' in snake
    assert "sleep.interval.civilEndTime" in camel


def test_exercise_uses_civil_start_time():
    snake, _ = spec("exercise").filter_candidates(date(2026, 3, 14))
    assert snake.startswith('exercise.interval.civil_start_time >= "2026-03-14"')


def test_rollup_limits():
    limits = {s.data_type: s.max_days for s in ROLLUP_SPECS}
    assert limits["heart-rate"] == 14
    assert limits["steps"] == 90


def test_only_read_scopes_are_requested():
    assert READ_SCOPES
    assert all(s.endswith(".readonly") for s in READ_SCOPES)
    assert len(READ_SCOPES) == 9
