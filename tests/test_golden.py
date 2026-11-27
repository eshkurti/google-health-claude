"""Golden test: the demo generator and derive, pinned to their exact output.

tests/golden/raw/             `demo --seed 1 --weeks 3` (invented data, fetch's layout)
tests/golden/expected/        derive's output for it: every CSV and schema.md
tests/golden/expected_sqlite/ one row per workout for SQLite-only tables (a digest of
                              exercise_points, which is too large to keep in full)

Two checks: regenerating from the seed reproduces raw/ (guards the generator),
and derive reproduces expected/ byte for byte (guards derive). SQLite is
compared through queries, never by file bytes.

JSON, CSV and Markdown are compared byte for byte. A .tcx.gz is compared by its
decompressed content plus a gzip header with mtime 0, not by its compressed
bytes: those depend on the Python build (3.11 and 3.12 copy zlib's OS byte into
the header, Windows builds from 3.14 compress with zlib-ng). Identical bytes for
the same Python and zlib are checked in test_demo.py.

Regenerate after an intended change with ``python -m pytest tests/test_golden.py
--update-golden`` and commit the golden files on their own, with the reason.
"""

from __future__ import annotations

import csv
import difflib
import gzip
import hashlib
import io
import shutil
import sqlite3
import zlib
from contextlib import closing
from pathlib import Path

import pytest

from google_health_claude.config import TIMEZONE
from google_health_claude.demo import generate
from google_health_claude.derive import derive
from google_health_claude.derive.output import cell
from google_health_claude.derive.schema import EXERCISE_POINTS, NOTES, TABLES, Table

GOLDEN = Path(__file__).parent / "golden"
RAW, EXPECTED, EXPECTED_SQLITE = GOLDEN / "raw", GOLDEN / "expected", GOLDEN / "expected_sqlite"
SEED, WEEKS = 1, 3
TEXT_SUFFIXES = (".json", ".csv", ".md")
DIGEST_COLUMNS = ("hr_bpm", "speed_m_s", "alt_m")
DIGEST_HEADER = ("workout_id", "rows", "with_hr", "with_speed", "with_alt", "sha256")
GZIP_DEFLATE_MAGIC = bytes([0x1F, 0x8B, 0x08])  # gzip id, compression method deflate
ZERO_MTIME = bytes(4)


def files_of(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def assert_same_tree(actual: Path, expected: Path) -> None:
    """Fail with the missing/extra names, or a unified diff of the first differing file."""
    got, want = files_of(actual), files_of(expected)
    missing, extra = sorted(set(want) - set(got)), sorted(set(got) - set(want))
    if missing or extra:
        pytest.fail(f"file sets differ under {expected.name}/: missing {missing}, extra {extra}")
    for name in sorted(want):
        if got[name] == want[name]:
            continue
        expected_text, actual_text = want[name], got[name]
        if name.endswith(".gz"):
            problem = gzip_header_problem(got[name])
            if problem:
                pytest.fail(f"{name}: {problem} (zlib {zlib.ZLIB_RUNTIME_VERSION})")
            expected_text, actual_text = gzip.decompress(want[name]), gzip.decompress(got[name])
            if expected_text == actual_text:
                continue  # same content; the deflate stream may differ between builds
        elif not name.endswith(TEXT_SUFFIXES):
            pytest.fail(
                f"{name} differs ({len(want[name])} bytes expected, {len(got[name])} actual)"
            )
        diff = difflib.unified_diff(
            _lines(expected_text),
            _lines(actual_text),
            f"expected/{name}",
            f"actual/{name}",
            lineterm="",
        )
        shown = list(diff)
        more = f"\n... {len(shown) - 80} more diff lines" if len(shown) > 80 else ""
        pytest.fail(f"{name} differs:\n" + "\n".join(shown[:80]) + more)


def gzip_header_problem(data: bytes) -> str | None:
    """What is wrong with a gzip header as fetch writes it (deflate, mtime 0), if anything."""
    if data[:3] != GZIP_DEFLATE_MAGIC:
        return "not a deflate gzip stream"
    if data[4:8] != ZERO_MTIME:
        return "gzip mtime is not 0"
    return None


def _lines(content: bytes) -> list[str]:
    """Text lines; single-line XML (TCX) is split into one element per line."""
    text = content.decode("utf-8")
    return (
        text.replace("><", ">\n<").splitlines() if text.startswith("<?xml") else text.splitlines()
    )


def sqlite_cells(conn: sqlite3.Connection, table: Table) -> list[list[str]]:
    """Every row of a table, each value formatted the way the CSV writer formats it."""
    query = f"SELECT {', '.join(table.names)} FROM {table.name}"  # noqa: S608 - names from schema.py
    return [
        [cell(value, c.kind) for value, c in zip(row, table.columns, strict=True)]
        for row in conn.execute(query)
    ]


def point_digests(conn: sqlite3.Connection) -> list[list[str]]:
    """exercise_points per workout: row count, non-empty counts, sha256 of its rows by time."""
    by_workout: dict[str, list[list[str]]] = {}
    for row in sorted(sqlite_cells(conn, EXERCISE_POINTS), key=lambda r: (r[0], float(r[1]))):
        by_workout.setdefault(row[0], []).append(row)
    names = EXERCISE_POINTS.names
    digests = []
    for workout_id, rows in sorted(by_workout.items()):
        text = "\n".join(",".join(r) for r in rows)
        digests.append(
            [
                workout_id,
                str(len(rows)),
                *(str(sum(1 for r in rows if r[names.index(c)])) for c in DIGEST_COLUMNS),
                hashlib.sha256(text.encode()).hexdigest(),
            ]
        )
    return digests


def csv_text(header: tuple[str, ...] | list[str], rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def write_sqlite_digests(db: Path, folder: Path) -> None:
    # A new SQLite-only table needs its own comparison here: fail instead of skipping it.
    assert [t for t in TABLES if not t.csv] == [EXERCISE_POINTS]
    folder.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db)) as conn:
        text = csv_text(DIGEST_HEADER, point_digests(conn))
    (folder / "exercise_points.csv").write_text(text, encoding="utf-8", newline="\n")


