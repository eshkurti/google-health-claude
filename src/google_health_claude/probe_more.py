"""`probe-more`: try data types the daily fetch does not use yet.

Terminal output only: nothing is written to disk. For each call it prints the
HTTP status, the number of points, the field names of one point and the
earliest/latest timestamps. It never prints raw values, coordinates or record
names, with two exceptions the probe asks for: max bpm and the median spacing
of heart-rate samples during one run.

Data type ids, record kinds and filter fields are from the Google Health data
types page (read Nov 2026). None of it is confirmed live except the snake_case
`.date` / `interval.civil_*` filters that `probe` already uses.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .client import HealthApiError
from .config import TIMEZONE
from .datatypes import _camel, _snake
from .tcx import load_workouts, parse_time, pick_workouts

# The API refusing a type is a finding, not a crash.
EXPECTED_REFUSALS = (400, 403, 404)

# How a list filter selects a time range, per record kind.
SAMPLE, INTERVAL, DAILY, SLEEP, ECG, NO_FILTER, ROLLUP = (
    "sample",
    "interval",
    "daily",
    "sleep",
    "ecg",
    "no-filter",
    "rollup",
)


@dataclass(frozen=True)
class TypeInfo:
    data_type: str
    kind: str
    note: str = ""


# Section 4: every other type readable with the nine read-only scopes. Write-only
# types (menstrual-period, moods, ovulation-test, symptoms) are left out.
OTHER_TYPES: tuple[TypeInfo, ...] = (
    TypeInfo("active-energy-burned", INTERVAL),
    TypeInfo("active-minutes", INTERVAL),
    TypeInfo("active-zone-minutes", INTERVAL),
    TypeInfo("activity-level", INTERVAL),
    TypeInfo("distance", INTERVAL),
    TypeInfo("sedentary-period", INTERVAL),
    TypeInfo("steps", INTERVAL),
    TypeInfo("swim-lengths-data", INTERVAL),
    TypeInfo("time-in-heart-rate-zone", INTERVAL),
    TypeInfo("exercise", INTERVAL),
    TypeInfo("blood-glucose", SAMPLE),
    TypeInfo("core-body-temperature", SAMPLE),
    TypeInfo("heart-rate-variability", SAMPLE),
    TypeInfo("height", SAMPLE),
    TypeInfo("oxygen-saturation", SAMPLE),
    TypeInfo("respiratory-rate-sleep-summary", SAMPLE),
    TypeInfo("vo2-max", SAMPLE),
    TypeInfo("skin-temperature-sensors", SAMPLE, "docs: v4beta only"),
    TypeInfo("daily-heart-rate-variability", DAILY),
    TypeInfo("daily-oxygen-saturation", DAILY),
    TypeInfo("daily-respiratory-rate", DAILY),
    TypeInfo("daily-resting-heart-rate", DAILY),
    TypeInfo("daily-sleep-temperature-derivations", DAILY),
    TypeInfo("daily-vo2-max", DAILY),
    TypeInfo("sleep", SLEEP),
    TypeInfo("electrocardiogram", ECG),
    TypeInfo("irregular-rhythm-notification", NO_FILTER, "docs: no filter field"),
    TypeInfo("food", NO_FILTER, "docs: no filter field"),
    TypeInfo("food-measurement-unit", NO_FILTER, "docs: no filter field"),
    TypeInfo("hydration-log", NO_FILTER, "docs: no filter field"),
    TypeInfo("nutrition-log", NO_FILTER, "docs: no filter field"),
    TypeInfo("basal-energy-burned", NO_FILTER, "in DataPoint schema, not in the types table"),
    TypeInfo("floors", ROLLUP, "docs: rollup only"),
    TypeInfo("total-calories", ROLLUP, "docs: rollup only"),
    TypeInfo("calories-in-heart-rate-zone", ROLLUP, "docs: rollup only"),
)

# The docs list no type with score, readiness, resilience, load, stress or
# cardio in its name. These are guesses: a 400/404 just means "not a type".
GUESSED_TYPES: tuple[TypeInfo, ...] = tuple(
    TypeInfo(name, NO_FILTER, "guess, not in docs")
    for name in (
        "daily-readiness",
        "daily-readiness-score",
        "readiness-score",
        "sleep-score",
        "daily-sleep-score",
        "stress-score",
        "daily-stress-score",
        "stress-management-score",
        "cardio-load",
        "daily-cardio-load",
        "training-load",
        "resilience",
        "daily-resilience",
        "cardio-fitness-score",
    )
)


# -- filters ------------------------------------------------------------------------


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), ZoneInfo(TIMEZONE))


def range_filters(info: TypeInfo, start: date, end_exclusive: date) -> list[str | None]:
    """Filter candidates for [start, end_exclusive), snake_case spelling first."""
    if info.kind == NO_FILTER:
        return [None]
    if info.kind == ECG:  # docs show only a lower bound on start_time
        return [f'electrocardiogram.interval.start_time >= "{_rfc3339(_midnight(start))}"']
    path = {
        SAMPLE: ("sample_time", "civil_time"),
        INTERVAL: ("interval", "civil_start_time"),
        DAILY: ("date",),
        SLEEP: ("interval", "civil_end_time"),
    }[info.kind]
    lo, hi = start.isoformat(), end_exclusive.isoformat()
    result: list[str | None] = []
    for convert in (_snake, _camel):
        field = ".".join([convert(info.data_type), *(convert(p) for p in path)])
        result.append(f'{field} >= "{lo}" AND {field} < "{hi}"')
    return result


def instant_filters(data_type: str, start: str, end: str) -> list[str | None]:
    """Sample filters on physical time between two RFC 3339 timestamps."""
    result: list[str | None] = []
    for convert in (_snake, _camel):
        field = ".".join([convert(data_type), convert("sample_time"), convert("physical_time")])
        result.append(f'{field} >= "{start}" AND {field} < "{end}"')
    return result


# -- value-free summaries ----------------------------------------------------------


def _walk(value: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """(dotted key path, leaf value) for every leaf; list items share a ``[]`` path."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child, f"{path}[]")
    else:
        yield path, value


