"""derive/load.py: the only reader of the data folder (all values invented)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from google_health_claude.derive.load import JsonFile, RawData, TcxFile, load, resolve_data_dir


def write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_json_files_come_in_sorted_order_with_non_date_names(tmp_path: Path) -> None:
    for stem in ("2026-11-02", "notes", "2026-11-01", "2026-10-31"):
        write(tmp_path / "daily" / f"{stem}.json", {"date": stem})
    write(tmp_path / "sleep" / "2026-11-01.json", {"sessions": []})
    write(tmp_path / "workouts" / "2026-11-01.json", [1, 2])
    (tmp_path / "daily" / "readme.txt").write_text("not json", encoding="utf-8")

    raw = load(tmp_path)

    assert [f.stem for f in raw.daily] == ["2026-10-31", "2026-11-01", "2026-11-02", "notes"]
    assert raw.daily[3] == JsonFile("notes", {"date": "notes"})
    assert raw.sleep == (JsonFile("2026-11-01", {"sessions": []}),)
    assert raw.workouts == (JsonFile("2026-11-01", [1, 2]),)  # shape is checked later


def test_tcx_bytes_by_exercise_id_with_their_relative_path(tmp_path: Path) -> None:
    for day, ex_id, content in (
        ("2026-11-01", "run1", b"first"),
        ("2026-11-02", "run2", b"second"),
        ("2026-11-03", "run1", b"later"),  # the same id again: the later path wins
    ):
        path = tmp_path / "tcx" / day / f"{ex_id}.tcx.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (tmp_path / "tcx" / "_end_times.json").write_text("{}", encoding="utf-8")

    raw = load(tmp_path)

    assert dict(raw.tcx) == {
        "run1": TcxFile("tcx/2026-11-03/run1.tcx.gz", b"later"),
        "run2": TcxFile("tcx/2026-11-02/run2.tcx.gz", b"second"),
    }


def test_an_unreadable_tcx_has_no_bytes(tmp_path: Path) -> None:
    (tmp_path / "tcx" / "2026-11-01" / "run1.tcx.gz").mkdir(parents=True)  # a folder, not a file
    assert load(tmp_path).tcx == {"run1": TcxFile("tcx/2026-11-01/run1.tcx.gz", None)}


def test_last_fetch_time_comes_from_meta(tmp_path: Path) -> None:
    assert load(tmp_path).last_fetch_utc is None
    write(tmp_path / "_meta.json", {"last_fetch_utc": "2026-11-23T04:10:00Z", "api_errors": {}})
    assert load(tmp_path).last_fetch_utc == "2026-11-23T04:10:00Z"


def test_an_empty_folder_loads_as_nothing(tmp_path: Path) -> None:
    assert load(tmp_path) == RawData()


def test_data_dir_is_the_folder_or_its_data_subfolder(tmp_path: Path) -> None:
    (tmp_path / "data" / "sleep").mkdir(parents=True)
    assert resolve_data_dir(tmp_path) == tmp_path / "data"
    assert resolve_data_dir(tmp_path / "data") == tmp_path / "data"
    with pytest.raises(FileNotFoundError, match="no daily/, sleep/ or workouts/ folder"):
        resolve_data_dir(tmp_path / "data" / "sleep")


def test_unreadable_non_date_sleep_files_are_skipped_and_reported(tmp_path: Path) -> None:
    write(tmp_path / "sleep" / "2026-11-01.json", {"sessions": []})
    (tmp_path / "sleep" / "notes.json").write_text("not json {", encoding="utf-8")
    write(tmp_path / "sleep" / "readme.json", {"fine": True})  # readable: kept, as before

    raw = load(tmp_path)

    assert [f.stem for f in raw.sleep] == ["2026-11-01", "readme"]
    assert raw.warnings == ("unreadable sleep file notes.json skipped (not named after a date)",)


@pytest.mark.parametrize("folder", ["daily", "workouts"])
def test_daily_and_workout_files_of_any_name_must_parse(tmp_path: Path, folder: str) -> None:
    """0.1.0 parsed every file in these folders (daily/ for run VO2 max), so this stays."""
    (tmp_path / folder).mkdir()
    (tmp_path / folder / "notes.json").write_text("not json {", encoding="utf-8")
    with pytest.raises(ValueError, match="Expecting value"):  # json.JSONDecodeError
        load(tmp_path)
