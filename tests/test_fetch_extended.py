"""New daily types, weight and TCX downloads, with a mocked client and synthetic data only."""

import gzip
import json
from datetime import date, timedelta

import pytest

from fakes import FakeClient
from google_health_claude import cli
from google_health_claude.client import HealthApiError, RawReply
from google_health_claude.fetch import fetch_range
from google_health_claude.workout_tcx import eligible, gzip_bytes

DAY = date(2026, 1, 8)
# Invented placeholder coordinates (open ocean); tests check they are never logged.
LAT, LON = "0.1234567", "-0.7654321"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def run(client, data_dir, start=DAY, end=DAY, **kwargs):
    logs = []
    report = fetch_range(client, start, end, data_dir, log=logs.append, **kwargs)
    return report, logs


# -- daily-heart-rate-zones and run-vo2-max ---------------------------------------


def test_new_daily_types_are_stored_with_the_summaries(tmp_path):
    c = FakeClient()
    c.lists["daily-heart-rate-zones"] = [{"name": "z/1", "dailyHeartRateZones": {"heartRateZones": []}}]
    c.lists["run-vo2-max"] = [{"name": "v/1", "runVo2Max": {"vo2Max": 40.5}}]
    run(c, tmp_path)
    metrics = read(tmp_path / "daily" / "2026-01-08.json")["metrics"]
    assert metrics["daily-heart-rate-zones"][0]["name"] == "z/1"
    assert metrics["run-vo2-max"][0]["runVo2Max"]["vo2Max"] == 40.5
    filters = dict(c.list_calls)
    assert filters["daily-heart-rate-zones"] == (
        'daily_heart_rate_zones.date >= "2026-01-08" AND daily_heart_rate_zones.date < "2026-01-09"'
    )
    assert filters["run-vo2-max"].startswith('run_vo2_max.sample_time.civil_time >= "2026-01-08"')


def test_new_types_follow_the_api_errors_handling(tmp_path):
    c = FakeClient()
    c.failing["daily-heart-rate-zones"] = HealthApiError(400, "unsupported")
    c.failing["weight"] = HealthApiError(403, "no scope")
    report, _ = run(c, tmp_path, DAY - timedelta(days=2), DAY)
    meta = read(tmp_path / "_meta.json")
    assert meta["api_errors"]["daily-heart-rate-zones"].startswith("HTTP 400")
    assert meta["api_errors"]["weight"].startswith("HTTP 403")
    assert meta["incomplete_days"] == []  # permanent refusals
    # A permanently refused per-day type is asked once (snake + camel), not every day.
    assert sum(1 for t, _ in c.list_calls if t == "daily-heart-rate-zones") == 2
    assert report.requests_ok > 0


# -- weight ------------------------------------------------------------------------


def weight(stamp, grams, civil=None, name="w/1"):
    sample = {"physicalTime": stamp}
    if civil:
        y, m, d = civil
        sample["civilTime"] = {"date": {"year": y, "month": m, "day": d}, "time": {"hours": 7}}
    return {"name": name, "weight": {"sampleTime": sample, "weightGrams": grams}}


def test_weight_lists_180_days_in_one_call_and_stores_by_sample_date(tmp_path):
    c = FakeClient()
    c.lists["weight"] = [
        weight("2025-09-01T06:00:00Z", 70000, civil=(2025, 9, 1), name="w/old"),
        # No civilTime: 23:30 UTC is already the next day in Berlin.
        weight("2026-01-05T23:30:00Z", 70500, name="w/late"),
    ]
    run(c, tmp_path)
    (flt,) = [f for t, f in c.list_calls if t == "weight"]
    first = (DAY - timedelta(days=179)).isoformat()
    assert flt == (
        f'weight.sample_time.civil_time >= "{first}" AND weight.sample_time.civil_time < "2026-01-09"'
    )
    # Days outside the 1-day fetch window still get their weigh-in.
    assert read(tmp_path / "daily" / "2025-09-01.json")["metrics"]["weight"][0]["name"] == "w/old"
    assert read(tmp_path / "daily" / "2026-01-06.json")["metrics"]["weight"][0]["name"] == "w/late"


