"""Helpers for `probe-tcx`: pick sample workouts and summarise a TCX file.

Nothing here touches the network. The summary holds only counts, time intervals
and XML tag names, never values such as coordinates or heart rates, so it is
safe to print and paste.
"""

from __future__ import annotations

import json
import statistics
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

# Field names below come from the schema notes written after the first real
# fetch (dataSource.platform / recordingMethod, exercise.exerciseType).
Matcher = Callable[[dict[str, Any]], bool]


def _source(w: dict[str, Any]) -> dict[str, Any]:
    return w.get("dataSource") or {}


def _exercise(w: dict[str, Any]) -> dict[str, Any]:
    return w.get("exercise") or {}


def _is(w: dict[str, Any], exercise_type: str, platform: str | None = None, method: str | None = None) -> bool:
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


def exercise_id(name: str) -> str:
    """Last path segment of a data point name (``users/.../dataPoints/<id>``)."""
    return name.rstrip("/").rsplit("/", 1)[-1]


def parse_time(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # Python 3.10 only accepts 3 or 6 fractional digits; drop the fraction.
        head, dot, rest = text.partition(".")
        if not dot:
            return None
        tz = next((rest[i:] for i, ch in enumerate(rest) if ch in "+-"), "")
        try:
            return datetime.fromisoformat(head + tz)
        except ValueError:
            return None


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
        start = parse_time(((_exercise(w).get("interval") or {}).get("startTime")))
        return (file_date, start.timestamp() if start else float("-inf"))

    picked: dict[str, Workout | None] = {}
    for target in TARGETS:
        candidates = [
            e for e in stored
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


# -- TCX summary ----------------------------------------------------------------


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
        root = ET.fromstring(content)
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
        children = {_local(c.tag): c for c in point.iter() if isinstance(c.tag, str) and c is not point}
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
