import json
from datetime import date

from fakes import FakeClient

from google_health_claude.client import HealthApiError
from google_health_claude.fetch import chunks, civil_date, fetch_range


def rollup_point(day, value):
    return {
        "civilStartTime": {"date": {"year": day.year, "month": day.month, "day": day.day}},
        "steps": {"countSum": value},
    }


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_civil_date_parsing():
    assert civil_date({"date": {"year": 2026, "month": 3, "day": 4}}) == date(2026, 3, 4)
    assert civil_date(None) is None
    assert civil_date({"date": {}}) is None


def test_chunks_cover_the_range_without_overlap():
    assert chunks(date(2026, 1, 1), date(2026, 1, 10), 4) == [
        (date(2026, 1, 1), date(2026, 1, 4)),
        (date(2026, 1, 5), date(2026, 1, 8)),
        (date(2026, 1, 9), date(2026, 1, 10)),
    ]


def test_fetch_writes_one_file_per_day_and_category(tmp_path):
    day = date(2026, 11, 25)
    c = FakeClient()
    c.rollups["steps"] = [rollup_point(day, "9000")]
    c.lists["daily-resting-heart-rate"] = [{"dailyRestingHeartRate": {"bpm": 52}}]
    c.lists["sleep"] = [{"sleep": {"id": 1}}]
    c.lists["exercise"] = [{"exercise": {"type": "RUN"}}]

    report = fetch_range(c, day, day, tmp_path, log=lambda _: None)

    daily = read(tmp_path / "daily" / "2026-11-25.json")
    assert daily["metrics"]["steps"]["steps"]["countSum"] == "9000"
    assert daily["metrics"]["daily-resting-heart-rate"][0]["dailyRestingHeartRate"]["bpm"] == 52
    assert read(tmp_path / "sleep" / "2026-11-25.json")["sessions"] == [{"sleep": {"id": 1}}]
    assert (
        read(tmp_path / "workouts" / "2026-11-25.json")["workouts"][0]["exercise"]["type"] == "RUN"
    )
    assert read(tmp_path / "_meta.json")["window"] == ["2026-11-25", "2026-11-25"]
    assert report.requests_failed == 0


def test_refetch_is_idempotent_and_never_erases_data(tmp_path):
    day = date(2026, 11, 25)
    c = FakeClient()
    c.rollups["steps"] = [rollup_point(day, "9000")]
    fetch_range(c, day, day, tmp_path, log=lambda _: None)
    before = (tmp_path / "daily" / "2026-11-25.json").read_text(encoding="utf-8")

    second = fetch_range(c, day, day, tmp_path, log=lambda _: None)
    assert (tmp_path / "daily" / "2026-11-25.json").read_text(encoding="utf-8") == before

    # The device has not synced yet: the API now returns nothing for that day.
    c.rollups["steps"] = []
    fetch_range(c, day, day, tmp_path, log=lambda _: None)
    assert (tmp_path / "daily" / "2026-11-25.json").read_text(encoding="utf-8") == before
    assert second.files_changed <= 1  # only _meta.json may change


def test_filter_spelling_falls_back_to_camel_case_and_is_remembered(tmp_path):
    days = [date(2026, 11, 24), date(2026, 11, 25)]
    c = FakeClient()
    c.reject_snake_case = True
    c.lists["daily-resting-heart-rate"] = [{"v": 1}]
    fetch_range(c, days[0], days[1], tmp_path, log=lambda _: None)
    calls = [f for t, f in c.list_calls if t == "daily-resting-heart-rate"]
    assert len(calls) == 3  # snake (rejected) + camel for day 1, camel directly for day 2
    assert calls[1].startswith("dailyRestingHeartRate.date")
    assert calls[2].startswith("dailyRestingHeartRate.date")


def test_unsupported_data_type_is_reported_once_and_skipped(tmp_path):
    c = FakeClient()
    c.failing["daily-vo2-max"] = HealthApiError(403, "no scope")
    report = fetch_range(c, date(2026, 11, 23), date(2026, 11, 25), tmp_path, log=lambda _: None)
    assert "daily-vo2-max" in report.errors
    assert sum(1 for t, _ in c.list_calls if t == "daily-vo2-max") == 1
    assert read(tmp_path / "_meta.json")["api_errors"]["daily-vo2-max"].startswith("HTTP 403")


def test_skip_existing_avoids_refetching_stored_days(tmp_path):
    day = date(2026, 11, 25)
    c = FakeClient()
    c.rollups["steps"] = [rollup_point(day, "1")]
    fetch_range(c, day, day, tmp_path, log=lambda _: None)
    c.list_calls.clear()
    fetch_range(c, day, day, tmp_path, skip_existing=True, log=lambda _: None)
    # Only weight is listed: it covers a fixed 180-day window on every run.
    assert [t for t, _ in c.list_calls] == ["weight"]