def test_weight_merges_by_timestamp_and_never_erases(tmp_path):
    c = FakeClient()
    c.lists["weight"] = [
        weight("2026-01-08T06:00:00Z", 70000, civil=(2026, 1, 8), name="w/a"),
        weight("2026-01-08T19:00:00Z", 70400, civil=(2026, 1, 8), name="w/b"),
    ]
    run(c, tmp_path)

    # Next run: the morning weigh-in comes back under another name with a
    # corrected value, the evening one is missing.
    c.lists["weight"] = [weight("2026-01-08T06:00:00Z", 69900, civil=(2026, 1, 8), name="w/a2")]
    run(c, tmp_path)

    stored = read(tmp_path / "daily" / "2026-01-08.json")["metrics"]["weight"]
    assert [(w["name"], w["weight"]["weightGrams"]) for w in stored] == [("w/a2", 69900), ("w/b", 70400)]


def test_undated_weight_points_are_reported(tmp_path):
    c = FakeClient()
    c.lists["weight"] = [{"name": "w/x", "weight": {"weightGrams": 1}}]
    report, _ = run(c, tmp_path)
    assert "no sampleTime" in report.errors["weight"]


def test_long_window_widens_the_weight_range(tmp_path):
    c = FakeClient()
    start = DAY - timedelta(days=199)
    run(c, tmp_path, start, DAY, skip_existing=True)
    (flt,) = [f for t, f in c.list_calls if t == "weight"]
    assert f'>= "{start.isoformat()}"' in flt


# -- 30-day window and the workflow's `days` input -------------------------------


def test_thirty_day_window_covers_every_day_and_one_weight_call(tmp_path):
    c = FakeClient()
    start = DAY - timedelta(days=29)
    run(c, tmp_path, start, DAY)
    zones_days = [f for t, f in c.list_calls if t == "daily-heart-rate-zones"]
    assert len(zones_days) == 30
    assert sum(1 for t, _ in c.list_calls if t == "weight") == 1
    assert read(tmp_path / "_meta.json")["window"] == [start.isoformat(), DAY.isoformat()]


@pytest.fixture
def cli_calls(monkeypatch):
    calls = []

    class Report:
        requests_ok = 1
        errors = {}

        class tcx:  # noqa: N801 - mimics the attribute
            failed = 0

    def fake_fetch_range(client, start, end, data_dir, **kwargs):
        calls.append((start, end, kwargs))
        return Report()

    monkeypatch.setattr(cli, "load_credentials", lambda *a: None)
    monkeypatch.setattr(cli, "HealthClient", lambda creds: type("C", (), {"refresh_token_expires_in": None})())
    monkeypatch.setattr(cli, "fetch_range", fake_fetch_range)
    monkeypatch.setattr(cli, "today", lambda: DAY)
    return calls


def test_cli_fetch_days_input_from_the_workflow(cli_calls):
    # The workflow passes the `days` input as a string: --days "30".
    assert cli.main(["fetch", "--data-dir", "data", "--days", "30"]) == 0
    start, end, kwargs = cli_calls[0]
    assert (start, end) == (DAY - timedelta(days=29), DAY)
    assert kwargs == {}


def test_cli_backfill_same_window(cli_calls):
    since = (DAY - timedelta(days=30)).isoformat()
    assert cli.main(["backfill", "--data-dir", "data", "--since", since]) == 0
    start, end, kwargs = cli_calls[0]
    assert (start, end) == (DAY - timedelta(days=30), DAY - timedelta(days=1))
    assert kwargs == {"skip_existing": True}


# -- TCX ---------------------------------------------------------------------------