def field_names(point: Any) -> list[str]:
    return sorted({path for path, _ in _walk(point)})


def _civil_date(value: Any) -> date | None:
    try:
        return date(int(value["year"]), int(value["month"]), int(value["day"]))
    except (KeyError, TypeError, ValueError):
        return None


def instants(point: Any) -> list[datetime]:
    """Physical timestamps in a point: string leaves whose key ends in 'Time'."""
    found = []
    for path, value in _walk(point):
        key = path.rsplit(".", 1)[-1].rstrip("[]")
        if key.endswith("Time") and not key.lower().startswith("civil"):
            moment = parse_time(value)
            if moment and moment.tzinfo:
                found.append(moment)
    return found


def _dates(point: Any) -> list[date]:
    found = []

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            if key == "date" and (d := _civil_date(value)):
                found.append(d)
                return
            for k, child in value.items():
                visit(child, k)
        elif isinstance(value, list):
            for child in value:
                visit(child, key)

    visit(point)
    return found


def time_range(points: list[Any]) -> str:
    moments = [m for p in points for m in instants(p)]
    if moments:
        return f"earliest {_rfc3339(min(moments))}, latest {_rfc3339(max(moments))}"
    days = [d for p in points for d in _dates(p)]
    if days:
        return f"earliest {min(days)}, latest {max(days)} (dates only)"
    return "no timestamps found"


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def max_bpm(points: list[Any]) -> float | None:
    values = [
        n
        for p in points
        for path, v in _walk(p)
        if path.rsplit(".", 1)[-1] == "beatsPerMinute" and (n := _number(v)) is not None
    ]
    return max(values) if values else None


def median_spacing_s(points: list[Any]) -> float | None:
    moments = sorted(min(ms) for p in points if (ms := instants(p)))
    gaps = [(b - a).total_seconds() for a, b in pairwise(moments)]
    return statistics.median(gaps) if gaps else None


def zone_boundaries(points: list[Any]) -> str:
    """How many points carry per-zone min/max bpm fields (docs: min/maxBeatsPerMinute)."""
    with_bounds = sum(
        any(
            name.rsplit(".", 1)[-1].lower().startswith(("min", "max", "lower", "upper"))
            and "beatsperminute" in name.lower()
            for name in field_names(p)
        )
        for p in points
    )
    return f"{'yes' if with_bounds else 'no'} ({with_bounds}/{len(points)} points)"


# -- calls --------------------------------------------------------------------------

Print = Callable[[str], None]


@dataclass
class Outcome:
    status: int | None
    points: list[Any]
    more_pages: bool = False
    error: str | None = None


