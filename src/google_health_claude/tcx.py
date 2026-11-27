"""Helpers for `probe-tcx`: pick sample workouts from the stored workout files.

Nothing here touches the network. The TCX summary it prints comes from
fetch/tcx.py, which also uses it to count trackpoints before storing a file.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .records import exercise_id, parse_time

# Field names below come from the schema notes written after the first real
# fetch (dataSource.platform / recordingMethod, exercise.exerciseType).
Matcher = Callable[[dict[str, Any]], bool]


def _source(w: dict[str, Any]) -> dict[str, Any]:
    return w.get("dataSource") or {}


def _exercise(w: dict[str, Any]) -> dict[str, Any]:
    return w.get("exercise") or {}


def _is(
    w: dict[str, Any], exercise_type: str, platform: str | None = None, method: str | None = None
) -> bool:
    return (
        _exercise(w).get("exerciseType") == exercise_type
        and (platform is None or _source(w).get("platform") == platform)
        and (method is None or _source(w).get("recordingMethod") == method)
    )


@dataclass(frozen=True)
class Target:
    label: str
    description: str
    match: Matcher
    # Tried first; if nothing matches, ``match`` alone decides.
    prefer: Matcher | None = None


TARGETS: tuple[Target, ...] = (
    Target(
        "running",
        "latest FITBIT ACTIVELY_MEASURED RUNNING",
        lambda w: _is(w, "RUNNING", "FITBIT", "ACTIVELY_MEASURED"),
    ),
    Target(
        "strength",
        "latest STRENGTH_TRAINING (FITBIT preferred)",
        lambda w: _is(w, "STRENGTH_TRAINING"),
        prefer=lambda w: _source(w).get("platform") == "FITBIT",
    ),
    Target(
        "walking",
        "latest FITBIT PASSIVELY_MEASURED WALKING",
        lambda w: _is(w, "WALKING", "FITBIT", "PASSIVELY_MEASURED"),
    ),
)


@dataclass(frozen=True)
class Workout:
    exercise_id: str
    file_date: str
    start: str
    end: str = ""


def workouts_dir(data_dir: Path) -> Path:
    """Accept either the data repo root or its ``data/`` folder."""
    data_dir = Path(data_dir)
    for candidate in (data_dir / "workouts", data_dir / "data" / "workouts"):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"no workouts/ folder under {data_dir}")


def load_workouts(data_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    """(file date, workout) for every stored workout, oldest file first."""
    result = []
    for path in sorted(workouts_dir(data_dir).glob("*.json")):
        body = json.loads(path.read_text(encoding="utf-8"))
        for item in body.get("workouts", []) if isinstance(body, dict) else []:
            if isinstance(item, dict):
                result.append((path.stem, item))
    return result


def pick_workouts(stored: list[tuple[str, dict[str, Any]]]) -> dict[str, Workout | None]:
    """The latest workout for each target, by start time (file date as fallback)."""

    def sort_key(entry: tuple[str, dict[str, Any]]) -> tuple[str, float]:
        file_date, w = entry
        start = parse_time((_exercise(w).get("interval") or {}).get("startTime"))
        return (file_date, start.timestamp() if start else float("-inf"))

    picked: dict[str, Workout | None] = {}
    for target in TARGETS:
        candidates = [
            e
            for e in stored
            if target.match(e[1]) and isinstance(e[1].get("name"), str) and e[1]["name"]
        ]
        if target.prefer:
            preferred = [e for e in candidates if target.prefer(e[1])]
            candidates = preferred or candidates
        if not candidates:
            picked[target.label] = None
            continue
        file_date, w = max(candidates, key=sort_key)
        interval = _exercise(w).get("interval") or {}
        picked[target.label] = Workout(
            exercise_id(w["name"]),
            file_date,
            str(interval.get("startTime") or ""),
            str(interval.get("endTime") or ""),
        )
    return picked
