"""Fetch a range of days and store them as one JSON file per day and category.

Layout under ``data_dir``::

    daily/YYYY-MM-DD.json     {"date", "metrics": {data_type: ...}}  (weight: by sample date)
    sleep/YYYY-MM-DD.json     {"date", "sessions": [...]}   (keyed by wake-up date)
    workouts/YYYY-MM-DD.json  {"date", "workouts": [...]}
    tcx/YYYY-MM-DD/<id>.tcx.gz  TCX export of a workout (see workout_tcx.py)
    _meta.json                last fetch time, window, API errors of the last run, days to
                              retry, TCX counts and failures

Re-fetching is safe: files are rewritten only when their content changes, and new
results are merged into what is stored, so nothing fetched before is ever erased.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .client import HealthApiError
from .config import TIMEZONE
from .datatypes import LIST_SPECS, ROLLUP_SPECS, WEIGHT_DAYS, WEIGHT_SPEC, ListSpec, _camel
from .records import civil_date, parse_time
from .store import day_path, load_json, merge_items, write_if_changed
from .workout_tcx import TcxReport, export_tcx_for_days

Log = Callable[[str], None]

# The API refuses these for a data type for good (unsupported, no scope, bad
# filter): asking again on another day or run will not help.
PERMANENT_STATUSES = (400, 403, 404)


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


@dataclass
class FetchReport:
    requests_ok: int = 0
    requests_failed: int = 0
    files_changed: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    tcx: TcxReport = field(default_factory=TcxReport)


def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def chunks(start: date, end: date, size: int) -> list[tuple[date, date]]:
    """Split [start, end] (inclusive) into consecutive windows of at most ``size`` days."""
    result = []
    cursor = start
    while cursor <= end:
        last = min(cursor + timedelta(days=size - 1), end)
        result.append((cursor, last))
        cursor = last + timedelta(days=1)
    return result


def _sample_time(point: Any, data_type: str) -> dict:
    payload = point.get(_camel(data_type)) if isinstance(point, dict) else None
    sample = payload.get("sampleTime") if isinstance(payload, dict) else None
    return sample if isinstance(sample, dict) else {}


def sample_day(point: Any, data_type: str) -> date | None:
    """Civil date of a sample point: civilTime if given, else physicalTime in TIMEZONE."""
    sample = _sample_time(point, data_type)
    day = civil_date(sample.get("civilTime"))
    if day:
        return day
    moment = parse_time(sample.get("physicalTime"))
    if moment and moment.tzinfo:
        return moment.astimezone(ZoneInfo(TIMEZONE)).date()
    return None


def sample_key(data_type: str) -> Callable[[Any], str | None]:
    """Merge identity for sample lists: the physical timestamp of the sample."""

    def key(point: Any) -> str | None:
        stamp = _sample_time(point, data_type).get("physicalTime")
        return f"sampleTime:{stamp}" if isinstance(stamp, str) and stamp else None

    return key


# Lists merged by something other than the record name.
MERGE_KEYS: dict[str, Callable[[Any], str | None]] = {"weight": sample_key("weight")}


def list_for_day(
    client: Any, spec: ListSpec, day: date, variant_cache: dict[str, int]
) -> list[dict]:
    """List a day's points, trying the filter spellings until one is accepted."""
    return list_range(client, spec, day, day + timedelta(days=1), variant_cache)


def list_range(
    client: Any, spec: ListSpec, start: date, end_exclusive: date, variant_cache: dict[str, int]
) -> list[dict]:
    """List the points of [start, end_exclusive), trying the filter spellings."""
    candidates = spec.range_filter_candidates(start, end_exclusive)
    first = variant_cache.get(spec.data_type, 0)
    order = [first] + [i for i in range(len(candidates)) if i != first]
    last_error: HealthApiError | None = None
    for index in order:
        try:
            points = client.list_data_points(spec.data_type, candidates[index])
        except HealthApiError as err:
            if err.status != 400:
                raise
            last_error = err
            continue
        variant_cache[spec.data_type] = index
        return points
    if last_error is None:  # unreachable: every ListSpec yields two filter spellings
        raise RuntimeError(f"no filter candidates for {spec.data_type}")
    raise last_error


