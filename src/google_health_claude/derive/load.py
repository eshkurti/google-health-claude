"""Every read of the data folder happens here; the rest of derive works on the values.

``load`` reads ``workouts/``, ``daily/`` and ``sleep/`` (each JSON file, in sorted
path order, non-date names included), the gzipped TCX bytes under
``tcx/<date>/<id>.tcx.gz`` and ``last_fetch_utc`` from ``_meta.json``. It decides
nothing about the content: which files count, and what a broken TCX means, are
decided by the pure code in workouts.py and days.py.

One exception keeps 0.1.0's behavior: a ``sleep/`` file whose name is not a date
is never used, so if it cannot be read it is skipped with a warning instead of
failing derive. In ``daily/`` and ``workouts/`` every file is used (daily files
of any name for run VO2 max), so an unreadable one still fails, as in 0.1.0.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

TCX_SUFFIX = ".tcx.gz"


@dataclass(frozen=True)
class JsonFile:
    stem: str  # file name without .json, e.g. "2026-11-23"
    body: Any  # the parsed JSON


@dataclass(frozen=True)
class TcxFile:
    path: str  # relative to the data folder, with forward slashes
    gz: bytes | None  # None: the file could not be read


@dataclass(frozen=True)
class RawData:
    workouts: tuple[JsonFile, ...] = ()
    daily: tuple[JsonFile, ...] = ()
    sleep: tuple[JsonFile, ...] = ()
    tcx: Mapping[str, TcxFile] = field(default_factory=dict)  # by exercise id
    last_fetch_utc: Any = None  # as stored in _meta.json (an RFC 3339 string if present)
    warnings: tuple[str, ...] = ()  # files skipped while reading


def date_of(stem: str) -> date | None:
    """The date a file is named after, or None: the one rule for "date-named" files."""
    try:
        return date.fromisoformat(stem)
    except ValueError:
        return None


def resolve_data_dir(data_dir: Path) -> Path:
    """Accept the data repo root or its ``data/`` folder."""
    data_dir = Path(data_dir)
    for candidate in (data_dir, data_dir / "data"):
        if any((candidate / sub).is_dir() for sub in ("daily", "sleep", "workouts")):
            return candidate
    raise FileNotFoundError(f"no daily/, sleep/ or workouts/ folder under {data_dir}")


def _json_files(folder: Path, skipped: list[str] | None = None) -> tuple[JsonFile, ...]:
    """Every *.json file; with ``skipped``, unreadable files without a date name are
    left out and reported there instead of raising."""
    files = []
    for path in sorted(folder.glob("*.json")):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # ValueError: bad JSON or bad UTF-8
            if skipped is None or date_of(path.stem) is not None:
                raise
            skipped.append(
                f"unreadable {folder.name} file {path.name} skipped (not named after a date)"
            )
            continue
        files.append(JsonFile(path.stem, body))
    return tuple(files)


def _tcx_files(data_dir: Path) -> dict[str, TcxFile]:
    """By exercise id; if two date folders hold the same id, the later path wins."""
    files = {}
    for path in sorted((data_dir / "tcx").glob(f"*/*{TCX_SUFFIX}")):
        try:
            gz: bytes | None = path.read_bytes()
        except OSError:
            gz = None
        files[path.name[: -len(TCX_SUFFIX)]] = TcxFile(path.relative_to(data_dir).as_posix(), gz)
    return files


def _last_fetch_utc(data_dir: Path) -> Any:
    meta_path = data_dir / "_meta.json"
    if not meta_path.exists():
        return None
    return json.loads(meta_path.read_text(encoding="utf-8")).get("last_fetch_utc")


def load(data_dir: Path) -> RawData:
    """Read a data folder (already resolved with resolve_data_dir)."""
    data_dir = Path(data_dir)
    skipped: list[str] = []
    return RawData(
        workouts=_json_files(data_dir / "workouts"),
        daily=_json_files(data_dir / "daily"),
        sleep=_json_files(data_dir / "sleep", skipped),
        tcx=_tcx_files(data_dir),
        last_fetch_utc=_last_fetch_utc(data_dir),
        warnings=tuple(skipped),
    )
