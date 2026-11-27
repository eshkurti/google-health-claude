"""Synthetic raw data for the derive tests. Every value here is invented.

Shapes follow the field paths of the fetched files (numbers as strings where
the API sends strings, "123s" durations, UTC timestamps). The scenario, with the
last fetch on Monday 2026-11-23 (so 21-23 Nov are partial):

* weight-only daily files on 2026-09-03 and 2026-09-07 (the weigh-in lookback);
* daily files 2026-10-19 .. 2026-11-23 except 2026-11-04 (a missing day);
  HRV outlier on 2026-11-20; no SpO2 on 2026-11-12; zero-valued heart-rate zones left
  out on 2026-11-11; no time-in-zone rollup on 2026-11-13; two duplicate weigh-ins
  plus a second weigh-in on 2026-11-10; AZM points on 2026-11-16;
* runA  Sun 15 Nov: GPS run with a 21 s GPS gap (with a distance jump and a 30 m
  altitude step across it) and a 60 s walk; its summary distance differs from the TCX total;
* runB1/runB2 Mon 16 Nov: the same run recorded twice by Fitbit (3 s apart),
  plus a Health Connect copy hcB; two identical run-vo2-max entries;
* runC  Thu 19 Nov: GPS run that slows down and whose heart rate rises;
* runD  Sun 22 Nov 23:30 UTC = Mon 23 Nov 00:30 Berlin (week boundary), with an
  8 s GPS gap: short enough that a 30 s smoothing window would reach across it;
* walk1 Sun 15 Nov: passive walk (not training, no TCX);
* str1  Tue 17 Nov: passive strength session with irregular 1 s / 3 s sampling;
* card1 Wed 18 Nov: passive cardio whose TCX has zero trackpoints;
* hcX   Sat 21 Nov: Health Connect record overlapping nothing;
* sleep for the nights ending 15 and 16 Nov, plus a nap on 15 Nov.
"""

from __future__ import annotations

import gzip
import itertools
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from google_health_claude.demo import shapes
from google_health_claude.derive.points import Point, parse_points

LAT, LON = "41.8902102", "12.4922309"  # must never appear in any output
COORD_MARKERS = ("8902102", "4922309")
AS_OF = date(2026, 11, 23)
FIRST_DAY, LAST_DAY, MISSING_DAY = date(2026, 10, 19), date(2026, 11, 23), date(2026, 11, 4)
HRV_OUTLIER_DAY, NO_SPO2_DAY, WEIGH_DAY = date(2026, 11, 20), date(2026, 11, 12), date(2026, 11, 10)
ZERO_ZONE_DAY, NO_ZONE_DAY = date(2026, 11, 11), date(2026, 11, 13)
WEIGHT_ONLY = (
    (date(2026, 9, 3), "2026-09-03T05:30:00Z", 80500),
    (date(2026, 9, 7), "2026-09-07T05:45:00Z", 80250),
)
HR_ZONE_BOUNDS = (
    ("LIGHT", 30, 116),
    ("MODERATE", 117, 142),
    ("VIGOROUS", 143, 175),
    ("PEAK", 176, 220),
)


def read_points(path: Path) -> list[Point] | None:
    """Trackpoints of a .tcx.gz file on disk (derive itself reads it in derive/load.py)."""
    return parse_points(Path(path).read_bytes())


def cet(_moment: datetime) -> int:
    """Every offset in this fixture is +01:00, also for the September weigh-ins."""
    return 3600


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.rstrip("Z")).replace(tzinfo=timezone.utc)


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


# -- tracks and TCX -------------------------------------------------------------------


def make_track(segments, gap=None, gap_extra_m=0.0, steps=(1,)):
    """[(t_s, hr, dist_m)] for segments of (seconds, speed m/s, hr).

    ``gap`` = (from_s, to_s): no points strictly between; ``gap_extra_m`` is added
    to the distance when the gap ends (a GPS re-acquisition jump).
    """
    total = sum(seconds for seconds, _, _ in segments)

    def segment_at(t):
        end = 0
        for seg in segments:
            end += seg[0]
            if t < end:
                return seg
        return segments[-1]

    points, t, d = [], 0.0, 0.0
    cycle = itertools.cycle(steps)
    while t <= total:
        _, speed, hr = segment_at(t)
        if not (gap and gap[0] < t < gap[1]):
            points.append((t, hr, d))
        step = next(cycle)
        d += speed * step
        if gap and t < gap[1] <= t + step:
            d += gap_extra_m
        t += step
    return points


