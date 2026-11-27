"""API-shaped records for a raw data folder, built from plain values.

One function per stored record type. Each takes numbers and times and returns
the object the way fetch stores it: numbers as strings where the API sends
strings, ``"123s"`` durations, UTC timestamps with a separate UTC offset, and
zero-valued zones and activity levels left out of their rollups.

Shared by the demo generator and the derive test fixtures, so both write the
same shapes. Every value comes from the caller and is invented.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

JsonDict = dict[str, Any]
# UTC offset in seconds of a moment, as the API reports it next to each timestamp.
OffsetOf = Callable[[datetime], int]

USER = "users/0000"  # real record names hold the Google user id; this one is invented
ZONES = ("LIGHT", "MODERATE", "VIGOROUS", "PEAK")


def z(moment: datetime) -> str:
    """RFC 3339 in UTC, to the second, as the API writes it."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ymd(day: date) -> dict[str, int]:
    return {"year": day.year, "month": day.month, "day": day.day}


def _offset(offset_of: OffsetOf, moment: datetime) -> str:
    return f"{offset_of(moment)}s"


def _name(data_type: str, point_id: str) -> str:
    return f"{USER}/dataTypes/{data_type}/dataPoints/{point_id}"


# -- daily rollups (one object per day) -------------------------------------------------


def _rollup(day: date, key: str, payload: JsonDict) -> JsonDict:
    return {
        "civilStartTime": {"date": ymd(day)},
        "civilEndTime": {"date": ymd(day + timedelta(days=1))},
        key: payload,
    }


def steps(day: date, count: int) -> JsonDict:
    return _rollup(day, "steps", {"countSum": str(count)})


def distance(day: date, millimeters: int) -> JsonDict:
    return _rollup(day, "distance", {"millimetersSum": str(millimeters)})


def active_energy_burned(day: date, kcal: float) -> JsonDict:
    return _rollup(day, "activeEnergyBurned", {"kcalSum": kcal})


def total_calories(day: date, kcal: float) -> JsonDict:
    return _rollup(day, "totalCalories", {"kcalSum": kcal})


def heart_rate(day: date, avg: float, low: float, high: float) -> JsonDict:
    return _rollup(
        day,
        "heartRate",
        {"beatsPerMinuteAvg": avg, "beatsPerMinuteMin": low, "beatsPerMinuteMax": high},
    )


def active_minutes(day: date, minutes_by_level: Mapping[str, int]) -> JsonDict:
    """Levels with zero minutes are left out, as the API does."""
    levels = [
        {"activityLevel": level, "activeMinutesSum": str(minutes)}
        for level, minutes in minutes_by_level.items()
        if minutes
    ]
    return _rollup(day, "activeMinutes", {"activeMinutesRollupByActivityLevel": levels})


def active_zone_minutes(day: date, fat_burn: int, cardio: int, peak: int) -> JsonDict:
    return _rollup(
        day,
        "activeZoneMinutes",
        {
            "sumInFatBurnHeartZone": str(fat_burn),
            "sumInCardioHeartZone": str(cardio),
            "sumInPeakHeartZone": str(peak),
        },
    )


def time_in_heart_rate_zone(day: date, seconds_by_zone: Mapping[str, int]) -> JsonDict:
    """Zones with zero seconds are left out, as the API does."""
    zones = [
        {"heartRateZone": zone, "duration": f"{seconds}s"}
        for zone, seconds in seconds_by_zone.items()
        if seconds
    ]
    return _rollup(day, "timeInHeartRateZone", {"timeInHeartRateZones": zones})


def sedentary_period(day: date, seconds: int) -> JsonDict:
    return _rollup(day, "sedentaryPeriod", {"durationSum": f"{seconds}s"})


# -- daily summaries (a list with one point per day) -----------------------------------------


def _summary(day: date, key: str, payload: JsonDict) -> list[JsonDict]:
    return [
        {
            "dataSource": {"platform": "FITBIT", "recordingMethod": "DERIVED"},
            key: {"date": ymd(day), **payload},
        }
    ]


def resting_heart_rate(day: date, bpm: int) -> list[JsonDict]:
    return _summary(day, "dailyRestingHeartRate", {"beatsPerMinute": str(bpm)})


def heart_rate_variability(
    day: date, avg_ms: float, deep_rmssd_ms: float, entropy: float, non_rem_bpm: int
) -> list[JsonDict]:
    return _summary(
        day,
        "dailyHeartRateVariability",
        {
            "averageHeartRateVariabilityMilliseconds": avg_ms,
            "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": deep_rmssd_ms,
            "entropy": entropy,
            "nonRemHeartRateBeatsPerMinute": str(non_rem_bpm),
        },
    )


