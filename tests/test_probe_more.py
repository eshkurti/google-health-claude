"""probe-more with a mocked client (all values invented)."""

import json
from datetime import date

import pytest
from fakes import FakeResponse, FakeSession

from google_health_claude.api.client import HealthApiError, HealthClient
from google_health_claude.api.credentials import Credentials
from google_health_claude.cli import build_parser
from google_health_claude.probe_more import (
    GUESSED_TYPES,
    OTHER_TYPES,
    TypeInfo,
    field_names,
    instant_filters,
    max_bpm,
    median_spacing_s,
    probe_more,
    range_filters,
    time_range,
    zone_boundaries,
)

TODAY = date(2026, 1, 10)


class FakeClient:
    """Answers by data type; a value may be points, an exception, or a function of the filter."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []
        self.last_status = None

    def _reply(self, data_type, flt):
        reply = self.replies.get(data_type, HealthApiError(404, "data type not found"))
        if callable(reply):
            reply = reply(flt)
        if isinstance(reply, HealthApiError):
            self.last_status = reply.status
            raise reply
        self.last_status = 200
        return reply

    def list_data_points(self, data_type, flt=None, page_size=None):
        self.calls.append(("list", data_type, flt))
        return self._reply(data_type, flt)

    def list_page(self, data_type, flt=None, page_size=None, page_token=None):
        self.calls.append(("page", data_type, flt))
        return {"dataPoints": self._reply(data_type, flt)}

    def daily_rollup(self, data_type, start, end_exclusive):
        self.calls.append(("rollup", data_type, (start, end_exclusive)))
        return self._reply(data_type, None)


def hr_point(t, bpm):
    return {
        "name": "users/0000/dataTypes/heart-rate/dataPoints/secret-id",
        "dataSource": {"platform": "FITBIT"},
        "heartRate": {
            "beatsPerMinute": bpm,
            "sampleTime": {"physicalTime": t, "civilTime": {"date": {"year": 2026}}},
        },
    }


HR = [
    hr_point("2026-01-08T07:00:00Z", "141"),
    hr_point("2026-01-08T07:00:01Z", "163"),
    hr_point("2026-01-08T07:00:02Z", "152"),
    hr_point("2026-01-08T07:00:07Z", "149"),
]

ZONES = [
    {
        "name": "users/0000/dataTypes/daily-heart-rate-zones/dataPoints/z1",
        "dailyHeartRateZones": {
            "date": {"year": 2026, "month": 1, "day": d},
            "heartRateZones": [
                {
                    "heartRateZoneType": "LIGHT",
                    "minBeatsPerMinute": "97",
                    "maxBeatsPerMinute": "123",
                },
            ],
        },
    }
    for d in (5, 9)
]


@pytest.fixture
def data_repo(tmp_path):
    folder = tmp_path / "repo" / "data" / "workouts"
    folder.mkdir(parents=True)
    run = {
        "name": "users/0000/dataTypes/exercise/dataPoints/run1",
        "dataSource": {"platform": "FITBIT", "recordingMethod": "ACTIVELY_MEASURED"},
        "exercise": {
            "exerciseType": "RUNNING",
            "interval": {"startTime": "2026-01-08T07:00:00Z", "endTime": "2026-01-08T07:30:00.5Z"},
        },
    }
    (folder / "2026-01-08.json").write_text(
        json.dumps({"date": "2026-01-08", "workouts": [run]}), encoding="utf-8"
    )
    return tmp_path / "repo"


# -- helpers ------------------------------------------------------------------------


def test_filters_follow_the_documented_fields():
    start, end = date(2026, 1, 4), date(2026, 1, 11)
    assert range_filters(TypeInfo("body-fat", "sample"), start, end) == [
        'body_fat.sample_time.civil_time >= "2026-01-04" AND body_fat.sample_time.civil_time < "2026-01-11"',
        'bodyFat.sampleTime.civilTime >= "2026-01-04" AND bodyFat.sampleTime.civilTime < "2026-01-11"',
    ]
    assert range_filters(TypeInfo("altitude", "interval"), start, end)[0].startswith(
        'altitude.interval.civil_start_time >= "2026-01-04"'
    )
    assert range_filters(TypeInfo("daily-heart-rate-zones", "daily"), start, end)[0].startswith(
        'daily_heart_rate_zones.date >= "2026-01-04"'
    )
    assert range_filters(TypeInfo("sleep", "sleep"), start, end)[0].startswith(
        "sleep.interval.civil_end_time"
    )
    # Europe/Berlin midnight is 23:00 UTC the day before in winter.
    assert range_filters(TypeInfo("electrocardiogram", "ecg"), start, end) == [
        'electrocardiogram.interval.start_time >= "2026-01-03T23:00:00Z"'
    ]
    assert range_filters(TypeInfo("food", "no-filter"), start, end) == [None]


def test_instant_filter_for_heart_rate():
    snake, camel = instant_filters("heart-rate", "2026-01-08T07:00:00Z", "2026-01-08T07:30:00Z")
    assert snake == (
        'heart_rate.sample_time.physical_time >= "2026-01-08T07:00:00Z" '
        'AND heart_rate.sample_time.physical_time < "2026-01-08T07:30:00Z"'
    )
    assert camel.startswith("heartRate.sampleTime.physicalTime >=")


def test_field_names_are_key_paths_without_values():
    names = field_names(ZONES[0])
    assert "dailyHeartRateZones.heartRateZones[].minBeatsPerMinute" in names
    assert "name" in names
    assert not any("97" in n or "users/" in n for n in names)


def test_summaries():
    assert max_bpm(HR) == 163
    assert median_spacing_s(HR) == 1
    assert time_range(HR) == "earliest 2026-01-08T07:00:00Z, latest 2026-01-08T07:00:07Z"
    assert time_range(ZONES) == "earliest 2026-01-05, latest 2026-01-09 (dates only)"
    assert time_range([{"x": 1}]) == "no timestamps found"
    assert zone_boundaries(ZONES) == "yes (2/2 points)"
    assert (
        zone_boundaries(
            [{"dailyHeartRateZones": {"heartRateZones": [{"heartRateZoneType": "LIGHT"}]}}]
        )
        == "no (0/1 points)"
    )


def test_catalog_has_no_write_only_types_and_no_duplicates():
    names = [t.data_type for t in (*OTHER_TYPES, *GUESSED_TYPES)]
    assert len(names) == len(set(names))
    for write_only in ("menstrual-period", "moods", "ovulation-test", "symptoms"):
        assert write_only not in names
    # Sections 1-3 are not repeated in section 4.
    for done in (
        "heart-rate",
        "daily-heart-rate-zones",
        "weight",
        "body-fat",
        "altitude",
        "run-vo2-max",
    ):
        assert done not in names
    for word in ("score", "readiness", "resilience", "load", "stress", "cardio"):
        assert any(word in n for n in names)


# -- whole probe ------------------------------------------------------------------


def test_probe_more_flow(data_repo, tmp_path, monkeypatch):
    def heart_rate(flt):
        # Only the camelCase spelling is "accepted", to exercise the fallback.
        return HR if flt.startswith("heartRate.") else HealthApiError(400, "bad filter")

    client = FakeClient(
        {
            "heart-rate": heart_rate,
            "daily-heart-rate-zones": ZONES,
            "weight": [],
            "body-fat": HealthApiError(403, "scope missing"),
            "altitude": [
                {
                    "altitude": {
                        "gainMillimeters": "4200",
                        "interval": {"startTime": "2026-01-02T08:00:00Z"},
                    }
                }
            ],
            "run-vo2-max": [],
            "steps": [{"steps": {"count": "12"}}] * 3,
            "floors": [{"floors": {}}],
            "sleep": HealthApiError(500, "backend error"),
        }
    )
    lines = []
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    failures = probe_more(client, data_repo, TODAY, out=lines.append)
    out = "\n".join(lines)

    assert failures == 1  # only the 500; 400/403/404 are results
    assert list(workdir.iterdir()) == []  # terminal output only

    # 1. heart-rate over the run interval, after the spelling fallback
    hr_calls = [c for c in client.calls if c[1] == "heart-rate"]
    assert len(hr_calls) == 2
    assert '"2026-01-08T07:00:00Z"' in hr_calls[1][2] and '"2026-01-08T07:30:00Z"' in hr_calls[1][2]
    assert "HTTP 200, 4 points" in out
    assert "max bpm: 163" in out
    assert "median seconds between samples: 1" in out
    assert "earliest 2026-01-08T07:00:00Z, latest 2026-01-08T07:00:07Z" in out

    # 2. zones over 7 days, with boundaries
    zones_flt = next(c[2] for c in client.calls if c[1] == "daily-heart-rate-zones")
    assert '>= "2026-01-04"' in zones_flt and '< "2026-01-11"' in zones_flt
    assert "zone boundaries present: yes (2/2 points)" in out

    # 3. 90 days; a 403 is reported, not raised
    weight_flt = next(c[2] for c in client.calls if c[1] == "weight")
    assert '>= "2025-10-13"' in weight_flt
    assert "HTTP 403: scope missing" in out
    assert "altitude.gainMillimeters" in out

    # 4. exactly one call per type; rollup-only types use dailyRollUp
    section4 = [
        c for c in client.calls if c[1] in {t.data_type for t in (*OTHER_TYPES, *GUESSED_TYPES)}
    ]
    assert len(section4) == len(OTHER_TYPES) + len(GUESSED_TYPES)
    assert ("rollup", "floors", (date(2026, 1, 4), date(2026, 1, 11))) in client.calls
    assert any(
        line.startswith("  steps [list]") and line.endswith("HTTP 200, 3 points") for line in lines
    )
    assert any(
        "daily-readiness [list, guess, not in docs]" in line and "HTTP 404" in line
        for line in lines
    )
    assert any(line.startswith("  sleep") and "HTTP 500" in line for line in lines)

    # Never raw values or record names.
    for secret in ("141", "152", "149", "97", "123", "4200", "users/0000", "secret-id", "run1"):
        assert secret not in out


def test_probe_more_without_a_run_skips_section_1(tmp_path):
    (tmp_path / "workouts").mkdir()
    lines = []
    client = FakeClient({})
    probe_more(client, tmp_path, TODAY, out=lines.append)
    assert any("SKIP" in line for line in lines)
    assert not any(c[1] == "heart-rate" for c in client.calls)


def test_list_page_returns_one_page_and_tracks_status():
    session = FakeSession(
        [
            FakeResponse(200, {"access_token": "tok", "expires_in": 3599}),
            FakeResponse(200, {"dataPoints": [{"a": 1}], "nextPageToken": "p2"}),
        ]
    )
    client = HealthClient(
        Credentials("id", "secret", "refresh"), session=session, sleep=lambda _: None
    )
    body = client.list_page("steps", "f")
    assert body["nextPageToken"] == "p2"
    assert client.last_status == 200
    assert len(session.calls) == 2  # no second page fetched


def test_cli_probe_more_arguments():
    args = build_parser().parse_args(["probe-more", "--data-dir", "repo"])
    assert args.command == "probe-more"
    assert str(args.data_dir) == "repo"
