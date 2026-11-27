"""Workouts: deduplicate the raw exercise records and build the workout tables.

Dedup rules (see the data repo's schema notes):
* only ``platform: FITBIT`` records become rows; HEALTH_CONNECT copies are
  counted in ``n_hc_copies`` of the Fitbit session they overlap most (same type
  preferred) and otherwise ignored;
* FITBIT records of the same exercise type whose intervals overlap are merged
  into one session, identified by the record that starts first.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import groupby
from math import fsum
from pathlib import Path
from statistics import median
from typing import Any

from ..records import exercise_id, tcx_eligible
from .points import (
    Point,
    gps_segments,
    hr_half_delta,
    hr_stats,
    point_rows,
    read_points,
    walk_seconds,
)
from .values import dig, local_date, local_iso, mean, minutes_between, num, ratio, scaled, timestamp

VO2_DUPLICATE_WINDOW = timedelta(minutes=5)
VO2_MATCH_SLACK = timedelta(minutes=5)
FULL_KM = 0.95  # a split at least this long counts as a full km
SLOW_KM_FACTOR = 1.2
PB_TARGETS_KM = (("5k", 5.0), ("10k", 10.0), ("half", 21.0975), ("marathon", 42.195))
ZONES = ("light", "moderate", "vigorous", "peak")


@dataclass
class Raw:
    id: str
    platform: str
    method: str
    type: str
    start: datetime
    end: datetime
    rec: dict[str, Any]

    @property
    def exercise(self) -> dict[str, Any]:
        return self.rec.get("exercise") or {}


@dataclass
class Session:
    primary: Raw
    members: list[Raw]
    hc_copies: int = 0
    tcx_path: Path | None = None
    points: list[Point] | None = None  # None: no readable TCX
    run_vo2max: float | None = None

    @property
    def span_end(self) -> datetime:
        return max(m.end for m in self.members)

    @property
    def needs_tcx(self) -> bool:
        return any(tcx_eligible(m.rec) for m in self.members)


@dataclass
class WorkoutTables:
    sessions: list[Session] = field(default_factory=list)
    workouts: list[dict[str, Any]] = field(default_factory=list)
    run_splits: list[dict[str, Any]] = field(default_factory=list)
    run_facts: list[dict[str, Any]] = field(default_factory=list)
    personal_bests: list[dict[str, Any]] = field(default_factory=list)

    def exercise_points(self) -> list[dict[str, Any]]:
        rows = []
        for s in self.sessions:
            for row in point_rows(s.points or [], s.primary.start):
                rows.append({"workout_id": s.primary.id, **row})
        return rows


# -- loading and dedup ------------------------------------------------------------


def load_raw(data_dir: Path) -> list[Raw]:
    seen: dict[str, Raw] = {}
    for path in sorted((Path(data_dir) / "workouts").glob("*.json")):
        body = json.loads(path.read_text(encoding="utf-8"))
        for rec in body.get("workouts", []) if isinstance(body, dict) else []:
            if not isinstance(rec, dict):
                continue
            ex_id = exercise_id(str(rec.get("name") or ""))
            start = timestamp(dig(rec, "exercise", "interval", "startTime"))
            end = timestamp(dig(rec, "exercise", "interval", "endTime"))
            if not ex_id or start is None or end is None:
                continue
            seen.setdefault(
                ex_id,
                Raw(
                    ex_id,
                    str(dig(rec, "dataSource", "platform") or ""),
                    str(dig(rec, "dataSource", "recordingMethod") or ""),
                    str(dig(rec, "exercise", "exerciseType") or ""),
                    start,
                    end,
                    rec,
                ),
            )
    return list(seen.values())


def _overlap_s(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    return (min(a_end, b_end) - max(a_start, b_start)).total_seconds()


def dedupe(raws: list[Raw]) -> list[Session]:
    fitbit = sorted(
        (r for r in raws if r.platform == "FITBIT"), key=lambda r: (r.type, r.start, r.id)
    )
    sessions: list[Session] = []
    for _, group in groupby(fitbit, key=lambda r: r.type):
        current: Session | None = None
        for r in group:
            if current is not None and r.start < current.span_end:
                current.members.append(r)
            else:
                current = Session(r, [r])
                sessions.append(current)
    for hc in (r for r in raws if r.platform == "HEALTH_CONNECT"):
        scored = [
            (
                (s.primary.type == hc.type),
                _overlap_s(s.primary.start, s.span_end, hc.start, hc.end),
                s,
            )
            for s in sessions
        ]
        scored = [entry for entry in scored if entry[1] > 0]
        if scored:
            max(scored, key=lambda e: (e[0], e[1]))[2].hc_copies += 1
    sessions.sort(key=lambda s: (s.primary.start, s.primary.id))
    return sessions


def tcx_index(data_dir: Path) -> dict[str, Path]:
    suffix = ".tcx.gz"
    return {
        p.name[: -len(suffix)]: p for p in sorted((Path(data_dir) / "tcx").glob(f"*/*{suffix}"))
    }


def attach_tcx(sessions: list[Session], data_dir: Path, warn: Callable[[str], None]) -> None:
    index = tcx_index(data_dir)
    for s in sessions:
        path = next((index[m.id] for m in [s.primary, *s.members] if m.id in index), None)
        if path is None:
            continue
        s.tcx_path = path
        s.points = read_points(path)
        if s.points is None:
            warn(
                f"unreadable TCX for the {s.primary.type} workout on {local_date(s.primary.start)}"
            )


def load_run_vo2(data_dir: Path) -> list[tuple[datetime, float]]:
    """run-vo2-max samples, with identical duplicates (same value within 5 min) merged."""
    samples = []
    for path in sorted((Path(data_dir) / "daily").glob("*.json")):
        body = json.loads(path.read_text(encoding="utf-8"))
        for point in dig(body, "metrics", "run-vo2-max") or []:
            moment = timestamp(dig(point, "runVo2Max", "sampleTime", "physicalTime"))
            value = num(dig(point, "runVo2Max", "runVo2Max"))
            if moment is not None and value is not None:
                samples.append((moment, value))
    merged: list[tuple[datetime, float]] = []
    for moment, value in sorted(samples):
        if merged and merged[-1][1] == value and moment - merged[-1][0] <= VO2_DUPLICATE_WINDOW:
            continue
        merged.append((moment, value))
    return merged


def attach_run_vo2(sessions: list[Session], samples: list[tuple[datetime, float]]) -> None:
    """Samples are stamped at the run's start: match each to the run whose interval holds it."""
    runs = [s for s in sessions if s.primary.type == "RUNNING"]
    for moment, value in samples:
        candidates = [s for s in runs if s.primary.start - VO2_MATCH_SLACK <= moment <= s.span_end]
        if candidates:
            min(
                candidates, key=lambda s: abs((moment - s.primary.start).total_seconds())
            ).run_vo2max = value


