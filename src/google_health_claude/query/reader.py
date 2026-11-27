"""Read a derived CSV file into typed rows, decoding each cell by its column kind.

An empty cell becomes None (missing, never 0); INTEGER, REAL and BOOL cells
are decoded the way derive/output.py wrote them. Errors name the file, a row
number and the schema's column name, never a cell's content: these files hold
health data, and the message may end up in a log.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from ..derive.schema import Table

T = TypeVar("T")

BOOLS = {"true": True, "false": False}


def decode(text: str, kind: str) -> object:
    """One CSV cell as a Python value; ValueError (with the cell in it) if malformed."""
    if text == "":
        return None
    if kind == "INTEGER":
        return int(text)
    if kind == "REAL":
        return float(text)
    if kind == "BOOL":
        if text not in BOOLS:
            raise ValueError(f"not a bool: {text!r}")
        return BOOLS[text]
    return text


def read_table(path: Path, table: Table, make: Callable[..., T]) -> list[T]:
    """Every row of ``path`` (written for ``table``), built as ``make(*cells)``."""
    path = Path(path)
    with path.open(encoding="utf-8", newline="") as handle:
        records = csv.reader(handle)
        if next(records, None) != table.names:
            raise ValueError(f"{path.name}: header does not match the schema")
        rows = []
        for number, cells in enumerate(records, 2):  # row 1 is the header
            if len(cells) != len(table.columns):
                raise ValueError(
                    f"{path.name}: row {number} has {len(cells)} cells, "
                    f"expected {len(table.columns)}"
                )
            values = []
            for text, column in zip(cells, table.columns, strict=True):
                if text == "" and column.name in table.primary_key:
                    raise ValueError(f"{path.name}: row {number}: {column.name} is empty")
                try:
                    values.append(decode(text, column.kind))
                except ValueError:
                    raise ValueError(
                        f"{path.name}: row {number}: {column.name} is not a valid {column.kind}"
                    ) from None  # the original message quotes the cell
            rows.append(make(*values))
    return rows
