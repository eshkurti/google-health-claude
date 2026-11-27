"""HealthData: the derived tables as typed rows, filtered by date.

Every call reads its CSV files again, so the answers follow the folder as
derive rewrites it; nothing is cached. Date ranges are inclusive at both ends,
and an open end (None) means "from the first day" or "to the last day".
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TypeVar

from ..derive.schema import (
    DAILY,
    PERSONAL_BESTS,
    QUALITY,
    RUN_FACTS,
    RUN_SPLITS,
    SLEEP,
    SLEEP_STAGES,
    WEEKLY,
    WORKOUTS,
    Table,
)
from .models import (
    Day,
    DayQuality,
    PersonalBest,
    RunFacts,
    RunSplit,
    SleepSession,
    SleepStage,
    Week,
    Workout,
)
from .reader import read_table

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Run:
    """A RUNNING workout with its splits and facts (GPS runs only have those)."""

    workout: Workout
    facts: RunFacts | None
    splits: tuple[RunSplit, ...]
    is_partial: bool | None  # from quality.csv for the run's date; None if that day has no row


@dataclass(frozen=True, slots=True)
class Night:
    """A sleep session with its stage segments."""

    sleep: SleepSession
    stages: tuple[SleepStage, ...]
    is_partial: bool | None  # from quality.csv for the wake-up date; None if that day has no row


@dataclass(frozen=True, slots=True)
class Freshness:
    """How far the derived data reaches, and which days may still change."""

    first_day: str | None
    last_day: str | None
    complete_through: str | None  # the last day that is not partial
    partial_days: tuple[str, ...]
    last_fetch_utc: str | None  # from _meta.json; None unless meta_path was given


def _check(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and start > end:
        raise ValueError(f"start {start} is after end {end}")


def _within(day: str | None, start: date | None, end: date | None) -> bool:
    """ISO date strings compare like dates."""
    if day is None:
        return False
    return (start is None or day >= start.isoformat()) and (end is None or day <= end.isoformat())


def _by(rows: Iterable[T], key: Callable[[T], str]) -> dict[str, list[T]]:
    grouped: dict[str, list[T]] = {}
    for row in rows:
        grouped.setdefault(key(row), []).append(row)
    return grouped


class HealthData:
    """Read-only access to a folder written by ``derive``."""

    def __init__(self, derived_dir: Path, meta_path: Path | None = None) -> None:
        self.derived_dir = Path(derived_dir)
        self.meta_path = None if meta_path is None else Path(meta_path)

    def _read(self, table: Table, make: Callable[..., T]) -> list[T]:
        return read_table(self.derived_dir / f"{table.name}.csv", table, make)

    def _partial(self) -> dict[str, bool | None]:
        return {q.date: q.is_partial for q in self._read(QUALITY, DayQuality)}

    def daily(self, start: date | None = None, end: date | None = None) -> list[Day]:
        _check(start, end)
        return [d for d in self._read(DAILY, Day) if _within(d.date, start, end)]

    def sleep(self, start: date | None = None, end: date | None = None) -> list[Night]:
        """Sleep sessions whose wake-up date is in the range, naps included (is_main)."""
        _check(start, end)
        sessions = [s for s in self._read(SLEEP, SleepSession) if _within(s.date, start, end)]
        stages = _by(self._read(SLEEP_STAGES, SleepStage), lambda st: st.sleep_id)
        partial = self._partial()
        return [
            Night(s, tuple(stages.get(s.id, ())), partial.get(s.date) if s.date else None)
            for s in sessions
        ]

    def runs(self, start: date | None = None, end: date | None = None) -> list[Run]:
        """RUNNING workouts in the range, auto-detected ones included (workout.is_training)."""
        _check(start, end)
        workouts = [
            w
            for w in self._read(WORKOUTS, Workout)
            if w.type == "RUNNING" and _within(w.date, start, end)
        ]
        facts = {f.workout_id: f for f in self._read(RUN_FACTS, RunFacts)}
        splits = _by(self._read(RUN_SPLITS, RunSplit), lambda sp: sp.workout_id)
        partial = self._partial()
        return [
            Run(
                w,
                facts.get(w.id),
                tuple(splits.get(w.id, ())),
                partial.get(w.date) if w.date else None,
            )
            for w in workouts
        ]

    def weekly(self, start: date | None = None, end: date | None = None) -> list[Week]:
        """Weeks that overlap the range (a week from Monday to Sunday)."""
        _check(start, end)
        return [
            w
            for w in self._read(WEEKLY, Week)
            if (start is None or (w.week_end is not None and w.week_end >= start.isoformat()))
            and (end is None or w.week_start <= end.isoformat())
        ]

    def personal_bests(self) -> list[PersonalBest]:
        return self._read(PERSONAL_BESTS, PersonalBest)

    def freshness(self) -> Freshness:
        days = self._read(QUALITY, DayQuality)
        settled = [q.date for q in days if q.is_partial is False]
        return Freshness(
            first_day=days[0].date if days else None,
            last_day=days[-1].date if days else None,
            complete_through=settled[-1] if settled else None,
            partial_days=tuple(q.date for q in days if q.is_partial),
            last_fetch_utc=self._last_fetch_utc(),
        )

    def _last_fetch_utc(self) -> str | None:
        if self.meta_path is None:
            return None
        meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        value = meta.get("last_fetch_utc") if isinstance(meta, dict) else None
        return value if isinstance(value, str) else None
