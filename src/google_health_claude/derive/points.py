"""TCX trackpoints as plain values, without coordinates, and statistics over them.

Sampling is not regular: GPS runs step mostly 1 s but have gaps, and passive
sessions mix 1, 2 and 3 s steps. So every statistic here is computed from
timestamps, never from point counts:

* heart rate is time-weighted: each sample holds until the next one, and steps
  longer than ``HR_MAX_STEP_S`` carry no weight (no data, nothing assumed);
* GPS segments break wherever positioned points are more than ``GPS_GAP_S``
  apart; speed, walking time and smoothed altitude never reach across a break.

Latitude and longitude are never read: only whether a point has a position.
"""

from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .values import local_iso, num, timestamp

HR_MAX_STEP_S = 10.0
GPS_GAP_S = 5.0
SPEED_HALF_WINDOW_S = 5.0
ALT_HALF_WINDOW_S = 15.0
WALK_SPEED_M_S = 6.5 / 3.6


@dataclass(frozen=True)
class Point:
    t: datetime
    ts: float  # epoch seconds, for arithmetic
    hr: float | None
    dist_m: float | None
    alt_m: float | None
    has_pos: bool


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def read_points(path: Path) -> list[Point] | None:
    """Trackpoints of a gzipped TCX file, sorted by time; None if unreadable."""
    try:
        root = ET.fromstring(gzip.decompress(Path(path).read_bytes()))
    except (OSError, EOFError, ET.ParseError):
        return None
    by_time: dict[float, Point] = {}
    for tp in root.iter():
        if _local(tp.tag) != "Trackpoint":
            continue
        fields: dict[str, ET.Element] = {}
        for child in tp:
            fields.setdefault(_local(child.tag), child)
        moment = timestamp(fields["Time"].text) if "Time" in fields else None
        if moment is None:
            continue
        hr_el = fields.get("HeartRateBpm")
        hr_value = next((c.text for c in hr_el if _local(c.tag) == "Value"), None) if hr_el is not None else None

        def value(name: str) -> float | None:
            el = fields.get(name)
            return num(el.text) if el is not None else None

        point = Point(moment, moment.timestamp(), num(hr_value), value("DistanceMeters"),
                      value("AltitudeMeters"), "Position" in fields)
        by_time.setdefault(point.ts, point)  # first point wins on a repeated timestamp
    return [by_time[ts] for ts in sorted(by_time)]


def hr_stats(points: list[Point], start: datetime | None = None, end: datetime | None = None
             ) -> tuple[float | None, float | None]:
    """(time-weighted average, maximum) heart rate within [start, end)."""
    lo = start.timestamp() if start else float("-inf")
    hi = end.timestamp() if end else float("inf")
    hr_points = [p for p in points if p.hr is not None]
    total = weight = 0.0
    peak: float | None = None
    for i, p in enumerate(hr_points):
        if lo <= p.ts < hi:
            peak = p.hr if peak is None else max(peak, p.hr)
        if i + 1 == len(hr_points):
            break
        nxt = hr_points[i + 1].ts
        if not 0 < nxt - p.ts <= HR_MAX_STEP_S:
            continue
        overlap = min(nxt, hi) - max(p.ts, lo)
        if overlap > 0:
            total += p.hr * overlap
            weight += overlap
    return (total / weight if weight else None), peak


def gps_segments(points: list[Point]) -> tuple[list[list[int]], float]:
    """Indices of positioned points with distance, split at gaps; and the total gap seconds."""
    segments: list[list[int]] = []
    current: list[int] = []
    gap_total = 0.0
    last: int | None = None
    for i, p in enumerate(points):
        if not p.has_pos or p.dist_m is None:
            continue
        if last is not None and p.ts - points[last].ts > GPS_GAP_S:
            gap_total += p.ts - points[last].ts
            segments.append(current)
            current = []
        current.append(i)
        last = i
    if current:
        segments.append(current)
    return segments, gap_total


def _windowed(points: list[Point], segment: list[int], half_window: float):
    """For each index of the segment: (first, last) indices within +-half_window seconds."""
    lo = hi = 0
    for k, idx in enumerate(segment):
        t = points[idx].ts
        while points[segment[lo]].ts < t - half_window:
            lo += 1
        while hi + 1 < len(segment) and points[segment[hi + 1]].ts <= t + half_window:
            hi += 1
        yield k, lo, hi


def segment_speeds(points: list[Point], segment: list[int]) -> dict[int, float | None]:
    speeds: dict[int, float | None] = {}
    for k, lo, hi in _windowed(points, segment, SPEED_HALF_WINDOW_S):
        a, b = points[segment[lo]], points[segment[hi]]
        dt = b.ts - a.ts
        speeds[segment[k]] = (b.dist_m - a.dist_m) / dt if dt > 0 else None
    return speeds


def segment_altitudes(points: list[Point], segment: list[int]) -> dict[int, float | None]:
    smoothed: dict[int, float | None] = {}
    for k, lo, hi in _windowed(points, segment, ALT_HALF_WINDOW_S):
        values = [points[segment[j]].alt_m for j in range(lo, hi + 1) if points[segment[j]].alt_m is not None]
        smoothed[segment[k]] = sum(values) / len(values) if values else None
    return smoothed


def walk_seconds(points: list[Point]) -> float | None:
    """Seconds spent below WALK_SPEED_M_S, counted only inside GPS segments."""
    segments, _ = gps_segments(points)
    if not segments:
        return None
    total = 0.0
    for segment in segments:
        speeds = segment_speeds(points, segment)
        for prev, idx in zip(segment, segment[1:]):
            speed = speeds[idx]
            if speed is not None and speed < WALK_SPEED_M_S:
                total += points[idx].ts - points[prev].ts
    return total


def hr_half_delta(points: list[Point]) -> float | None:
    """Second-half minus first-half average heart rate, halves split at half the TCX distance."""
    with_dist = [p for p in points if p.dist_m is not None]
    if len(with_dist) < 2:
        return None
    middle = (with_dist[0].dist_m + with_dist[-1].dist_m) / 2
    t_mid = next(p.t for p in with_dist if p.dist_m >= middle)
    first, _ = hr_stats(points, None, t_mid)
    second, _ = hr_stats(points, t_mid, None)
    return None if first is None or second is None else second - first


def point_rows(points: list[Point], start: datetime) -> list[dict]:
    """exercise_points rows (no coordinates): speed and altitude only inside GPS segments."""
    speeds: dict[int, float | None] = {}
    altitudes: dict[int, float | None] = {}
    for segment in gps_segments(points)[0]:
        speeds.update(segment_speeds(points, segment))
        altitudes.update(segment_altitudes(points, segment))
    return [
        {
            "t_s": p.ts - start.timestamp(),
            "time_local": local_iso(p.t),
            "hr_bpm": p.hr,
            "dist_m": p.dist_m,
            "alt_m": altitudes.get(i),
            "speed_m_s": speeds.get(i),
        }
        for i, p in enumerate(points)
    ]