def tcx_bytes(start: datetime, points, gps: bool, alt_step=None) -> bytes:
    """``alt_step`` = (t_s, metres): altitude jumps by that much from t_s on."""
    trackpoints = []
    for i, (t, hr, d) in enumerate(points):
        if gps:
            jump = alt_step[1] if alt_step and t >= alt_step[0] else 0
            trackpoints.append(
                shapes.Trackpoint(t, hr, d, float(LAT), float(LON), 50 + jump + (i % 7) - 3)
            )
        else:
            trackpoints.append(shapes.Trackpoint(t, hr))
    return shapes.tcx_document(start, trackpoints)


def write_tcx(root: Path, day: date, ex_id: str, content: bytes) -> None:
    path = root / "tcx" / day.isoformat() / f"{ex_id}.tcx.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(content, mtime=0))


def splits_from(start: datetime, points) -> list[dict]:
    """API-style per-km splits; boundaries at the first point past each km."""
    return shapes.splits(start, [(t, d) for t, _, d in points], offset_of=cet)


# -- records -----------------------------------------------------------------------------


def workout(
    ex_id,
    ex_type,
    start,
    seconds,
    platform="FITBIT",
    method="ACTIVELY_MEASURED",
    distance_m=0.0,
    avg_hr=None,
    steps=None,
    zones=None,
    azm=None,
    splits=None,
    elevation_mm=None,
):
    return shapes.exercise(
        ex_id,
        ex_type,
        start,
        seconds,
        offset_of=cet,
        platform=platform,
        method=method,
        distance_m=distance_m,
        avg_hr=avg_hr,
        kcal=int(seconds / 6) if avg_hr is not None else None,
        step_count=steps,
        zone_s=zones,
        azm=azm,
        elevation_mm=elevation_mm,
        split_list=splits or (),
    )


def weight_point(point_id: str, platform: str, grams: int, stamp: str) -> dict:
    return shapes.weight(point_id, platform, grams, utc(stamp), offset_of=cet)


def daily_metrics(day: date, i: int) -> dict:
    hrv = 95.0 if day == HRV_OUTLIER_DAY else 40.0 + (i % 5) - 2
    zero = day == ZERO_ZONE_DAY  # the API leaves zero-valued zones and levels out
    azm_day = day == date(2026, 11, 16)
    metrics = {
        "steps": shapes.steps(day, 8000 + 10 * i),
        "distance": shapes.distance(day, 6_000_000 + 1000 * i),
        "active-energy-burned": shapes.active_energy_burned(day, 500.5),
        "total-calories": shapes.total_calories(day, 2500.25),
        "heart-rate": shapes.heart_rate(day, 70.5, 50, 160),
        # The fixture never sends MODERATE active minutes.
        "active-minutes": shapes.active_minutes(
            day, {"LIGHT": 120, "MODERATE": 0, "VIGOROUS": 0 if zero else 25}
        ),
        "active-zone-minutes": shapes.active_zone_minutes(
            day, 10 if azm_day else 0, 60 if azm_day else 0, 12 if azm_day else 0
        ),
        "time-in-heart-rate-zone": shapes.time_in_heart_rate_zone(
            day,
            {
                "LIGHT": 80000,
                "MODERATE": 600,
                "VIGOROUS": 0 if zero else 1800,
                "PEAK": 0 if zero else 360,
            },
        ),
        "sedentary-period": shapes.sedentary_period(day, 36000),
        "daily-resting-heart-rate": shapes.resting_heart_rate(day, 55 + (i % 3) - 1),
        "daily-heart-rate-variability": shapes.heart_rate_variability(day, hrv, 45.5, 2.5, 58),
        "daily-oxygen-saturation": shapes.oxygen_saturation(
            day, 96 + ((i % 4) - 1.5) * 0.2, 94, 98.5, 1.25
        ),
        "daily-respiratory-rate": shapes.respiratory_rate(day, 14.5),
        "daily-sleep-temperature-derivations": shapes.sleep_temperature(day, 33.75, 33.5, 0.25),
        "daily-vo2-max": shapes.vo2_max(day, 42.5, 1.5, "AVERAGE"),
        "daily-heart-rate-zones": shapes.heart_rate_zones(day, HR_ZONE_BOUNDS),
    }
    if day == NO_SPO2_DAY:
        del metrics["daily-oxygen-saturation"]
    if day == NO_ZONE_DAY:
        del metrics["time-in-heart-rate-zone"]
    if day == WEIGH_DAY:
        metrics["weight"] = [
            weight_point(f"w{n}", platform, grams, stamp)
            for n, (platform, grams, stamp) in enumerate(
                (
                    ("FITBIT", 78001, "2026-11-10T06:00:00Z"),
                    ("FITBIT_WEB_API", 78001, "2026-11-10T06:00:47Z"),
                    ("FITBIT", 79001, "2026-11-10T18:00:00Z"),
                )
            )
        ]
    if day == date(2026, 11, 16):
        metrics["run-vo2-max"] = [
            shapes.run_vo2_max(43.6, utc(stamp), offset_of=cet)
            for stamp in ("2026-11-16T06:00:00", "2026-11-16T06:00:03")
        ]
    return metrics


