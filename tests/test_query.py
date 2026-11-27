"""query: typed rows of the derived CSVs (golden files and invented values only)."""

from __future__ import annotations

import dataclasses
import typing
from pathlib import Path

import pytest

from google_health_claude.derive.output import csv_text
from google_health_claude.derive.schema import DAILY, TABLES, WEIGHT
from google_health_claude.query.models import MODELS, Day, WeighIn
from google_health_claude.query.reader import read_table

EXPECTED = Path(__file__).parent / "golden" / "expected"
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