def test_rollup_windows_respect_the_14_day_limit(tmp_path):
    c = FakeClient()
    fetch_range(c, date(2026, 10, 1), date(2026, 10, 30), tmp_path, log=lambda _: None)
    heart_windows = [(s, e) for t, s, e in c.rollup_calls if t == "heart-rate"]
    assert len(heart_windows) == 3
    assert all((e - s).days <= 14 for s, e in heart_windows)
    steps_windows = [(s, e) for t, s, e in c.rollup_calls if t == "steps"]
    assert len(steps_windows) == 1


def test_shorter_refetch_never_erases_stored_sessions(tmp_path):
    day = date(2026, 11, 25)
    c = FakeClient()
    c.lists["sleep"] = [{"name": "s/1", "v": 1}, {"name": "s/2", "v": 2}]
    c.lists["exercise"] = [{"name": "e/1"}, {"name": "e/2"}]
    c.lists["daily-resting-heart-rate"] = [{"name": "r/1", "bpm": 50}]
    fetch_range(c, day, day, tmp_path, log=lambda _: None)

    # Next run: one session is missing, another was updated after a late sync.
    c.lists["sleep"] = [{"name": "s/2", "v": 22}]
    c.lists["exercise"] = [{"name": "e/2"}]
    c.lists["daily-resting-heart-rate"] = [{"name": "r/1", "bpm": 51}]
    fetch_range(c, day, day, tmp_path, log=lambda _: None)

    assert read(tmp_path / "sleep" / "2026-11-25.json")["sessions"] == [
        {"name": "s/1", "v": 1},
        {"name": "s/2", "v": 22},
    ]
    assert read(tmp_path / "workouts" / "2026-11-25.json")["workouts"] == [
        {"name": "e/1"},
        {"name": "e/2"},
    ]
    daily = read(tmp_path / "daily" / "2026-11-25.json")["metrics"]
    assert daily["daily-resting-heart-rate"] == [{"name": "r/1", "bpm": 51}]


def test_backfill_retries_days_whose_lists_failed_transiently(tmp_path):
    day = date(2026, 11, 25)
    c = FakeClient()
    c.rollups["steps"] = [rollup_point(day, "1")]  # creates daily/2026-11-25.json
    c.failing["sleep"] = HealthApiError(503, "unavailable")
    fetch_range(c, day, day, tmp_path, skip_existing=True, log=lambda _: None)
    assert read(tmp_path / "_meta.json")["incomplete_days"] == ["2026-11-25"]

    # Second backfill: the daily file exists, but the day must still be retried.
    del c.failing["sleep"]
    c.lists["sleep"] = [{"name": "s/1"}]
    c.list_calls.clear()
    fetch_range(c, day, day, tmp_path, skip_existing=True, log=lambda _: None)
    assert any(t == "sleep" for t, _ in c.list_calls)
    assert read(tmp_path / "sleep" / "2026-11-25.json")["sessions"] == [{"name": "s/1"}]
    assert read(tmp_path / "_meta.json")["incomplete_days"] == []


def test_permanent_refusals_do_not_mark_days_incomplete(tmp_path):
    c = FakeClient()
    c.failing["daily-vo2-max"] = HealthApiError(403, "no scope")
    fetch_range(c, date(2026, 11, 24), date(2026, 11, 25), tmp_path, log=lambda _: None)
    assert read(tmp_path / "_meta.json")["incomplete_days"] == []


def test_failing_type_does_not_lose_the_rest_of_the_run(tmp_path):
    day = date(2026, 11, 25)
    c = FakeClient()
    c.rollups["steps"] = [rollup_point(day, "9000")]
    c.failing["sleep"] = HealthApiError(0, "network error (ReadTimeout)")
    report = fetch_range(c, day, day, tmp_path, log=lambda _: None)
    assert (
        read(tmp_path / "daily" / "2026-11-25.json")["metrics"]["steps"]["steps"]["countSum"]
        == "9000"
    )
    assert "sleep" in report.errors


def test_unmatched_rollup_points_are_reported_not_silently_dropped(tmp_path):
    day = date(2026, 11, 25)
    c = FakeClient()
    c.rollups["steps"] = [{"civil_start_time": {"date": {"year": 2026, "month": 11, "day": 25}}}]
    report = fetch_range(c, day, day, tmp_path, log=lambda _: None)
    assert "civilStartTime" in report.errors["steps"]
    assert "civilStartTime" in read(tmp_path / "_meta.json")["api_errors"]["steps"]


def test_meta_records_every_run_and_keeps_error_fields_stable(tmp_path):
    day = date(2026, 11, 25)
    c = FakeClient()
    c.failing["sleep"] = HealthApiError(503, "unavailable")
    c.failing["daily-vo2-max"] = HealthApiError(403, "no scope")
    fetch_range(c, day, day, tmp_path, log=lambda _: None)
    first = read(tmp_path / "_meta.json")
    fetch_range(c, day, day, tmp_path, log=lambda _: None)
    second = read(tmp_path / "_meta.json")

    assert set(second) == {"last_fetch_utc", "window", "api_errors", "incomplete_days", "tcx"}
    assert second["window"] == ["2026-11-25", "2026-11-25"]
    assert second["api_errors"] == first["api_errors"]
    assert second["incomplete_days"] == first["incomplete_days"] == ["2026-11-25"]