SLEEP_SUMMARY = shapes.SleepSummary(
    asleep_min=420,
    awake_min=30,
    in_period_min=450,
    to_fall_asleep_min=10,
    after_wake_up_min=0,
    stages=(("AWAKE", 30, 3), ("LIGHT", 270, 3), ("DEEP", 60, 3), ("REM", 90, 3)),
)


def sleep_session(sleep_id, bed, wake, main=True):
    stages = []
    cursor = bed
    for stage, minutes in (
        ("AWAKE", 10),
        ("LIGHT", 120),
        ("DEEP", 60),
        ("REM", 90),
        ("LIGHT", 150),
        ("AWAKE", 20),
    ):
        end = min(cursor + timedelta(minutes=minutes), wake)
        if end > cursor:
            stages.append(shapes.Span(stage, cursor, end))
        cursor = end
    awakenings = [
        shapes.Span("LIGHT", bed + timedelta(hours=2), bed + timedelta(hours=2, minutes=1)),
        shapes.Span("REM", bed + timedelta(hours=4), bed + timedelta(hours=4, minutes=2)),
    ]
    return shapes.sleep_session(
        sleep_id, bed, wake, stages, SLEEP_SUMMARY, awakenings, main=main, offset_of=cet
    )


# -- the dataset ----------------------------------------------------------------------------


RUN_A = utc("2026-11-15T09:00:00")
RUN_B = utc("2026-11-16T06:00:00")
RUN_C = utc("2026-11-19T16:00:00")
RUN_D = utc("2026-11-22T23:30:00")


