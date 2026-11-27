"""probe-tcx: workout selection, TCX summary and the probe flow (all values invented)."""

import json

import pytest
from fakes import FakeResponse, FakeSession

from google_health_claude.cli import build_parser
from google_health_claude.client import HealthClient
from google_health_claude.credentials import Credentials
from google_health_claude.probe import probe_tcx
from google_health_claude.tcx import exercise_id, load_workouts, pick_workouts, summarize_tcx

# Distinctive invented coordinates: the tests assert they are never printed.
LAT, LON = "12.3456789", "98.7654321"

NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
EXT = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"


def trackpoint(time, position=True, hr=True, altitude=True):
    parts = [f"<Time>{time}</Time>"]
    if position:
        parts.append(
            f"<Position><LatitudeDegrees>{LAT}</LatitudeDegrees><LongitudeDegrees>{LON}</LongitudeDegrees></Position>"
        )
    if altitude:
        parts.append("<AltitudeMeters>41.5</AltitudeMeters>")
    if hr:
        parts.append("<HeartRateBpm><Value>131</Value></HeartRateBpm>")
    parts.append(
        f'<Extensions><ns3:TPX xmlns:ns3="{EXT}"><ns3:Speed>2.9</ns3:Speed></ns3:TPX></Extensions>'
    )
    return "<Trackpoint>" + "".join(parts) + "</Trackpoint>"


def tcx(*points):
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><TrainingCenterDatabase xmlns="{NS}">'
        '<Activities><Activity Sport="Running"><Id>2026-01-01T08:00:00Z</Id>'
        '<Lap StartTime="2026-01-01T08:00:00Z"><Track>' + "".join(points) + "</Track></Lap>"
        "</Activity></Activities></TrainingCenterDatabase>"
    )


GPS_RUN = tcx(
    trackpoint("2026-01-01T08:00:00Z"),
    trackpoint("2026-01-01T08:00:01Z"),
    trackpoint("2026-01-01T08:00:02.000Z"),
    trackpoint("2026-01-01T08:00:07Z", position=False, altitude=False),
)
NO_POINTS = tcx()


# -- summary ----------------------------------------------------------------------


def test_summary_counts_fields_and_intervals():
    s = summarize_tcx(GPS_RUN.encode())
    assert s.error is None
    assert s.trackpoints == 4
    assert (s.with_heart_rate, s.with_position, s.with_altitude) == (4, 3, 3)
    assert s.intervals_s == [1.0, 1.0, 5.0]
    assert "median 1s" in s.interval_text() and "max 5s" in s.interval_text()


def test_summary_tags_are_local_names_without_namespaces():
    tags = summarize_tcx(GPS_RUN.encode()).tags
    assert {
        "TrainingCenterDatabase",
        "Trackpoint",
        "Position",
        "LatitudeDegrees",
        "TPX",
        "Speed",
    } <= set(tags)
    assert not any("{" in t for t in tags)
    assert tags == sorted(tags)


def test_summary_of_session_without_trackpoints():
    s = summarize_tcx(NO_POINTS.encode())
    assert s.trackpoints == 0
    assert s.interval_text().startswith("n/a")


@pytest.mark.parametrize("body, expected", [(b"", "empty"), (b"not xml at all", "not valid XML")])
def test_summary_of_unusable_body(body, expected):
    assert expected in summarize_tcx(body).error


# -- workout selection -----------------------------------------------------------


def workout(n, kind, platform="FITBIT", method="ACTIVELY_MEASURED", start="2026-01-01T08:00:00Z"):
    return {
        "name": f"users/0000/dataTypes/exercise/dataPoints/{n}",
        "dataSource": {"platform": platform, "recordingMethod": method},
        "exercise": {"exerciseType": kind, "interval": {"startTime": start}},
    }


def write_day(root, day, items):
    folder = root / "data" / "workouts"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{day}.json").write_text(
        json.dumps({"date": day, "workouts": items}), encoding="utf-8"
    )


@pytest.fixture
def data_repo(tmp_path):
    write_day(
        tmp_path,
        "2026-01-01",
        [
            workout(1, "RUNNING"),
            workout(2, "STRENGTH_TRAINING", platform="HEALTH_CONNECT"),
            workout(3, "WALKING", method="PASSIVELY_MEASURED"),
        ],
    )
    write_day(
        tmp_path,
        "2026-01-02",
        [
            workout(10, "RUNNING", start="2026-01-02T07:00:00Z"),
            workout(11, "RUNNING", start="2026-01-02T18:00:00.5Z"),  # latest GPS run
            workout(12, "RUNNING", method="PASSIVELY_MEASURED", start="2026-01-02T20:00:00Z"),
            workout(13, "RUNNING", platform="HEALTH_CONNECT", start="2026-01-02T21:00:00Z"),
            workout(14, "STRENGTH_TRAINING", platform="HEALTH_CONNECT"),
            workout(15, "WALKING", method="ACTIVELY_MEASURED"),
        ],
    )
    write_day(
        tmp_path, "2026-01-03", [workout(20, "STRENGTH_TRAINING", method="PASSIVELY_MEASURED")]
    )
    return tmp_path