def workout(n, kind="RUNNING", platform="FITBIT", method="ACTIVELY_MEASURED", end="2026-01-08T07:30:00Z"):
    return {
        "name": f"users/0000/dataTypes/exercise/dataPoints/ex{n}",
        "dataSource": {"platform": platform, "recordingMethod": method},
        "exercise": {
            "exerciseType": kind,
            "interval": {"startTime": "2026-01-08T07:00:00Z", "endTime": end},
        },
    }


def tcx_body(points):
    tps = "".join(
        f"<Trackpoint><Time>2026-01-08T07:00:{i:02d}Z</Time><Position><LatitudeDegrees>{LAT}"
        f"</LatitudeDegrees><LongitudeDegrees>{LON}</LongitudeDegrees></Position></Trackpoint>"
        for i in range(points)
    )
    return (
        '<?xml version="1.0"?><TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/'
        f'TrainingCenterDatabase/v2"><Activities><Activity><Lap><Track>{tps}</Track></Lap>'
        "</Activity></Activities></TrainingCenterDatabase>"
    ).encode()


OK = RawReply(200, tcx_body(3))
EMPTY = RawReply(200, tcx_body(0))
TCX_DIR = ("tcx", "2026-01-08")


def tcx_file(root, n):
    return root.joinpath(*TCX_DIR, f"ex{n}.tcx.gz")


def test_eligibility_rules():
    assert eligible(workout(1))  # GPS run
    assert eligible(workout(1, "WALKING"))  # walk started on the band
    assert eligible(workout(1, "STRENGTH_TRAINING", method="PASSIVELY_MEASURED"))
    assert not eligible(workout(1, "WALKING", method="PASSIVELY_MEASURED"))
    assert not eligible(workout(1, platform="HEALTH_CONNECT"))
    assert not eligible(workout(1, method="UNKNOWN"))
    assert not eligible({"name": "x"})


def test_tcx_download_skip_rules_and_storage(tmp_path):
    c = FakeClient()
    c.lists["exercise"] = [
        workout(1),
        workout(2, "STRENGTH_TRAINING", method="PASSIVELY_MEASURED"),
        workout(3, "WALKING", method="PASSIVELY_MEASURED"),
        workout(4, platform="HEALTH_CONNECT"),
    ]
    c.tcx = {"ex1": OK, "ex2": EMPTY}
    report, logs = run(c, tmp_path)

    assert c.tcx_calls == [("ex1", True), ("ex2", True)]  # partialData=true, no walk, no Health Connect
    assert gzip.decompress(tcx_file(tmp_path, 1).read_bytes()) == OK.content
    assert not tcx_file(tmp_path, 2).exists()  # empty shell is not stored
    meta = read(tmp_path / "_meta.json")["tcx"]
    assert meta == {"fetched": 1, "skipped": 0, "empty": 1, "failed": 0, "failures": []}
    assert read(tmp_path.joinpath(*TCX_DIR, "_end_times.json")) == {"ex1": "2026-01-08T07:30:00Z"}

    log_text = "\n".join(logs)
    assert "TCX: 1 fetched, 0 skipped, 1 empty, 0 failed." in log_text
    assert LAT not in log_text and "users/0000" not in log_text


def test_tcx_is_idempotent_and_redownloads_when_end_time_changes(tmp_path):
    c = FakeClient()
    c.lists["exercise"] = [workout(1)]
    c.tcx = {"ex1": OK}
    run(c, tmp_path)
    before = tcx_file(tmp_path, 1).read_bytes()

    # Same workout, same end time: no call, no file change.
    c.tcx_calls.clear()
    report, _ = run(c, tmp_path)
    assert c.tcx_calls == []
    assert report.tcx.skipped == 1 and report.tcx.files_changed == 0
    assert report.files_changed == 1  # only _meta.json
    assert tcx_file(tmp_path, 1).read_bytes() == before

    # The workout was extended after a late sync: download again.
    c.lists["exercise"] = [workout(1, end="2026-01-08T07:45:00Z")]
    c.tcx = {"ex1": RawReply(200, tcx_body(5))}
    report, _ = run(c, tmp_path)
    assert c.tcx_calls == [("ex1", True)]
    assert report.tcx.fetched == 1
    assert gzip.decompress(tcx_file(tmp_path, 1).read_bytes()) == tcx_body(5)
    assert read(tmp_path.joinpath(*TCX_DIR, "_end_times.json"))["ex1"] == "2026-01-08T07:45:00Z"