def oxygen_saturation(
    day: date, avg: float, lower: float, upper: float, std: float
) -> list[JsonDict]:
    return _summary(
        day,
        "dailyOxygenSaturation",
        {
            "averagePercentage": avg,
            "lowerBoundPercentage": lower,
            "upperBoundPercentage": upper,
            "standardDeviationPercentage": std,
        },
    )


def respiratory_rate(day: date, breaths_per_minute: float) -> list[JsonDict]:
    return _summary(day, "dailyRespiratoryRate", {"breathsPerMinute": breaths_per_minute})


def sleep_temperature(
    day: date, nightly_c: float, baseline_c: float, relative_stddev_c: float
) -> list[JsonDict]:
    return _summary(
        day,
        "dailySleepTemperatureDerivations",
        {
            "nightlyTemperatureCelsius": nightly_c,
            "baselineTemperatureCelsius": baseline_c,
            "relativeNightlyStddev30dCelsius": relative_stddev_c,
        },
    )


def vo2_max(day: date, vo2: float, covariance: float, level: str) -> list[JsonDict]:
    return _summary(
        day,
        "dailyVo2Max",
        {"vo2Max": vo2, "vo2MaxCovariance": covariance, "cardioFitnessLevel": level},
    )


def heart_rate_zones(day: date, bounds: Sequence[tuple[str, int, int]]) -> list[JsonDict]:
    """``bounds``: (zone type, min bpm, max bpm) per zone."""
    return _summary(
        day,
        "dailyHeartRateZones",
        {
            "heartRateZones": [
                {
                    "heartRateZoneType": zone,
                    "minBeatsPerMinute": str(low),
                    "maxBeatsPerMinute": str(high),
                }
                for zone, low, high in bounds
            ]
        },
    )


# -- samples ---------------------------------------------------------------------------------


def run_vo2_max(value: float, stamp: datetime, *, offset_of: OffsetOf) -> JsonDict:
    return {
        "dataSource": {"platform": "FITBIT", "recordingMethod": "DERIVED"},
        "runVo2Max": {
            "runVo2Max": value,
            "sampleTime": {"physicalTime": z(stamp), "utcOffset": _offset(offset_of, stamp)},
        },
    }


def weight(
    point_id: str, platform: str, grams: int, stamp: datetime, *, offset_of: OffsetOf
) -> JsonDict:
    return {
        "name": _name("weight", point_id),
        "dataSource": {"platform": platform, "recordingMethod": "MANUAL"},
        "weight": {
            "weightGrams": grams,
            "sampleTime": {"physicalTime": z(stamp), "utcOffset": _offset(offset_of, stamp)},
        },
    }


# -- sleep -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """A typed time span: a sleep stage or a short awakening."""

    kind: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class SleepSummary:
    asleep_min: int
    awake_min: int
    in_period_min: int
    to_fall_asleep_min: int
    after_wake_up_min: int
    stages: Sequence[tuple[str, int, int]]  # (stage, minutes, count)


def sleep_session(
    sleep_id: str,
    bed: datetime,
    wake: datetime,
    stages: Sequence[Span],
    summary: SleepSummary,
    short_awakenings: Sequence[Span],
    *,
    main: bool,
    offset_of: OffsetOf,
) -> JsonDict:
    return {
        "name": _name("sleep", sleep_id),
        "dataSource": {"platform": "FITBIT", "recordingMethod": "DERIVED"},
        "sleep": {
            "type": "STAGES",
            "interval": {
                "startTime": z(bed),
                "endTime": z(wake),
                "startUtcOffset": _offset(offset_of, bed),
                "endUtcOffset": _offset(offset_of, wake),
            },
            "metadata": {"mainSleep": main, "processed": True},
            "summary": {
                "minutesAsleep": str(summary.asleep_min),
                "minutesAwake": str(summary.awake_min),
                "minutesInSleepPeriod": str(summary.in_period_min),
                "minutesToFallAsleep": str(summary.to_fall_asleep_min),
                "minutesAfterWakeUp": str(summary.after_wake_up_min),
                "stagesSummary": [
                    {"type": stage, "minutes": str(minutes), "count": str(count)}
                    for stage, minutes, count in summary.stages
                ],
            },
            "stages": [
                {
                    "type": s.kind,
                    "startTime": z(s.start),
                    "endTime": z(s.end),
                    "startUtcOffset": _offset(offset_of, s.start),
                    "endUtcOffset": _offset(offset_of, s.end),
                }
                for s in stages
            ],
            "shortAwakenings": [
                {"type": a.kind, "startTime": z(a.start), "endTime": z(a.end)}
                for a in short_awakenings
            ],
        },
    }


# -- workouts ----------------------------------------------------------------------------------