def update_golden(tmp: Path) -> None:
    for folder in (RAW, EXPECTED, EXPECTED_SQLITE):
        shutil.rmtree(folder, ignore_errors=True)
    generate(RAW, SEED, WEEKS)
    db = tmp / "golden.sqlite"
    derive(RAW, EXPECTED, db, log=lambda m: None)
    write_sqlite_digests(db, EXPECTED_SQLITE)


@pytest.fixture(scope="module")
def derived(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """derive's CSV folder and SQLite file for the golden raw data, in a temp folder."""
    assert TIMEZONE == "Europe/Berlin", "the golden files assume GHC_TIMEZONE is not set"
    tmp = tmp_path_factory.mktemp("golden")
    if request.config.getoption("--update-golden"):
        update_golden(tmp)
    out = tmp / "derived"
    derive(RAW, out, tmp / "derived.sqlite", log=lambda m: None)
    return out


def test_generator_reproduces_golden_raw(derived: Path, tmp_path: Path) -> None:
    generate(tmp_path / "raw", SEED, WEEKS)
    assert_same_tree(tmp_path / "raw", RAW)


def test_derive_matches_golden_expected(derived: Path) -> None:
    assert_same_tree(derived, EXPECTED)


def test_sqlite_tables_match_the_golden_csvs(derived: Path) -> None:
    with closing(sqlite3.connect(derived.parent / "derived.sqlite")) as conn:
        for table in (*TABLES, NOTES):
            if not table.csv:
                continue
            with (EXPECTED / f"{table.name}.csv").open(encoding="utf-8", newline="") as handle:
                header, *expected = list(csv.reader(handle))
            assert header == table.names, table.name
            actual = sqlite_cells(conn, table)
            if sorted(actual) != sorted(expected):
                diff = difflib.unified_diff(
                    [",".join(r) for r in sorted(expected)],
                    [",".join(r) for r in sorted(actual)],
                    f"expected/{table.name}.csv",
                    f"sqlite:{table.name}",
                    lineterm="",
                )
                pytest.fail(f"SQLite table {table.name} differs:\n" + "\n".join(list(diff)[:80]))


def test_sqlite_only_tables_match_their_digests(derived: Path, tmp_path: Path) -> None:
    write_sqlite_digests(derived.parent / "derived.sqlite", tmp_path)
    assert_same_tree(tmp_path, EXPECTED_SQLITE)