def _list_all(client: Any, data_type: str, filters: list[str | None]) -> Outcome:
    """List with full pagination, trying filter spellings on HTTP 400."""
    last: HealthApiError | None = None
    for flt in filters:
        try:
            points = client.list_data_points(data_type, flt)
            return Outcome(client.last_status, points)
        except HealthApiError as err:
            last = err
            if err.status != 400:
                break
    if last is None:  # unreachable: callers always pass at least one filter
        raise RuntimeError(f"no filters to try for {data_type}")
    return Outcome(last.status, [], error=str(last))


def _one_call(client: Any, info: TypeInfo, start: date, end_exclusive: date) -> Outcome:
    """Exactly one request for the type (snake_case filter only)."""
    try:
        if info.kind == ROLLUP:
            points = client.daily_rollup(info.data_type, start, end_exclusive)
            return Outcome(client.last_status, points)
        body = client.list_page(info.data_type, range_filters(info, start, end_exclusive)[0])
        return Outcome(
            client.last_status, body.get("dataPoints", []), bool(body.get("nextPageToken"))
        )
    except HealthApiError as err:
        return Outcome(err.status, [], error=str(err))


def _report(out: Print, title: str, result: Outcome, details: bool = True) -> None:
    out(f"\n{title}")
    if result.error:
        out(f"  {result.error}")
        return
    more = " (first page only, more pages exist)" if result.more_pages else ""
    out(f"  HTTP {result.status}, {len(result.points)} points{more}")
    if details and result.points:
        out(f"  fields: {', '.join(field_names(result.points[0]))}")
        out(f"  {time_range(result.points)}")


def probe_more(client: Any, data_dir: Path, today: date, out: Print = print) -> int:
    """Returns the number of calls that failed for reasons other than 400/403/404."""
    outcomes: list[Outcome] = []
    end = today + timedelta(days=1)

    # 1. Heart-rate samples during the latest GPS run.
    out("== 1. heart-rate during the latest FITBIT ACTIVELY_MEASURED RUNNING ==")
    run = pick_workouts(load_workouts(data_dir))["running"]
    run_start = parse_time(run.start) if run is not None else None
    run_end = parse_time(run.end) if run is not None else None
    if run is None or run_start is None or run_end is None:
        out("  SKIP: no run with start and end time in the data repo")
    else:
        start_s, end_s = _rfc3339(run_start), _rfc3339(run_end)
        result = _list_all(client, "heart-rate", instant_filters("heart-rate", start_s, end_s))
        outcomes.append(result)
        _report(out, f"heart-rate (run stored under {run.file_date})", result)
        if result.points:
            bpm, gap = max_bpm(result.points), median_spacing_s(result.points)
            out(f"  max bpm: {'n/a' if bpm is None else f'{bpm:g}'}")
            out(f"  median seconds between samples: {'n/a' if gap is None else f'{gap:g}'}")

    # 2. Daily heart-rate zones, last 7 days.
    out("\n== 2. daily-heart-rate-zones, last 7 days ==")
    info = TypeInfo("daily-heart-rate-zones", DAILY)
    result = _list_all(client, info.data_type, range_filters(info, today - timedelta(days=6), end))
    outcomes.append(result)
    _report(out, info.data_type, result)
    if result.points:
        out(f"  zone boundaries present: {zone_boundaries(result.points)}")

    # 3. Body and fitness samples, last 90 days.
    out("\n== 3. weight, body-fat, altitude, run-vo2-max, last 90 days ==")
    for info in (
        TypeInfo("weight", SAMPLE),
        TypeInfo("body-fat", SAMPLE),
        TypeInfo("altitude", INTERVAL),
        TypeInfo("run-vo2-max", SAMPLE),
    ):
        result = _list_all(
            client, info.data_type, range_filters(info, today - timedelta(days=89), end)
        )
        outcomes.append(result)
        _report(out, info.data_type, result)

    # 4. Everything else, one call each, last 7 days: status and count only.
    out("\n== 4. other readable types, one call each, last 7 days ==")
    for info in (*OTHER_TYPES, *GUESSED_TYPES):
        result = _one_call(client, info, today - timedelta(days=6), end)
        outcomes.append(result)
        how = "dailyRollUp" if info.kind == ROLLUP else "list"
        label = f"{info.data_type} [{how}{', ' + info.note if info.note else ''}]"
        status = result.error or f"HTTP {result.status}, {len(result.points)} points" + (
            " (more pages exist)" if result.more_pages else ""
        )
        out(f"  {label:<62} {status}")

    return sum(1 for o in outcomes if o.error and o.status not in EXPECTED_REFUSALS)