def splits(
    start: datetime, track: Sequence[tuple[float, float]], *, offset_of: OffsetOf
) -> list[JsonDict]:
    """Per-km splits from (seconds since start, metres) points.

    A split ends at the first point at or past each full km; the last one ends at
    the final point and is usually shorter than 1 km.
    """
    result: list[JsonDict] = []
    if not track:
        return result
    prev_t, prev_d, km = 0.0, 0.0, 1
    final = track[-1]
    for t, d in track:
        if d >= km * 1000 or (t, d) == final:
            seconds, metres = t - prev_t, d - prev_d
            if metres <= 0:
                break
            begin, end = start + timedelta(seconds=prev_t), start + timedelta(seconds=t)
            result.append(
                {
                    "splitType": "DISTANCE",
                    "startTime": z(begin),
                    "startUtcOffset": _offset(offset_of, begin),
                    "endTime": z(end),
                    "endUtcOffset": _offset(offset_of, end),
                    "activeDuration": f"{seconds:g}s",
                    "metricsSummary": {
                        "distanceMillimeters": round(metres * 1000),
                        "averagePaceSecondsPerMeter": seconds / metres,
                    },
                }
            )
            prev_t, prev_d, km = t, d, km + 1
    return result


def exercise(
    ex_id: str,
    ex_type: str,
    start: datetime,
    active_s: int,
    *,
    offset_of: OffsetOf,
    elapsed_s: int | None = None,
    platform: str = "FITBIT",
    method: str = "ACTIVELY_MEASURED",
    distance_m: float = 0.0,
    avg_hr: int | None = None,
    kcal: int | None = None,
    step_count: int | None = None,
    zone_s: Sequence[int] | None = None,
    azm: int | None = None,
    elevation_mm: float | None = None,
    split_list: Sequence[JsonDict] = (),
) -> JsonDict:
    """An exercise record. ``zone_s``: seconds in the light, moderate, vigorous, peak zones."""
    end = start + timedelta(seconds=active_s if elapsed_s is None else elapsed_s)
    summary: JsonDict = {
        "distanceMillimeters": round(distance_m * 1000),
        "averagePaceSecondsPerMeter": active_s / distance_m if distance_m else 0.0,
    }
    if step_count is not None:
        summary["steps"] = str(step_count)
    if avg_hr is not None:
        summary["averageHeartRateBeatsPerMinute"] = str(avg_hr)
    if kcal is not None:
        summary["caloriesKcal"] = kcal
    if zone_s is not None:
        summary["heartRateZoneDurations"] = {
            f"{name}Time": f"{s}s"
            for name, s in zip(("light", "moderate", "vigorous", "peak"), zone_s, strict=True)
        }
    if azm is not None:
        summary["activeZoneMinutes"] = str(azm)
    if elevation_mm is not None:
        summary["elevationGainMillimeters"] = elevation_mm
    record: JsonDict = {
        "exerciseType": ex_type,
        "displayName": ex_type.title(),
        "activeDuration": f"{active_s}s",
        "interval": {
            "startTime": z(start),
            "startUtcOffset": _offset(offset_of, start),
            "endTime": z(end),
            "endUtcOffset": _offset(offset_of, end),
        },
        "metricsSummary": summary,
    }
    if split_list:
        record["splits"] = list(split_list)
    return {
        "name": _name("exercise", ex_id),
        "dataSource": {"platform": platform, "recordingMethod": method},
        "exercise": record,
    }


# -- TCX ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Trackpoint:
    t_s: float  # seconds since the workout start
    hr: int | None = None
    dist_m: float | None = None
    lat: float | None = None
    lon: float | None = None
    alt_m: float | None = None


def tcx_document(start: datetime, points: Sequence[Trackpoint], sport: str = "Running") -> bytes:
    """A TCX file with one lap holding every trackpoint (the parts derive reads)."""
    rows = []
    for p in points:
        parts = [f"<Time>{z(start + timedelta(seconds=p.t_s))}</Time>"]
        if p.lat is not None and p.lon is not None:
            parts.append(
                f"<Position><LatitudeDegrees>{p.lat:.7f}</LatitudeDegrees>"
                f"<LongitudeDegrees>{p.lon:.7f}</LongitudeDegrees></Position>"
            )
        if p.alt_m is not None:
            parts.append(f"<AltitudeMeters>{p.alt_m:.1f}</AltitudeMeters>")
        if p.dist_m is not None:
            parts.append(f"<DistanceMeters>{p.dist_m:.1f}</DistanceMeters>")
        if p.hr is not None:
            parts.append(f"<HeartRateBpm><Value>{p.hr}</Value></HeartRateBpm>")
        rows.append("<Trackpoint>" + "".join(parts) + "</Trackpoint>")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
        f'<Activities><Activity Sport="{sport}"><Id>{z(start)}</Id><Lap StartTime="{z(start)}">'
        f"<TotalTimeSeconds>1</TotalTimeSeconds><DistanceMeters>0</DistanceMeters><Track>{''.join(rows)}</Track>"
        "</Lap></Activity></Activities></TrainingCenterDatabase>"
    )
    return xml.encode()