# -- rows ----------------------------------------------------------------------------


def _pace_min_km(seconds_per_meter: Any) -> float | None:
    value = num(seconds_per_meter)
    return value * 1000 / 60 if value else None


def workout_row(s: Session, data_dir: Path) -> dict[str, Any]:
    ex = s.primary.exercise
    summary = ex.get("metricsSummary") or {}
    zones = summary.get("heartRateZoneDurations") or {}
    zone_min = {z: scaled(zones.get(f"{z}Time"), 1 / 60) for z in ZONES}
    zone_total = (
        fsum(v for v in zone_min.values() if v is not None)
        if any(v is not None for v in zone_min.values())
        else None
    )
    hard = None
    if zone_total and zone_min["vigorous"] is not None and zone_min["peak"] is not None:
        hard = (zone_min["vigorous"] + zone_min["peak"]) / zone_total

    distance_km = scaled(summary.get("distanceMillimeters"), 1e-6)
    duration_min = scaled(ex.get("activeDuration"), 1 / 60)
    steps = num(summary.get("steps"))
    stride = scaled(dig(summary, "mobilityMetrics", "avgStrideLengthMillimeters"), 1e-3)
    if stride is None and distance_km and steps:
        stride = distance_km * 1000 / steps
    _, peak = hr_stats(s.points) if s.points else (None, None)

    return {
        "id": s.primary.id,
        "date": local_date(s.primary.start).isoformat(),
        "start_local": local_iso(s.primary.start),
        "end_local": local_iso(s.primary.end),
        "type": s.primary.type,
        "recording_method": s.primary.method,
        "is_training": s.primary.method == "ACTIVELY_MEASURED",
        "has_gps": s.points is not None and any(p.has_pos for p in s.points),
        "duration_min": duration_min,
        "elapsed_min": minutes_between(s.primary.start, s.primary.end),
        "distance_km": distance_km,
        "pace_min_km": _pace_min_km(summary.get("averagePaceSecondsPerMeter"))
        if distance_km
        else None,
        "avg_hr_bpm": num(summary.get("averageHeartRateBeatsPerMinute")),
        "peak_hr_bpm": peak,
        "kcal": num(summary.get("caloriesKcal")),
        "steps": steps,
        "cadence_spm": ratio(steps, duration_min) if steps else None,
        "stride_m": stride,
        "elevation_gain_m": scaled(summary.get("elevationGainMillimeters"), 1e-3),
        **{f"min_{z}": zone_min[z] for z in ZONES},
        "hard_share": hard,
        "azm_points": num(summary.get("activeZoneMinutes")),
        "run_vo2max": s.run_vo2max,
        "merged_ids": ";".join(sorted(m.id for m in s.members if m is not s.primary)) or None,
        "n_raw_merged": len(s.members),
        "n_hc_copies": s.hc_copies,
        "tcx_file": s.tcx_path.relative_to(data_dir).as_posix() if s.tcx_path else None,
    }


