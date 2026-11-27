"""query: typed rows of the derived CSVs (golden files and invented values only)."""

from __future__ import annotations

import dataclasses
import shutil
import typing
from datetime import date
from pathlib import Path

import pytest

from google_health_claude.derive.output import csv_text
from google_health_claude.derive.schema import DAILY, TABLES, WEIGHT
from google_health_claude.query import Freshness, HealthData
from google_health_claude.query.models import MODELS, Day, WeighIn
from google_health_claude.query.reader import read_table

GOLDEN = Path(__file__).parent / "golden"
EXPECTED, META = GOLDEN / "expected", GOLDEN / "raw" / "_meta.json"
KIND_TYPES = {"TEXT": str, "REAL": float, "INTEGER": int, "BOOL": bool}


# -- models ---------------------------------------------------------------------------


def test_there_is_one_model_per_csv_table() -> None:
    assert [table for table, _ in MODELS] == [t for t in TABLES if t.csv]


@pytest.mark.parametrize(("table", "model"), MODELS, ids=lambda x: getattr(x, "name", ""))
def test_model_fields_mirror_the_schema(table, model) -> None:
    assert [f.name for f in dataclasses.fields(model)] == table.names
    hints = typing.get_type_hints(model)
    for column in table.columns:
        expected = KIND_TYPES[column.kind]
        if column.name not in table.primary_key:
            expected = expected | None
        assert hints[column.name] == expected, column.name


@pytest.mark.parametrize(("table", "model"), MODELS, ids=lambda x: getattr(x, "name", ""))
def test_models_are_frozen_and_slotted(table, model) -> None:
    assert "__slots__" in vars(model) and "__dict__" not in dir(model)
    row = model(*([None] * len(table.columns)))
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.__setattr__(table.names[0], "x")


# -- reader ---------------------------------------------------------------------------


@pytest.mark.parametrize(("table", "model"), MODELS, ids=lambda x: getattr(x, "name", ""))
def test_golden_csvs_round_trip_byte_for_byte(table, model) -> None:
    path = EXPECTED / f"{table.name}.csv"
    rows = read_table(path, table, model)
    assert len(rows) == path.read_text(encoding="utf-8").count("\n") - 1
    again = csv_text(table, [dataclasses.asdict(r) for r in rows])
    assert again.encode("utf-8") == path.read_bytes()


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="")
    return path


def test_cells_decode_by_kind_and_empty_is_none(tmp_path: Path) -> None:
    header = ",".join(DAILY.names)
    cells = {"date": "2026-11-01", "steps": "8165", "hr_avg_bpm": "70.5", "is_partial": "true"}
    line = ",".join(cells.get(name, "") for name in DAILY.names)
    (day,) = read_table(write(tmp_path / "daily.csv", f"{header}\n{line}\n"), DAILY, Day)
    assert (day.date, day.steps, day.hr_avg_bpm, day.is_partial) == ("2026-11-01", 8165, 70.5, True)
    assert day.resting_hr_bpm is None and day.vo2max_level is None
    assert type(day.steps) is int and type(day.hr_avg_bpm) is float


def test_a_header_mismatch_names_the_file_only(tmp_path: Path) -> None:
    path = write(tmp_path / "weight.csv", "timestamp_local,weight_kg,source,secret_77.7\n")
    with pytest.raises(ValueError, match=r"^weight\.csv: header does not match the schema$"):
        read_table(path, WEIGHT, WeighIn)
    first_line_is_data = write(tmp_path / "weight.csv", "2026-11-01T07:00:00+01:00,77.7,FITBIT\n")
    with pytest.raises(ValueError, match=r"^weight\.csv: header does not match the schema$"):
        read_table(first_line_is_data, WEIGHT, WeighIn)
    with pytest.raises(ValueError, match="header does not match"):
        read_table(write(tmp_path / "weight.csv", ""), WEIGHT, WeighIn)


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("2026-11-01T07:00:00+01:00,77.7kg,FITBIT", "row 2: weight_kg is not a valid REAL"),
        (",77.7,FITBIT", "row 2: timestamp_local is empty"),
        ("2026-11-01T07:00:00+01:00,77.7", "row 2 has 2 cells, expected 3"),
    ],
)
def test_bad_rows_raise_without_the_cell(tmp_path: Path, line: str, message: str) -> None:
    path = write(tmp_path / "weight.csv", f"timestamp_local,weight_kg,source\n{line}\n")
    with pytest.raises(ValueError, match=message) as caught:
        read_table(path, WEIGHT, WeighIn)
    assert "77.7" not in str(caught.value)
    error = caught.value  # no chained exception (which would quote the cell) is shown
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)


def test_a_bad_bool_raises_without_the_cell(tmp_path: Path) -> None:
    header = ",".join(DAILY.names)
    line = ",".join(
        "2026-11-01" if n == "date" else "yes" if n == "is_partial" else "" for n in DAILY.names
    )
    with pytest.raises(ValueError, match="is_partial is not a valid BOOL") as caught:
        read_table(write(tmp_path / "daily.csv", f"{header}\n{line}\n"), DAILY, Day)
    assert "yes" not in str(caught.value)


# -- HealthData: known answers from tests/golden (demo seed 1, 2026-10-05 .. 2026-10-26) -------


@pytest.fixture
def health() -> HealthData:
    return HealthData(EXPECTED)


def d(text: str) -> date:
    return date.fromisoformat(text)


