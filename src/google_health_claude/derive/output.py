"""Writing the derived tables: byte-stable CSV files and an optional SQLite file."""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from pathlib import Path
from typing import Any

from .schema import NOTES, TABLES, Table, schema_markdown

DECIMALS = 3


def cell(value: Any, kind: str) -> str:
    if value is None:
        return ""
    if kind == "BOOL":
        return "true" if value else "false"
    if kind == "INTEGER":
        return str(int(round(value)))
    if kind == "REAL":
        text = f"{float(value):.{DECIMALS}f}".rstrip("0").rstrip(".")
        return "0" if text == "-0" else text
    return str(value)


def sql_value(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if kind in ("BOOL", "INTEGER"):
        return int(round(value))
    if kind == "REAL":
        return round(float(value), DECIMALS)
    return str(value)


def csv_text(table: Table, rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(table.names)
    for row in rows:
        writer.writerow([cell(row.get(c.name), c.kind) for c in table.columns])
    return buffer.getvalue()


def write_text_if_changed(path: Path, text: str) -> bool:
    path = Path(path)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return True


def write_csvs(out_dir: Path, tables: dict[str, list[dict]]) -> int:
    changed = 0
    for table in TABLES:
        if table.csv:
            changed += write_text_if_changed(Path(out_dir) / f"{table.name}.csv", csv_text(table, tables[table.name]))
    changed += write_text_if_changed(Path(out_dir) / "schema.md", schema_markdown())
    return changed


def ensure_notes(out_dir: Path) -> bool:
    """Create an empty notes.csv template if there is none; never touch an existing one."""
    path = Path(out_dir) / "notes.csv"
    if path.exists():
        return False
    write_text_if_changed(path, csv_text(NOTES, []))
    return True


def read_notes(out_dir: Path) -> list[dict]:
    path = Path(out_dir) / "notes.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return [{k: (v or None) for k, v in row.items() if k in NOTES.names} for row in csv.DictReader(handle)]


def write_sqlite(path: Path, tables: dict[str, list[dict]], notes: list[dict]) -> None:
    """Rebuild the SQLite file from scratch (it is derived data) with foreign keys enforced."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        for table, rows in [*((t, tables[t.name]) for t in TABLES), (NOTES, notes)]:
            conn.execute(table.ddl())
            placeholders = ", ".join("?" for _ in table.columns)
            conn.executemany(
                f"INSERT INTO {table.name} ({', '.join(table.names)}) VALUES ({placeholders})",
                ([sql_value(row.get(c.name), c.kind) for c in table.columns] for row in rows),
            )
        conn.commit()
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"foreign key check failed: {problems[:5]}")
    finally:
        conn.close()
    os.replace(tmp, path)
