"""Derive analysis tables (CSV, optional SQLite) from the raw fetched files.

``derive`` is three steps: ``load.load`` reads ``daily/``, ``sleep/``,
``workouts/``, ``tcx/<date>/<id>.tcx.gz`` and ``_meta.json`` (and never writes to
the data folder), ``build_tables`` turns them into rows without any I/O, and
``output`` writes the CSV and SQLite files. No output contains coordinates: TCX
positions are only checked for presence.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .days import build_days
from .load import RawData, load, resolve_data_dir
from .output import ensure_notes, read_notes, write_csvs, write_sqlite
from .values import TZ, timestamp
from .workouts import build_workouts


@dataclass
class DeriveReport:
    rows: dict[str, int] = field(default_factory=dict)
    files_changed: int = 0
    notes_created: bool = False
    as_of: date | None = None
    warnings: list[str] = field(default_factory=list)


def as_of_from(last_fetch_utc: Any) -> date | None:
    """The Berlin date of the last fetch (days up to it may still change), if it is known."""
    moment = timestamp(last_fetch_utc)
    return None if moment is None else moment.astimezone(TZ).date()


def build_tables(
    raw: RawData, as_of: date, warn: Callable[[str], None], points: bool = False
) -> dict[str, list[dict[str, Any]]]:
    """Every table's rows from the loaded files; ``points`` adds exercise_points (large)."""
    workouts = build_workouts(raw, warn)
    tables = {
        "workouts": workouts.workouts,
        "run_splits": workouts.run_splits,
        "run_facts": workouts.run_facts,
        "personal_bests": workouts.personal_bests,
        **build_days(raw, workouts, as_of),
    }
    if points:
        tables["exercise_points"] = workouts.exercise_points()
    return tables


def derive(
    data_dir: Path,
    out_dir: Path,
    sqlite_path: Path | None = None,
    as_of: date | None = None,
    log: Callable[[str], None] = lambda m: print(m, file=sys.stderr),
) -> DeriveReport:
    data_dir = resolve_data_dir(data_dir)
    out_dir = Path(out_dir)
    for label, target in (("--out", out_dir), ("--sqlite", sqlite_path)):
        if target is not None and (
            Path(target).resolve() == data_dir.resolve()
            or data_dir.resolve() in Path(target).resolve().parents
        ):
            raise ValueError(f"{label} must not be inside the data folder")
    raw = load(data_dir)
    as_of = as_of or as_of_from(raw.last_fetch_utc) or datetime.now(TZ).date()
    report = DeriveReport(as_of=as_of)

    tables = build_tables(raw, as_of, report.warnings.append, points=sqlite_path is not None)
    report.files_changed = write_csvs(out_dir, tables)
    report.notes_created = ensure_notes(out_dir)
    if sqlite_path is not None:
        write_sqlite(sqlite_path, tables, read_notes(out_dir))
    report.rows = {name: len(rows) for name, rows in tables.items()}

    for warning in report.warnings:
        log(f"warning: {warning}")
    log(
        f"Derived up to {report.as_of} (last 3 days partial): "
        + ", ".join(f"{name} {n}" for name, n in report.rows.items())
        + f"; {report.files_changed} files changed"
        + ("; notes.csv created" if report.notes_created else "")
    )
    return report
