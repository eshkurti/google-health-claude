"""Download TCX files for stored workouts (runs after the exercise records are saved).

Layout under ``data_dir``::

    tcx/YYYY-MM-DD/<exercise id>.tcx.gz   raw exportExerciseTcx reply, gzipped
    tcx/YYYY-MM-DD/_end_times.json        {exercise id: workout endTime} of each stored file

Which workouts: platform FITBIT and either ACTIVELY_MEASURED, or PASSIVELY_MEASURED
with an exerciseType other than WALKING. Health Connect copies and passive walks
are skipped.

Rules:
* a 200 reply with zero trackpoints is not stored and counts as "empty";
* a stored file is not downloaded again unless the workout's endTime changed;
* transient errors are retried with backoff by the client; any failure is
  recorded and the run continues;
* gzip is written with a fixed header (mtime 0), so the same reply gives the
  same bytes and no git diff on the same Python and zlib build (the compressed
  stream can differ between builds; the content never does);
* logs name the date and exercise type only, never coordinates or record names.
"""

from __future__ import annotations

import gzip
import json
import re
import statistics
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..api.client import HealthApi, HealthApiError
from ..records import exercise_id, parse_time, tcx_eligible
from .store import day_path, load_json, write_bytes_if_changed, write_if_changed

Log = Callable[[str], None]

INDEX_NAME = "_end_times.json"
# Exercise ids become file names: refuse anything that could leave the folder.
SAFE_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,200}$")


@dataclass
class TcxReport:
    fetched: int = 0
    skipped: int = 0
    empty: int = 0
    failed: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    files_changed: int = 0

    def as_meta(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "skipped": self.skipped,
            "empty": self.empty,
            "failed": self.failed,
            "failures": sorted(self.failures, key=lambda f: (f["date"], f["exercise_id"])),
        }

    def fail(self, day: date, ex_id: str, kind: str, status: int, error: str, log: Log) -> None:
        self.failed += 1
        self.failures.append(
            {
                "date": day.isoformat(),
                "exercise_id": ex_id,
                "exercise_type": kind,
                "status": status,
                "error": error,
            }
        )
        log(f"TCX failed for the {kind} workout on {day} (HTTP {status}): {error}")


def gzip_bytes(content: bytes) -> bytes:
    return gzip.compress(content, compresslevel=9, mtime=0)


def tcx_path(data_dir: Path, day: date, ex_id: str) -> Path:
    return Path(data_dir) / "tcx" / day.isoformat() / f"{ex_id}.tcx.gz"


def _reply_error(content: bytes) -> str:
    try:
        message = json.loads(content)["error"]["message"]
    except Exception:  # noqa: BLE001 - fall back to the raw text
        message = content.decode("utf-8", errors="replace")
    message = " ".join(str(message).split())
    return message if len(message) <= 300 else message[:300] + "..."


def export_tcx_for_days(client: HealthApi, data_dir: Path, days: list[date], log: Log) -> TcxReport:
    report = TcxReport()
    for day in days:
        _export_day(client, data_dir, day, report, log)
    log(
        f"TCX: {report.fetched} fetched, {report.skipped} skipped, "
        f"{report.empty} empty, {report.failed} failed."
    )
    return report


def _export_day(client: HealthApi, data_dir: Path, day: date, report: TcxReport, log: Log) -> None:
    """Every eligible workout stored for ``day``; the day's index is rewritten if it changed."""
    stored = load_json(day_path(data_dir, "workouts", day)) or {}
    index_path = Path(data_dir) / "tcx" / day.isoformat() / INDEX_NAME
    old_index: dict[str, str] = load_json(index_path) or {}
    index = dict(old_index)
    for workout in stored.get("workouts", []):
        if isinstance(workout, dict) and tcx_eligible(workout):
            _export_one(client, data_dir, day, workout, index, report, log)
    if index != old_index:
        report.files_changed += write_if_changed(index_path, index)


def _export_one(
    client: HealthApi,
    data_dir: Path,
    day: date,
    workout: dict[str, Any],
    index: dict[str, str],
    report: TcxReport,
    log: Log,
) -> None:
    """Download one workout's TCX unless it is stored for the same endTime."""
    exercise = workout.get("exercise") or {}
    kind = str(exercise.get("exerciseType") or "unknown type")
    ex_id = exercise_id(str(workout.get("name") or ""))
    end_time = str((exercise.get("interval") or {}).get("endTime") or "")

    if not SAFE_ID.match(ex_id):
        report.fail(day, "", kind, 0, "record name has no usable exercise id", log)
        return
    path = tcx_path(data_dir, day, ex_id)
    if path.exists() and index.get(ex_id) == end_time:
        report.skipped += 1
        return
    try:
        reply = client.export_exercise_tcx(ex_id, partial_data=True)
    except HealthApiError as err:  # network trouble after the client's retries
        report.fail(day, ex_id, kind, err.status, err.message, log)
        return
    if not 200 <= reply.status < 300:
        report.fail(day, ex_id, kind, reply.status, _reply_error(reply.content), log)
        return
    summary = summarize_tcx(reply.content)
    if summary.error:
        report.fail(day, ex_id, kind, reply.status, summary.error, log)
        return
    if summary.trackpoints == 0:
        report.empty += 1  # nothing recorded yet; tried again next run
        return
    report.files_changed += write_bytes_if_changed(path, gzip_bytes(reply.content))
    index[ex_id] = end_time
    report.fetched += 1


# -- TCX summary ----------------------------------------------------------------
# Counts, time intervals and XML tag names only, never values such as
# coordinates or heart rates, so probe-tcx can print it safely.


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass
class TcxSummary:
    trackpoints: int = 0
    with_heart_rate: int = 0
    with_position: int = 0
    with_altitude: int = 0
    intervals_s: list[float] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    error: str | None = None

    def interval_text(self) -> str:
        if not self.intervals_s:
            return "n/a (fewer than two timed trackpoints)"
        values = self.intervals_s
        return (
            f"median {statistics.median(values):g}s, min {min(values):g}s, "
            f"max {max(values):g}s over {len(values)} gaps"
        )


def summarize_tcx(content: bytes) -> TcxSummary:
    summary = TcxSummary()
    if not content.strip():
        summary.error = "empty body"
        return summary
    try:
        root = ET.fromstring(content)  # noqa: S314 - ElementTree never loads external entities and expat >= 2.4.1 refuses entity-expansion attacks; test_xml_entity_attacks_are_refused checks both
    except ET.ParseError as err:
        # The position in the message is line/column only, never body content.
        line, column = err.position
        summary.error = f"not valid XML (line {line}, column {column})"
        return summary

    summary.tags = sorted({_local(el.tag) for el in root.iter() if isinstance(el.tag, str)})
    previous: datetime | None = None
    for point in root.iter():
        if not isinstance(point.tag, str) or _local(point.tag) != "Trackpoint":
            continue
        summary.trackpoints += 1
        children = {
            _local(c.tag): c for c in point.iter() if isinstance(c.tag, str) and c is not point
        }
        summary.with_heart_rate += "HeartRateBpm" in children
        summary.with_position += "Position" in children
        summary.with_altitude += "AltitudeMeters" in children
        time_el = children.get("Time")
        current = parse_time(time_el.text) if time_el is not None else None
        if current and previous:
            summary.intervals_s.append((current - previous).total_seconds())
        if current:
            previous = current
    return summary