def split_rows(s: Session) -> list[dict[str, Any]]:
    raw_splits: Any = next(
        (m.exercise.get("splits") for m in [s.primary, *s.members] if m.exercise.get("splits")), []
    )
    parsed = []
    for split in raw_splits if isinstance(raw_splits, list) else []:
        start, end = timestamp(split.get("startTime")), timestamp(split.get("endTime"))
        if start is None:
            continue
        parsed.append((start, end, split))
    parsed.sort(key=lambda item: item[0])
    rows = []
    for km_no, (start, end, split) in enumerate(parsed, 1):
        distance_km = scaled(dig(split, "metricsSummary", "distanceMillimeters"), 1e-6)
        duration_s = num(split.get("activeDuration"))
        pace = _pace_min_km(dig(split, "metricsSummary", "averagePaceSecondsPerMeter"))
        if pace is None and duration_s and distance_km:
            pace = duration_s / distance_km / 60
        avg_hr, max_hr = hr_stats(s.points, start, end) if s.points and end else (None, None)
        rows.append(
            {
                "workout_id": s.primary.id,
                "km_no": km_no,
                "start_local": local_iso(start),
                "end_local": local_iso(end),
                "distance_km": distance_km,
                "duration_s": duration_s,
                "pace_min_km": pace,
                "avg_hr_bpm": avg_hr,
                "max_hr_bpm": max_hr,
            }
        )
    return rows


def pace_fade(splits: list[dict[str, Any]]) -> float | None:
    """Second-half minus first-half pace (s/km), halves by distance, from split active times."""
    raw = [(r["distance_km"], r["duration_s"]) for r in splits]
    if not raw or any(d is None or t is None or d <= 0 for d, t in raw):
        return None
    parts: list[tuple[float, float]] = raw  # checked above: no None, positive distances
    total_km = fsum(d for d, _ in parts)
    total_s = fsum(t for _, t in parts)
    half = total_km / 2
    covered = elapsed = 0.0
    for d, t in parts:
        if covered + d >= half:
            elapsed += t * (half - covered) / d  # within one split only
            break
        covered += d
        elapsed += t
    return (total_s - elapsed) / half - elapsed / half