def build_dataset(root: Path) -> dict:
    """Write the scenario under ``root`` (a data folder); return facts the tests check."""
    root = Path(root)
    day = FIRST_DAY
    i = 0
    while day <= LAST_DAY:
        if day != MISSING_DAY:
            write_json(
                root / "daily" / f"{day}.json",
                {"date": day.isoformat(), "metrics": daily_metrics(day, i)},
            )
        day += timedelta(days=1)
        i += 1

    # Weight-only files from the weigh-in lookback, weeks before the fetched days (summer time).
    for day, stamp, grams in WEIGHT_ONLY:
        write_json(
            root / "daily" / f"{day}.json",
            {
                "date": day.isoformat(),
                "metrics": {"weight": [weight_point(f"old{day}", "FITBIT", grams, stamp)]},
            },
        )

    workouts: dict[date, list] = {}

    def add(day: date, record: dict) -> None:
        workouts.setdefault(day, []).append(record)

    track_a = make_track(
        [(400, 3.0, 145), (60, 1.2, 130), (400, 3.0, 155)], gap=(200, 221), gap_extra_m=57.0
    )
    track_b = make_track([(700, 3.2, 150)])
    track_c = make_track([(450, 3.3, 150), (450, 2.9, 160)])
    track_d = make_track([(300, 3.0, 150)], gap=(100, 108), gap_extra_m=30.0)
    gps_runs = {
        "runA": (RUN_A, track_a, 2520.0, 148, (60, 200, 500, 100)),
        "runB1": (RUN_B, track_b, track_b[-1][2], 150, (0, 100, 600, 0)),
        "runB2": (RUN_B + timedelta(seconds=3), track_b, track_b[-1][2], 150, (0, 100, 600, 0)),
        "runC": (RUN_C, track_c, track_c[-1][2], 155, (0, 100, 700, 100)),
        "runD": (RUN_D, track_d, track_d[-1][2], 150, (0, 100, 200, 0)),
    }
    for ex_id, (start, track, distance_m, avg_hr, zones) in gps_runs.items():
        civil = (start + timedelta(hours=1)).date()
        add(
            civil,
            workout(
                ex_id,
                "RUNNING",
                start,
                int(track[-1][0]),
                distance_m=distance_m,
                avg_hr=avg_hr,
                steps=int(distance_m / 0.83),
                zones=zones,
                azm=zones[2] // 30 + zones[3] // 30,
                splits=splits_from(start, track),
                elevation_mm=12000.0,
            ),
        )
        # GPS re-acquired after the gap at another altitude
        alt_step = {"runA": (221, 30.0), "runD": (108, 30.0)}.get(ex_id)
        write_tcx(root, civil, ex_id, tcx_bytes(start, track, gps=True, alt_step=alt_step))

    add(
        date(2026, 11, 16),
        workout(
            "hcB",
            "RUNNING",
            RUN_B + timedelta(seconds=10),
            710,
            platform="HEALTH_CONNECT",
            distance_m=990.0,
            steps=1200,
        ),
    )
    add(
        date(2026, 11, 15),
        workout(
            "walk1",
            "WALKING",
            utc("2026-11-15T14:00:00"),
            2400,
            method="PASSIVELY_MEASURED",
            distance_m=3000.0,
            avg_hr=95,
            steps=4000,
            zones=(2400, 0, 0, 0),
            azm=0,
        ),
    )
    strength_start = utc("2026-11-17T17:00:00")
    strength = [
        (t, 100 if n % 2 == 0 else 160, None)
        for n, t in enumerate(itertools.accumulate([0] + [3, 1] * 300))
    ]
    add(
        date(2026, 11, 17),
        workout(
            "str1",
            "STRENGTH_TRAINING",
            strength_start,
            int(strength[-1][0]),
            method="PASSIVELY_MEASURED",
            avg_hr=110,
            zones=(1200, 0, 0, 0),
        ),
    )
    write_tcx(root, date(2026, 11, 17), "str1", tcx_bytes(strength_start, strength, gps=False))
    card_start = utc("2026-11-18T18:00:00")
    add(
        date(2026, 11, 18),
        workout(
            "card1",
            "CARDIO_WORKOUT",
            card_start,
            900,
            method="PASSIVELY_MEASURED",
            avg_hr=120,
            zones=(300, 600, 0, 0),
        ),
    )
    # Deliberately unrealistic, keep it: fetch today never stores an empty TCX export
    # (zero trackpoints are counted as "empty" and not written). This file tests that
    # derive survives one anyway, e.g. from older data. Do not "fix" it.
    write_tcx(root, date(2026, 11, 18), "card1", tcx_bytes(card_start, [], gps=False))
    add(
        date(2026, 11, 21),
        workout(
            "hcX",
            "WALKING",
            utc("2026-11-21T10:00:00"),
            1200,
            platform="HEALTH_CONNECT",
            distance_m=1500.0,
        ),
    )
    for day, items in workouts.items():
        write_json(root / "workouts" / f"{day}.json", {"date": day.isoformat(), "workouts": items})

    write_json(
        root / "sleep" / "2026-11-15.json",
        {
            "date": "2026-11-15",
            "sessions": [
                sleep_session("s15", utc("2026-11-14T22:30:00"), utc("2026-11-15T06:00:00")),
                sleep_session(
                    "nap15", utc("2026-11-15T13:00:00"), utc("2026-11-15T13:30:00"), main=False
                ),
            ],
        },
    )
    write_json(
        root / "sleep" / "2026-11-16.json",
        {
            "date": "2026-11-16",
            "sessions": [
                sleep_session("s16", utc("2026-11-15T22:00:00"), utc("2026-11-16T05:30:00")),
            ],
        },
    )
    write_json(
        root / "_meta.json",
        {
            "last_fetch_utc": "2026-11-23T04:10:00Z",
            "window": ["2026-11-21", "2026-11-23"],
            "api_errors": {},
            "incomplete_days": [],
            "tcx": {},
        },
    )
    return {"track_a_total_m": track_a[-1][2], "strength_points": strength}
