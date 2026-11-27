"""Pure helpers for raw Google Health records, shared by fetch, derive and the dev tools.

Nothing here does I/O or imports another layer: it only reads values out of
API objects (timestamps, civil dates, record names) and decides which
workouts have a TCX file worth downloading.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


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


def civil_date(civil_date_time: Any) -> date | None:
    """The date of a CivilDateTime (``{"date": {"year", "month", "day"}, ...}``)."""
    try:
        d = civil_date_time["date"]
        return date(int(d["year"]), int(d["month"]), int(d["day"]))
    except (KeyError, TypeError, ValueError):
        return None


def exercise_id(name: str) -> str:
    """Last path segment of a data point name (``users/.../dataPoints/<id>``)."""
    return name.rstrip("/").rsplit("/", 1)[-1]


def tcx_eligible(workout: dict[str, Any]) -> bool:
    """FITBIT workouts that are ACTIVELY_MEASURED, or PASSIVELY_MEASURED and not WALKING."""
    source = workout.get("dataSource") or {}
    exercise = workout.get("exercise") or {}
    if source.get("platform") != "FITBIT":
        return False
    method = source.get("recordingMethod")
    return method == "ACTIVELY_MEASURED" or (
        method == "PASSIVELY_MEASURED" and exercise.get("exerciseType") != "WALKING"
    )
