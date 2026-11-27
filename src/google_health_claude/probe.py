"""`probe`: try every data type once for a single day and report what works.

Use it before the first real fetch. It prints one line per data type and can
save the raw responses so the data format can be inspected. Saved samples
contain personal health data, so keep them out of git.

`probe-tcx`: export three stored workouts (GPS run, strength, passive walk) as
TCX and report the file structure only (status, size, trackpoint counts, which
fields are present, sampling interval, tag names). Never prints coordinates.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .api.client import HealthApiError
from .datatypes import LIST_SPECS, ROLLUP_SPECS
from .fetch import list_for_day
from .tcx import TARGETS, Workout, load_workouts, pick_workouts, summarize_tcx


def probe(client: Any, day: date, save_dir: Path | None = None) -> int:  # noqa: C901 - split in refactor session
    """Returns the number of data types that failed."""
    failures = 0

    def out(status: str, name: str, detail: str = "") -> None:
        print(f"{status:<6} {name:<38} {detail}")

    def save(name: str, payload: Any) -> None:
        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)
            (save_dir / f"{name}.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )

    try:
        identity = client.identity()
        out("OK", "identity", "account is linked to Google Health")
        save("identity", identity)
    except HealthApiError as err:
        out("FAIL", "identity", str(err))
        failures += 1

    print(f"\nProbing {day} ...\n")
    variant_cache: dict[str, int] = {}
    for spec in LIST_SPECS:
        try:
            points = list_for_day(client, spec, day, variant_cache)
        except HealthApiError as err:
            out("FAIL", spec.data_type, str(err))
            failures += 1
            continue
        spelling = ("snake_case", "camelCase")[variant_cache.get(spec.data_type, 0)]
        out(
            "OK" if points else "EMPTY",
            spec.data_type,
            f"{len(points)} point(s), filter {spelling}",
        )
        if points:
            save(spec.data_type, points)

    for rollup in ROLLUP_SPECS:
        try:
            points = client.daily_rollup(rollup.data_type, day, day + timedelta(days=1))
        except HealthApiError as err:
            out("FAIL", rollup.data_type + " (rollup)", str(err))
            failures += 1
            continue
        out("OK" if points else "EMPTY", rollup.data_type + " (rollup)", f"{len(points)} point(s)")
        if points:
            save(rollup.data_type + "_rollup", points)

    print(
        "\nrefresh_token_expires_in reported by Google:",
        client.refresh_token_expires_in,
        file=sys.stdout,
    )
    return failures


# -- probe-tcx ------------------------------------------------------------------

ERROR_BODY_LIMIT = 2000


def _error_body(content: bytes) -> str:
    text = " ".join(content.decode("utf-8", errors="replace").split())
    return text if len(text) <= ERROR_BODY_LIMIT else text[:ERROR_BODY_LIMIT] + "..."


def _tcx_attempt(
    client: Any, workout: Workout, label: str, partial: bool, save_dir: Path
) -> tuple[bool, int]:
    """One export call. Returns (HTTP success, trackpoint count). Prints no values."""
    name = "partialData=true" if partial else "default"
    try:
        reply = client.export_exercise_tcx(workout.exercise_id, partial_data=partial)
    except HealthApiError as err:
        print(f"  [{name}] request failed: {err}")
        return False, 0

    ok = 200 <= reply.status < 300
    save_dir.mkdir(parents=True, exist_ok=True)
    suffix = (".partialData" if partial else "") + (".tcx" if ok else ".error.txt")
    path = save_dir / f"{label}{suffix}"
    path.write_bytes(reply.content)

    print(f"  [{name}] HTTP {reply.status}, {len(reply.content)} bytes, saved to {path}")
    if not ok:
        print(f"    error body: {_error_body(reply.content) or '(empty)'}")
        return False, 0

    s = summarize_tcx(reply.content)
    if s.error:
        print(f"    {s.error}")
        return True, 0
    n = s.trackpoints
    print(f"    trackpoints: {n}")
    print(
        f"    HeartRateBpm: {s.with_heart_rate}/{n}, Position: {s.with_position}/{n}, AltitudeMeters: {s.with_altitude}/{n}"
    )
    print(f"    interval between points: {s.interval_text()}")
    print(f"    tags: {', '.join(s.tags)}")
    return True, n


def probe_tcx(client: Any, data_dir: Path, save_dir: Path) -> int:
    """Export three sample workouts as TCX and describe their structure.

    Returns the number of workouts that could not be found or whose final
    export call failed. Coordinates and other values are never printed.
    """
    picked = pick_workouts(load_workouts(data_dir))
    failures = 0
    for target in TARGETS:
        workout = picked[target.label]
        print(f"\n{target.label}: {target.description}")
        if workout is None:
            print("  SKIP: no matching workout with a name in the data repo")
            failures += 1
            continue
        print(f"  stored under {workout.file_date}")
        ok, points = _tcx_attempt(client, workout, target.label, False, save_dir)
        if points == 0:
            ok, points = _tcx_attempt(client, workout, target.label, True, save_dir)
        failures += not ok
    return failures
