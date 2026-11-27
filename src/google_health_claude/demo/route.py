"""An invented running loop and a per-second simulation of runs over it.

The loop is a hand-drawn polygon of about 6.3 km in local metres around an
anchor on a lake shore in Slovenia, with a 35 m climb on the north side. It is
not a real route and is nowhere near any user of this project.

Only ``+ - * /`` and ``sqrt`` are used on floats here: IEEE 754 rounds those
exactly, so a seed gives the same bytes on every platform (``sin``, ``cos`` and
``log`` may differ in the last bit between C libraries, so they are avoided).
"""

from __future__ import annotations

import random
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from math import sqrt

from .shapes import Trackpoint

ANCHOR_LAT, ANCHOR_LON = 46.3612, 14.0838
M_PER_DEG_LAT = 111_132.0
M_PER_DEG_LON = 76_900.0  # 111_320 m x cos(46.36 deg), precomputed: no cos at run time

# (east, north) in metres from the anchor; the loop closes back on the first point.
WAYPOINTS: tuple[tuple[float, float], ...] = (
    (0.0, 0.0),
    (420.0, -40.0),
    (830.0, 30.0),
    (1120.0, 230.0),
    (1260.0, 560.0),
    (1210.0, 900.0),
    (980.0, 1180.0),
    (620.0, 1360.0),
    (200.0, 1420.0),
    (-230.0, 1380.0),
    (-640.0, 1250.0),
    (-960.0, 1010.0),
    (-1130.0, 680.0),
    (-1090.0, 330.0),
    (-830.0, 90.0),
    (-420.0, -30.0),
)
# (share of the loop, altitude in m): flat along the lake, one climb and descent.
PROFILE: tuple[tuple[float, float], ...] = (
    (0.0, 477.0),
    (0.40, 478.5),
    (0.47, 486.0),
    (0.56, 509.0),
    (0.62, 513.0),
    (0.70, 495.0),
    (0.76, 480.0),
    (1.0, 477.0),
)

WALK_SPEED = 1.45  # m/s during a walk break
WALK_BREAK_S = 60
MIN_RUN_SPEED = 1.95  # m/s: running never drops to walking pace by noise alone
DEVICE_DISTANCE_BIAS = 1.004  # the watch's GPS distance runs slightly long


def _interpolate(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    i = min(max(bisect_right(xs, x) - 1, 0), len(xs) - 2)
    span = xs[i + 1] - xs[i]
    share = (x - xs[i]) / span if span else 0.0
    return ys[i] + (ys[i + 1] - ys[i]) * share


class Loop:
    def __init__(self) -> None:
        points = [*WAYPOINTS, WAYPOINTS[0]]
        self.cumulative = [0.0]
        for (x0, y0), (x1, y1) in pairwise(points):
            self.cumulative.append(self.cumulative[-1] + sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2))
        self._east = [x for x, _ in points]
        self._north = [y for _, y in points]
        self.length = self.cumulative[-1]
        self._xs = [share * self.length for share, _ in PROFILE]
        self._alts = [alt for _, alt in PROFILE]

    def position(self, s: float) -> tuple[float, float]:
        """(east, north) in metres after ``s`` metres along the loop (laps repeat)."""
        s %= self.length
        return (
            _interpolate(self.cumulative, self._east, s),
            _interpolate(self.cumulative, self._north, s),
        )

    def altitude(self, s: float) -> float:
        return _interpolate(self._xs, self._alts, s % self.length)

    def grade_pct(self, s: float) -> float:
        return (self.altitude(s + 10.0) - self.altitude(s - 10.0)) / 20.0 * 100.0


LOOP = Loop()


def to_lat_lon(east: float, north: float) -> tuple[float, float]:
    return ANCHOR_LAT + north / M_PER_DEG_LAT, ANCHOR_LON + east / M_PER_DEG_LON


@dataclass(frozen=True)
class RunPlan:
    distance_m: float
    speed_m_s: float  # running speed on the flat
    fitness: float  # 0 at the start of the plan, 1 at the end: lowers heart rate a little
    walk_every_s: int | None = None  # run-walk: a 60 s walk break this often
    gap_s: tuple[int, int] | None = None  # no GPS fixes strictly between these seconds
    repeat_at_s: int | None = None  # the watch writes this second twice


@dataclass(frozen=True)
class RunTrack:
    points: list[Trackpoint]
    duration_s: int
    device_distance_m: float  # the watch's total distance (TCX)
    avg_hr: int
    peak_hr: int
    hr_seconds: list[int]  # heart rate of every second, for zone totals
    elevation_gain_m: float


def _jitter(rng: random.Random, width: float) -> float:
    return (rng.random() - 0.5) * width


def simulate_run(plan: RunPlan, rng: random.Random) -> RunTrack:
    """One point per second until the planned distance is covered."""
    s = gain = 0.0
    speed, hr = plan.speed_m_s * 0.8, 92.0
    points: list[Trackpoint] = []
    hr_seconds: list[int] = []
    t = 0
    while True:
        east, north = LOOP.position(s)
        lat, lon = to_lat_lon(east + _jitter(rng, 3.0), north + _jitter(rng, 3.0))
        point = Trackpoint(
            float(t),
            round(hr),
            round(s * DEVICE_DISTANCE_BIAS, 1),
            lat,
            lon,
            round(LOOP.altitude(s) + _jitter(rng, 0.6), 1),
        )
        hr_seconds.append(round(hr))
        if not (plan.gap_s and plan.gap_s[0] < t < plan.gap_s[1]):
            points.append(point)
            if t == plan.repeat_at_s:
                points.append(Trackpoint(point.t_s, round(hr) + 1, point.dist_m))
        if s >= plan.distance_m:
            break

        grade = LOOP.grade_pct(s)
        walking = (
            plan.walk_every_s is not None
            and t > 0
            and t % plan.walk_every_s >= plan.walk_every_s - WALK_BREAK_S
            and s < plan.distance_m - 500
        )
        if walking:
            target_speed = WALK_SPEED
        else:
            target_speed = max(plan.speed_m_s * (1 - 0.025 * grade), MIN_RUN_SPEED)
        speed += (target_speed + _jitter(rng, 0.3) - speed) * 0.3
        target_hr = 70 + 30 * speed + 3.5 * grade + 0.0015 * t - 4 * plan.fitness
        hr += (target_hr - hr) * 0.03 + _jitter(rng, 1.2)

        before = LOOP.altitude(s)
        s += speed
        gain += max(LOOP.altitude(s) - before, 0.0)
        t += 1

    return RunTrack(
        points=points,
        duration_s=t,
        device_distance_m=round(s * DEVICE_DISTANCE_BIAS, 1),
        avg_hr=round(sum(hr_seconds) / len(hr_seconds)),
        peak_hr=max(hr_seconds),
        hr_seconds=hr_seconds,
        elevation_gain_m=gain,
    )