def test_tcx_redownloads_when_file_is_missing(tmp_path):
    c = FakeClient()
    c.lists["exercise"] = [workout(1)]
    c.tcx = {"ex1": OK}
    run(c, tmp_path)
    tcx_file(tmp_path, 1).unlink()
    c.tcx_calls.clear()
    run(c, tmp_path)
    assert c.tcx_calls == [("ex1", True)]
    assert tcx_file(tmp_path, 1).exists()


def test_empty_reply_never_overwrites_a_stored_file(tmp_path):
    c = FakeClient()
    c.lists["exercise"] = [workout(1)]
    c.tcx = {"ex1": OK}
    run(c, tmp_path)
    c.lists["exercise"] = [workout(1, end="2026-01-08T08:00:00Z")]
    c.tcx = {"ex1": EMPTY}
    report, _ = run(c, tmp_path)
    assert report.tcx.empty == 1
    assert gzip.decompress(tcx_file(tmp_path, 1).read_bytes()) == OK.content


def test_tcx_failures_are_recorded_and_do_not_stop_the_run(tmp_path):
    c = FakeClient()
    c.lists["exercise"] = [workout(1), workout(2), workout(3), workout(4)]
    c.tcx = {
        "ex1": RawReply(404, b'{"error": {"code": 404, "message": "no TCX for this session"}}'),
        "ex2": HealthApiError(0, "network error (ReadTimeout)"),
        "ex3": RawReply(200, b"<not xml"),
        "ex4": OK,
    }
    report, logs = run(c, tmp_path)

    assert tcx_file(tmp_path, 4).exists()
    meta = read(tmp_path / "_meta.json")["tcx"]
    assert (meta["fetched"], meta["failed"]) == (1, 3)
    assert [(f["exercise_id"], f["status"]) for f in meta["failures"]] == [("ex1", 404), ("ex2", 0), ("ex3", 200)]
    assert meta["failures"][0]["error"] == "no TCX for this session"
    assert meta["failures"][0]["exercise_type"] == "RUNNING"
    assert "not valid XML" in meta["failures"][2]["error"]
    # Failed downloads are retried on the next run.
    c.tcx_calls.clear()
    run(c, tmp_path)
    assert sorted(i for i, _ in c.tcx_calls) == ["ex1", "ex2", "ex3"]
    assert not any("users/0000" in line for line in logs)


def test_unsafe_exercise_id_is_never_used_as_a_path(tmp_path):
    c = FakeClient()
    bad = workout(1)
    bad["name"] = "users/0000/dataTypes/exercise/dataPoints/.."
    c.lists["exercise"] = [bad]
    report, _ = run(c, tmp_path)
    assert c.tcx_calls == []
    assert report.tcx.failed == 1
    assert read(tmp_path / "_meta.json")["tcx"]["failures"][0]["exercise_id"] == ""


def test_tcx_covers_workouts_stored_by_earlier_runs(tmp_path):
    c = FakeClient()
    c.lists["exercise"] = [workout(1)]
    run(c, tmp_path)  # 404 by default: failed
    c.lists["exercise"] = []  # the API no longer returns it; the stored record remains
    c.tcx = {"ex1": OK}
    c.tcx_calls.clear()
    run(c, tmp_path, skip_existing=True)
    assert c.tcx_calls == [("ex1", True)]
    assert tcx_file(tmp_path, 1).exists()


def test_gzip_output_is_byte_stable():
    assert gzip_bytes(b"<x/>") == gzip_bytes(b"<x/>")