def fetch_range(  # noqa: C901 - split in refactor session
    client: Any,
    start: date,
    end: date,
    data_dir: Path,
    *,
    skip_existing: bool = False,
    log: Log = _stderr,
) -> FetchReport:
    """Fetch every configured data type for each day in [start, end]."""
    data_dir = Path(data_dir)
    report = FetchReport()
    days = days_between(start, end)
    metrics: dict[date, dict[str, Any]] = {day: {} for day in days}
    sleep: dict[date, list] = {}
    workouts: dict[date, list] = {}

    meta_path = data_dir / "_meta.json"
    old_meta = load_json(meta_path) or {}
    # Days whose per-day lists failed for a transient reason in an earlier run.
    # `backfill` must not skip them just because a daily file exists.
    retry_days: set[str] = set(old_meta.get("incomplete_days", []))
    attempted: set[date] = set()
    incomplete: set[date] = set()

    def record_error(data_type: str, err: Exception) -> None:
        report.requests_failed += 1
        report.errors.setdefault(data_type, str(err))

    # 1. Aggregated metrics: one request per window of days.
    for rollup in ROLLUP_SPECS:
        for window_start, window_end in chunks(start, end, rollup.max_days):
            try:
                points = client.daily_rollup(
                    rollup.data_type, window_start, window_end + timedelta(days=1)
                )
            except HealthApiError as err:
                record_error(rollup.data_type, err)
                break
            report.requests_ok += 1
            matched = 0
            for point in points:
                day = civil_date(point.get("civilStartTime"))
                if day in metrics:
                    metrics[day][rollup.data_type] = point
                    matched += 1
            if points and not matched:
                # Most likely the day field is not called civilStartTime (unverified).
                report.errors.setdefault(
                    rollup.data_type,
                    f"{len(points)} rollup point(s) had no civilStartTime date in "
                    f"{window_start}..{window_end}; nothing stored",
                )

    # 2. Per-day lists: daily summaries, sleep sessions and workouts.
    variant_cache: dict[str, int] = {}
    broken: set[str] = set()
    for day in days:
        if (
            skip_existing
            and day_path(data_dir, "daily", day).exists()
            and day.isoformat() not in retry_days
        ):
            continue
        attempted.add(day)
        for spec in LIST_SPECS:
            if spec.data_type in broken:
                continue
            try:
                points = list_for_day(client, spec, day, variant_cache)
            except HealthApiError as err:
                record_error(spec.data_type, err)
                if err.status in PERMANENT_STATUSES:
                    broken.add(spec.data_type)  # no point asking again for other days
                else:
                    incomplete.add(day)  # timeout, 429/5xx, odd reply: retry later
                continue
            report.requests_ok += 1
            if not points:
                continue
            if spec.category == "daily":
                metrics[day][spec.data_type] = points
            elif spec.category == "sleep":
                sleep[day] = points
            else:
                workouts[day] = points

    # 2b. Weight: weigh-ins are sparse, so list the last WEIGHT_DAYS days (or the
    # whole window if longer) on every run, in one paginated call.
    weight_start = min(start, end - timedelta(days=WEIGHT_DAYS - 1))
    try:
        points = list_range(
            client, WEIGHT_SPEC, weight_start, end + timedelta(days=1), variant_cache
        )
    except HealthApiError as err:
        record_error(WEIGHT_SPEC.data_type, err)
    else:
        report.requests_ok += 1
        undated = 0
        for point in points:
            day = sample_day(point, WEIGHT_SPEC.data_type)
            if day is None:
                undated += 1
                continue
            metrics.setdefault(day, {}).setdefault(WEIGHT_SPEC.data_type, []).append(point)
        if undated:
            report.errors.setdefault(
                WEIGHT_SPEC.data_type,
                f"{undated} weight point(s) had no sampleTime civilTime/physicalTime; not stored",
            )

    # 3. Save, merging into what is already stored. Weight can add days
    # outside the window; those files are merged the same way.
    for day in sorted(metrics):
        report.files_changed += _save_daily(data_dir, day, metrics[day])
    for day in days:
        report.files_changed += _save_list(data_dir, "sleep", "sessions", day, sleep.get(day))
        report.files_changed += _save_list(data_dir, "workouts", "workouts", day, workouts.get(day))

    # 3b. TCX for the stored workouts of the window (also those stored earlier).
    report.tcx = export_tcx_for_days(client, data_dir, days, log)
    report.files_changed += report.tcx.files_changed

    # 4. Metadata, rewritten on every run (last_fetch_utc always changes).
    retry_days -= {day.isoformat() for day in attempted}
    retry_days |= {day.isoformat() for day in incomplete}
    meta = {
        "last_fetch_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": [start.isoformat(), end.isoformat()],
        "api_errors": report.errors,
        "incomplete_days": sorted(retry_days),
        "tcx": report.tcx.as_meta(),
    }
    report.files_changed += write_if_changed(meta_path, meta)
    log(
        f"Fetched {start} .. {end}: {report.requests_ok} requests ok, "
        f"{report.requests_failed} failed, {report.files_changed} files changed."
    )
    return report


def _save_daily(data_dir: Path, day: date, new_metrics: dict[str, Any]) -> bool:
    if not new_metrics:
        return False
    path = day_path(data_dir, "daily", day)
    merged = dict((load_json(path) or {}).get("metrics", {}))
    for data_type, value in new_metrics.items():
        old = merged.get(data_type)
        # Listed summaries are merged point by point; a rollup is one point per
        # day, so the newer value (e.g. after a late sync) replaces the old one.
        if isinstance(old, list) and isinstance(value, list):
            merged[data_type] = merge_items(old, value, MERGE_KEYS.get(data_type))
        else:
            merged[data_type] = value
    return write_if_changed(path, {"date": day.isoformat(), "metrics": merged})


def _save_list(data_dir: Path, category: str, key: str, day: date, items: list | None) -> bool:
    if not items:
        return False
    path = day_path(data_dir, category, day)
    existing = (load_json(path) or {}).get(key, [])
    return write_if_changed(path, {"date": day.isoformat(), key: merge_items(existing, items)})