def facts_row(
    s: Session,
    workout: dict[str, Any],
    splits: list[dict[str, Any]],
    previous: list[dict[str, Any]],
) -> dict[str, Any]:
    full = [
        r
        for r in splits
        if r["distance_km"] is not None
        and r["distance_km"] >= FULL_KM
        and r["pace_min_km"] is not None
    ]
    paces = [r["pace_min_km"] for r in full]
    typical = median(paces) if paces else None
    points = s.points or []
    _, gap_total = gps_segments(points)
    avg_hr = workout["avg_hr_bpm"]
    last = previous[-1]["avg_hr_bpm"] if previous else None
    start = s.primary.start
    window = [w["avg_hr_bpm"] for w in previous if timestamp_of(w) >= start - timedelta(days=28)]
    four_week = mean(window)
    return {
        "workout_id": s.primary.id,
        "date": workout["date"],
        "distance_km": workout["distance_km"],
        "first_km_pace_min_km": splits[0]["pace_min_km"] if splits and splits[0] in full else None,
        "fastest_km_pace_min_km": min(paces) if paces else None,
        "slowest_km_pace_min_km": max(paces) if paces else None,
        "pace_fade_s_per_km": pace_fade(splits),
        "slow_km_count": sum(p > SLOW_KM_FACTOR * typical for p in paces)
        if typical is not None
        else None,
        "hr_second_half_minus_first_half_bpm": hr_half_delta(points),
        "peak_hr_bpm": workout["peak_hr_bpm"],
        "gps_gap_total_s": gap_total,
        "walk_s": walk_seconds(points),
        "avg_hr_delta_vs_previous_run_bpm": avg_hr - last
        if avg_hr is not None and last is not None
        else None,
        "avg_hr_delta_vs_4wk_avg_bpm": avg_hr - four_week
        if avg_hr is not None and four_week is not None
        else None,
    }


def timestamp_of(workout: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(workout["start_local"])


def personal_bests(
    gps_runs: list[dict[str, Any]], splits: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {w["id"]: w for w in gps_runs}
    rows = []
    full = [
        r
        for r in splits
        if r["distance_km"] is not None
        and r["distance_km"] >= FULL_KM
        and r["pace_min_km"] is not None
    ]
    if full:
        best = min(full, key=lambda r: (r["pace_min_km"], r["workout_id"], r["km_no"]))
        rows.append(
            {
                "record": "best_1km_split",
                "target_km": 1.0,
                "workout_id": best["workout_id"],
                "date": by_id[best["workout_id"]]["date"],
                "distance_km": best["distance_km"],
                "duration_s": best["duration_s"],
                "pace_min_km": best["pace_min_km"],
            }
        )
    for label, target in PB_TARGETS_KM:
        qualifying = [
            w
            for w in gps_runs
            if w["distance_km"] is not None
            and w["distance_km"] >= target
            and w["pace_min_km"] is not None
        ]
        if not qualifying:
            continue
        best = min(qualifying, key=lambda w: (w["pace_min_km"], w["start_local"]))
        rows.append(
            {
                "record": f"best_run_{label}",
                "target_km": target,
                "workout_id": best["id"],
                "date": best["date"],
                "distance_km": best["distance_km"],
                "duration_s": best["duration_min"] * 60
                if best["duration_min"] is not None
                else None,
                "pace_min_km": best["pace_min_km"],
            }
        )
    return rows


def build_workouts(data_dir: Path, warn: Callable[[str], None]) -> WorkoutTables:
    data_dir = Path(data_dir)
    tables = WorkoutTables(dedupe(load_raw(data_dir)))
    attach_tcx(tables.sessions, data_dir, warn)
    attach_run_vo2(tables.sessions, load_run_vo2(data_dir))

    gps_runs: list[dict[str, Any]] = []
    for s in tables.sessions:
        row = workout_row(s, data_dir)
        tables.workouts.append(row)
        if row["type"] != "RUNNING" or not row["has_gps"]:
            continue
        splits = split_rows(s)
        tables.run_splits.extend(splits)
        previous = [w for w in gps_runs if w["avg_hr_bpm"] is not None]
        tables.run_facts.append(facts_row(s, row, splits, previous))
        gps_runs.append(row)
    tables.personal_bests = personal_bests(gps_runs, tables.run_splits)
    return tables
