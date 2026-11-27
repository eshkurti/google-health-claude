"""Derive analysis tables (CSV, optional SQLite) from the raw fetched files.

Reads ``daily/``, ``sleep/``, ``workouts/``, ``tcx/<date>/<id>.tcx.gz`` and
``_meta.json`` from the data folder and never writes there. No output contains
coordinates: TCX positions are only checked for presence.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .days import build_days
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


def resolve_data_dir(data_dir: Path) -> Path:
    """Accept the data repo root or its ``data/`` folder."""
    data_dir = Path(data_dir)
    for candidate in (data_dir, data_dir / "data"):
        if any((candidate / sub).is_dir() for sub in ("daily", "sleep", "workouts")):
            return candidate
    raise FileNotFoundError(f"no daily/, sleep/ or workouts/ folder under {data_dir}")


def default_as_of(data_dir: Path) -> date:
    """The Berlin date of the last fetch (days up to it may still change); else today."""
    meta_path = Path(data_dir) / "_meta.json"
    if meta_path.exists():
        moment = timestamp(json.loads(meta_path.read_text(encoding="utf-8")).get("last_fetch_utc"))
        if moment is not None:
            return moment.astimezone(TZ).date()
    return datetime.now(TZ).date()


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
    as_of = as_of or default_as_of(data_dir)
    report = DeriveReport(as_of=as_of)

    workouts = build_workouts(data_dir, report.warnings.append)
    tables = {
        "workouts": workouts.workouts,
        "run_splits": workouts.run_splits,
        "run_facts": workouts.run_facts,
        "personal_bests": workouts.personal_bests,
        **build_days(data_dir, workouts, as_of),
    }
    report.files_changed = write_csvs(out_dir, tables)
    report.notes_created = ensure_notes(out_dir)
    if sqlite_path is not None:
        tables["exercise_points"] = workouts.exercise_points()
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