def test_daily_is_inclusive_and_skips_days_without_data(health: HealthData) -> None:
    assert [r.date for r in health.daily(d("2026-10-20"), d("2026-10-23"))] == [
        "2026-10-20",
        "2026-10-22",  # 2026-10-21 has no data
        "2026-10-23",
    ]
    (first,) = health.daily(d("2026-10-05"), d("2026-10-05"))
    assert (first.steps, first.distance_km, first.resting_hr_bpm) == (12064, 8.942, 57.0)
    assert len(health.daily()) == 21 and health.daily(end=d("2026-10-04")) == []
    assert [r.date for r in health.daily(start=d("2026-10-26"))] == ["2026-10-26"]


def test_runs_carry_facts_splits_and_partial_flag(health: HealthData) -> None:
    runs = health.runs()
    assert len(runs) == 9 and all(r.workout.type == "RUNNING" for r in runs)
    (last,) = health.runs(d("2026-10-24"), d("2026-10-26"))
    assert last.workout.id == "run-20261025" and last.is_partial is True
    assert last.facts is not None and last.facts.distance_km == 13.124
    assert [s.km_no for s in last.splits] == list(range(1, 15))
    (first,) = health.runs(d("2026-10-06"), d("2026-10-06"))
    assert first.is_partial is False and len(first.splits) == 6
    assert first.facts is not None and first.facts.first_km_pace_min_km == 6.768


def test_nights_carry_stages_and_partial_flag(health: HealthData) -> None:
    nights = health.sleep(d("2026-10-24"), d("2026-10-26"))  # no sleep ends on 2026-10-26
    assert [(n.sleep.id, n.is_partial) for n in nights] == [
        ("sleep-20261024", True),
        ("sleep-20261025", True),
    ]
    assert len(nights[1].stages) == 26 and {s.sleep_id for s in nights[1].stages} == {
        "sleep-20261025"
    }
    (night,) = health.sleep(d("2026-10-05"), d("2026-10-05"))
    assert (night.sleep.is_main, night.sleep.asleep_h, night.is_partial) == (True, 6.9, False)


def test_weekly_returns_weeks_that_overlap_the_range(health: HealthData) -> None:
    assert [w.week_start for w in health.weekly()] == [
        "2026-10-05",
        "2026-10-12",
        "2026-10-19",
        "2026-10-26",
    ]
    assert [w.week_start for w in health.weekly(d("2026-10-11"), d("2026-10-12"))] == [
        "2026-10-05",
        "2026-10-12",
    ]
    (week,) = health.weekly(d("2026-10-19"), d("2026-10-19"))
    assert (week.runs, week.run_km, week.long_run_km) == (2, 17.01, 13.124)
    (empty,) = health.weekly(start=d("2026-10-26"))
    assert (empty.runs, empty.run_km, empty.long_run_km) == (0, 0.0, None)


def test_personal_bests(health: HealthData) -> None:
    assert [(b.record, b.workout_id, b.pace_min_km) for b in health.personal_bests()] == [
        ("best_1km_split", "run-20261015", 5.584),
        ("best_run_5k", "run-20261015", 6.034),
        ("best_run_10k", "run-20261025", 7.014),
    ]


def test_freshness_with_and_without_meta(health: HealthData) -> None:
    expected = Freshness(
        first_day="2026-10-05",
        last_day="2026-10-26",
        complete_through="2026-10-23",
        partial_days=("2026-10-24", "2026-10-25", "2026-10-26"),
        last_fetch_utc=None,
    )
    assert health.freshness() == expected
    with_meta = HealthData(EXPECTED, META).freshness()
    assert with_meta == dataclasses.replace(expected, last_fetch_utc="2026-10-26T04:10:00Z")


@pytest.mark.parametrize("method", ["daily", "sleep", "runs", "weekly"])
def test_start_after_end_is_refused(health: HealthData, method: str) -> None:
    with pytest.raises(ValueError, match="start 2026-10-06 is after end 2026-10-05"):
        getattr(health, method)(d("2026-10-06"), d("2026-10-05"))
    assert getattr(health, method)(d("2026-10-05"), d("2026-10-05")) is not None  # equal is fine


def test_every_call_reads_the_files_again(tmp_path: Path) -> None:
    folder = shutil.copytree(EXPECTED, tmp_path / "derived")
    health = HealthData(folder)
    assert len(health.daily()) == 21
    daily = folder / "daily.csv"
    lines = daily.read_text(encoding="utf-8").splitlines(keepends=True)
    daily.write_text("".join(lines[:-1]), encoding="utf-8", newline="")
    assert len(health.daily()) == 20
    (folder / "quality.csv").unlink()
    with pytest.raises(FileNotFoundError):
        health.freshness()


def test_meta_without_a_fetch_time_gives_none(tmp_path: Path) -> None:
    for body in ('{"api_errors": {}}', "[1, 2]", '{"last_fetch_utc": 20261026}'):
        (tmp_path / "_meta.json").write_text(body, encoding="utf-8")
        assert HealthData(EXPECTED, tmp_path / "_meta.json").freshness().last_fetch_utc is None


def test_tables_with_only_a_header_give_empty_answers(tmp_path: Path) -> None:
    for table in TABLES:
        if table.csv:
            (tmp_path / f"{table.name}.csv").write_text(csv_text(table, []), encoding="utf-8")
    health = HealthData(tmp_path)
    assert health.daily() == health.sleep() == health.runs() == health.weekly() == []
    assert health.freshness() == Freshness(None, None, None, (), None)
