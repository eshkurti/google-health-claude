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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .api.client import HealthApiError
from .records import exercise_id, tcx_eligible
from .store import day_path, load_json, write_bytes_if_changed, write_if_changed
from .tcx import summarize_tcx

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


def export_tcx_for_days(  # noqa: C901 - split in refactor session
    client: Any, data_dir: Path, days: list[date], log: Callable[[str], None]
) -> TcxReport:
    report = TcxReport()
    for day in days:
        stored = load_json(day_path(data_dir, "workouts", day)) or {}
        folder = Path(data_dir) / "tcx" / day.isoformat()
        index_path = folder / INDEX_NAME
        old_index = load_json(index_path) or {}
        index = dict(old_index)

        for workout in stored.get("workouts", []):
            if not isinstance(workout, dict) or not tcx_eligible(workout):
                continue
            exercise = workout.get("exercise") or {}
            kind = str(exercise.get("exerciseType") or "unknown type")
            ex_id = exercise_id(str(workout.get("name") or ""))
            end_time = str((exercise.get("interval") or {}).get("endTime") or "")

            def fail(
                status: int, error: str, safe_id: str = ex_id, day: date = day, kind: str = kind
            ) -> None:
                report.failed += 1
                report.failures.append(
                    {
                        "date": day.isoformat(),
                        "exercise_id": safe_id,
                        "exercise_type": kind,
                        "status": status,
                        "error": error,
                    }
                )
                log(f"TCX failed for the {kind} workout on {day} (HTTP {status}): {error}")

            if not SAFE_ID.match(ex_id):
                fail(0, "record name has no usable exercise id", safe_id="")
                continue
            path = tcx_path(data_dir, day, ex_id)
            if path.exists() and index.get(ex_id) == end_time:
                report.skipped += 1
                continue

            try:
                reply = client.export_exercise_tcx(ex_id, partial_data=True)
            except HealthApiError as err:  # network trouble after the client's retries
                fail(err.status, err.message)
                continue
            if not 200 <= reply.status < 300:
                fail(reply.status, _reply_error(reply.content))
                continue
            summary = summarize_tcx(reply.content)
            if summary.error:
                fail(reply.status, summary.error)
                continue
            if summary.trackpoints == 0:
                report.empty += 1  # nothing recorded yet; tried again next run
                continue
            report.files_changed += write_bytes_if_changed(path, gzip_bytes(reply.content))
            index[ex_id] = end_time
            report.fetched += 1

        if index != old_index:
            report.files_changed += write_if_changed(index_path, index)

    log(
        f"TCX: {report.fetched} fetched, {report.skipped} skipped, "
        f"{report.empty} empty, {report.failed} failed."
    )
    return report