def test_exercise_id_is_last_name_segment():
    assert exercise_id("users/0000/dataTypes/exercise/dataPoints/42") == "42"


def test_picks_latest_matching_workout_for_each_target(data_repo):
    picked = pick_workouts(load_workouts(data_repo))
    assert picked["running"].exercise_id == "11"
    assert picked["strength"].exercise_id == "20"  # FITBIT preferred over Health Connect
    assert picked["walking"].exercise_id == "3"  # passive only; 15 was actively measured
    assert picked["running"].file_date == "2026-01-02"


def test_strength_falls_back_to_other_platforms(tmp_path):
    write_day(tmp_path, "2026-01-01", [workout(7, "STRENGTH_TRAINING", platform="HEALTH_CONNECT")])
    picked = pick_workouts(load_workouts(tmp_path))
    assert picked["strength"].exercise_id == "7"
    assert picked["running"] is None and picked["walking"] is None


def test_accepts_data_folder_directly(data_repo):
    assert load_workouts(data_repo / "data") == load_workouts(data_repo)


def test_missing_workouts_folder_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_workouts(tmp_path)


# -- probe flow with a mocked HTTP session --------------------------------------

TOKEN = FakeResponse(200, {"access_token": "tok", "expires_in": 3599})


def test_probe_tcx_flow(data_repo, tmp_path, capsys):
    session = FakeSession(
        [
            TOKEN,
            FakeResponse(200, text=GPS_RUN),  # running: has points, no retry
            FakeResponse(200, text=NO_POINTS),  # strength: empty ...
            FakeResponse(200, text=GPS_RUN),  # ... retried with partialData
            FakeResponse(404, {"error": {"code": 404, "message": "no TCX"}}),  # walking: fails ...
            FakeResponse(500, {"error": {"message": "boom"}}),  # ... retry: 5xx, retried
            FakeResponse(404, {"error": {"code": 404, "message": "still no TCX"}}),
        ]
    )
    client = HealthClient(
        Credentials("id", "secret", "refresh"), session=session, sleep=lambda _: None
    )
    save_dir = tmp_path / "out"

    failures = probe_tcx(client, data_repo, save_dir)

    assert failures == 1  # only walking ended in an HTTP error
    urls = [(c["url"].rsplit("/", 1)[-1], c["params"]) for c in session.calls[1:]]
    assert urls == [
        ("11:exportExerciseTcx", {"alt": "media"}),
        ("20:exportExerciseTcx", {"alt": "media"}),
        ("20:exportExerciseTcx", {"alt": "media", "partialData": "true"}),
        ("3:exportExerciseTcx", {"alt": "media"}),
        ("3:exportExerciseTcx", {"alt": "media", "partialData": "true"}),
        ("3:exportExerciseTcx", {"alt": "media", "partialData": "true"}),
    ]

    assert (save_dir / "running.tcx").read_text(encoding="utf-8") == GPS_RUN
    assert (save_dir / "strength.tcx").read_text(encoding="utf-8") == NO_POINTS
    assert (save_dir / "strength.partialData.tcx").exists()
    assert "no TCX" in (save_dir / "walking.error.txt").read_text(encoding="utf-8")
    assert (save_dir / "walking.partialData.error.txt").exists()

    out = capsys.readouterr().out
    assert "HTTP 200" in out and "HTTP 404" in out
    assert f"{len(GPS_RUN.encode())} bytes" in out
    assert "trackpoints: 4" in out and "trackpoints: 0" in out
    assert "HeartRateBpm: 4/4, Position: 3/4, AltitudeMeters: 3/4" in out
    assert "median 1s" in out
    assert "tags: " in out and "LatitudeDegrees" in out
    assert "still no TCX" in out  # 4xx body is reported
    # Never print coordinates, other values or the full record name.
    for secret in (LAT, LON, "131", "users/0000"):
        assert secret not in out


def test_probe_tcx_reports_missing_workouts(tmp_path, capsys):
    write_day(tmp_path, "2026-01-01", [workout(1, "RUNNING")])
    session = FakeSession([TOKEN, FakeResponse(200, text=GPS_RUN)])
    client = HealthClient(
        Credentials("id", "secret", "refresh"), session=session, sleep=lambda _: None
    )
    assert probe_tcx(client, tmp_path, tmp_path / "out") == 2
    assert capsys.readouterr().out.count("SKIP") == 2


def test_cli_probe_tcx_arguments():
    args = build_parser().parse_args(["probe-tcx", "--data-dir", "repo"])
    assert args.command == "probe-tcx"
    assert str(args.data_dir) == "repo"
    assert args.save_samples.parts == ("samples", "tcx")
