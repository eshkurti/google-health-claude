"""Small conversions shared by the derive modules.

The raw files keep the API's own encodings: numbers may arrive as JSON strings
("8165"), durations as strings in seconds with an ``s`` suffix ("1422.323s"),
timestamps in UTC. Everything here turns those into plain Python values and
returns ``None`` for anything missing or malformed, so missing data stays empty
in the output instead of becoming 0.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import median
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from ..config import TIMEZONE
from ..tcx import parse_time

TZ = ZoneInfo(TIMEZONE)


def num(value: Any) -> float | None:
    """A number from an int, a float, a numeric string or a ``"123s"`` duration."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("s"):
            text = text[:-1]
        try:
            return float(text)
        except ValueError:
            return None
    return None


def dig(obj: Any, *path: str) -> Any:
    """``obj[path[0]][path[1]]...`` or None if any step is missing."""
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def scaled(value: Any, factor: float) -> float | None:
    n = num(value)
    return None if n is None else n * factor


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return numerator / denominator


def timestamp(text: Any) -> datetime | None:
    """An aware datetime from an RFC 3339 string; None if it has no offset."""
    moment = parse_time(text)
    return moment if moment is not None and moment.tzinfo is not None else None


def local(moment: datetime | None) -> datetime | None:
    return None if moment is None else moment.astimezone(TZ)


def local_iso(moment: datetime | None) -> str | None:
    """ISO 8601 in Europe/Berlin with its offset, to the second."""
    return None if moment is None else local(moment).replace(microsecond=0).isoformat()


def local_date(moment: datetime | None) -> date | None:
    return None if moment is None else local(moment).date()


def ymd(d: Any) -> date | None:
    """A date from ``{"year", "month", "day"}``."""
    try:
        return date(int(d["year"]), int(d["month"]), int(d["day"]))
    except (KeyError, TypeError, ValueError):
        return None


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def minutes_between(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 60


def mean(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def weighted_mean(pairs: Iterable[tuple[float | None, float | None]]) -> float | None:
    """Mean of values weighted by the second element; pairs with a None are skipped."""
    total = weight = 0.0
    for value, w in pairs:
        if value is None or not w:
            continue
        total += value * w
        weight += w
    return total / weight if weight else None


def median_or_none(values: list[float]) -> float | None:
    return median(values) if values else None
