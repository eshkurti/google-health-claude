#!/usr/bin/env python3
"""Compare derive's output from two code versions on the same raw data, showing no values.

    python scripts/compare_real.py --raw PATH --ref TAG [--ref2 TAG_OR_WORKTREE]

``--ref`` and ``--ref2`` each name a git ref (tag, branch, commit), which is
checked out into a temporary git worktree, or a source tree on disk (a folder
holding ``src/google_health_claude``). ``--ref2`` defaults to this working tree,
uncommitted changes included.

Each version runs ``derive`` (CSV and SQLite) in its own process, with that
tree's ``src`` first on the import path, into a temporary folder outside both
repos. The report lists file and table names, row counts and how many lines or
rows differ, nothing else: no cell value is ever printed, and derive's own
messages are not shown either (they can name dates). The temporary worktrees and
folders, which hold derived health data, are removed at the end.

Exit status: 0 identical, 1 differences, 2 error. Meant for real data, so run it
yourself; it never touches the network.
"""

from __future__ import annotations

import argparse
import difflib
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

USAGE = "Compare derive's output from two code versions on the same raw data, showing no values."
REPO = Path(__file__).resolve().parents[1]
PACKAGE = Path("src") / "google_health_claude"


class CompareError(Exception):
    """A problem worth one line on stderr (never contains data values)."""


@dataclass(frozen=True)
class Tree:
    label: str
    path: Path


@dataclass(frozen=True)
class Difference:
    name: str
    rows_a: int | None  # None: missing in that version
    rows_b: int | None
    only_a: int  # lines or rows only in A
    only_b: int

    @property
    def same(self) -> bool:
        return self.rows_a == self.rows_b and not self.only_a and not self.only_b


def _git(*args: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise CompareError("git not found on PATH")
    result = subprocess.run(  # noqa: S603 - fixed git binary, list arguments, no shell
        [git, "-C", str(REPO), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise CompareError(f"git {args[0]} failed: {result.stderr.strip().splitlines()[-1:]}")
    return result.stdout


def resolve(spec: str, scratch: Path, name: str, worktrees: list[Path]) -> Tree:
    """A source tree on disk as given, or a git ref checked out into a temporary worktree."""
    candidate = Path(spec)
    if candidate.is_dir():
        if not (candidate / PACKAGE).is_dir():
            raise CompareError(f"{spec} is a folder but has no {PACKAGE.as_posix()}")
        return Tree(spec, candidate.resolve())
    if spec.startswith("-"):
        raise CompareError(f"not a ref: {spec}")
    try:
        commit = _git("rev-parse", "--verify", "--quiet", f"{spec}^{{commit}}").strip()
    except CompareError:
        raise CompareError(f"{spec} is neither a source folder nor a git ref") from None
    target = scratch / name
    _git("worktree", "add", "--detach", str(target), commit)
    worktrees.append(target)
    return Tree(spec, target)


def run_derive(tree: Tree, raw: Path, out: Path, scratch: Path) -> None:
    """derive with this tree's code; its output and messages stay in ``out``/unshown."""
    env = {**os.environ, "PYTHONPATH": str(tree.path / "src")}
    check = subprocess.run(
        [sys.executable, "-c", "import google_health_claude as m; print(m.__file__)"],
        cwd=scratch,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    loaded = Path(check.stdout.strip()).resolve() if check.returncode == 0 else None
    if loaded is None or (tree.path / PACKAGE).resolve() not in loaded.parents:
        raise CompareError(f"could not import the code of {tree.label} from its own tree")
    result = subprocess.run(  # noqa: S603 - this interpreter, list arguments, no shell
        [
            sys.executable,
            "-m",
            "google_health_claude",
            "derive",
            "--data-dir",
            str(raw),
            "--out",
            str(out / "csv"),
            "--sqlite",
            str(out / "derived.sqlite"),
        ],
        cwd=scratch,
        env=env,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise CompareError(
            f"derive failed for {tree.label} (exit {result.returncode}); "
            "run it by hand to see the message"
        )


def _lines(path: Path) -> list[str] | None:
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else None


def _only_counts(a: Sequence[Any], b: Sequence[Any]) -> tuple[int, int]:
    only_a = only_b = 0
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            only_a += i2 - i1
            only_b += j2 - j1
    return only_a, only_b


def compare_files(a: Path, b: Path) -> list[Difference]:
    names = sorted({p.name for p in a.iterdir()} | {p.name for p in b.iterdir()})
    result = []
    for name in names:
        lines_a, lines_b = _lines(a / name), _lines(b / name)
        header = 1 if name.endswith(".csv") else 0  # rows of a CSV, lines of anything else
        rows_a = None if lines_a is None else len(lines_a) - header
        rows_b = None if lines_b is None else len(lines_b) - header
        only_a, only_b = _only_counts(lines_a or [], lines_b or [])
        result.append(Difference(name, rows_a, rows_b, only_a, only_b))
    return result


def _table_rows(db: Path) -> dict[str, tuple[list[str], Counter[tuple[Any, ...]]]]:
    with closing(sqlite3.connect(db)) as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        tables = {}
        for name in sorted(names):
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [r[1] for r in conn.execute(f"PRAGMA table_info({quoted})")]
            rows = Counter(conn.execute(f"SELECT * FROM {quoted}"))  # noqa: S608 - quoted table names read from the file itself
            tables[name] = (columns, rows)
    return tables


def compare_sqlite(a: Path, b: Path) -> list[Difference]:
    tables_a, tables_b = _table_rows(a), _table_rows(b)
    result = []
    for name in sorted(set(tables_a) | set(tables_b)):
        columns_a, rows_a = tables_a.get(name, ([], Counter()))
        columns_b, rows_b = tables_b.get(name, ([], Counter()))
        if columns_a != columns_b:  # every row differs when the columns do
            only_a, only_b = rows_a.total(), rows_b.total()
        else:
            only_a, only_b = (rows_a - rows_b).total(), (rows_b - rows_a).total()
        result.append(
            Difference(
                f"sqlite:{name}",
                rows_a.total() if name in tables_a else None,
                rows_b.total() if name in tables_b else None,
                only_a,
                only_b,
            )
        )
    return result


def _remove(path: Path) -> None:
    def force(func: Callable[[str], object], target: str, _exc: object) -> None:
        os.chmod(target, stat.S_IWRITE)  # git marks some object files read-only on Windows
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=force)
    else:
        shutil.rmtree(path, onerror=force)


def compare(raw: Path, ref: str, ref2: str | None) -> list[Difference]:
    raw = raw.resolve()
    if not raw.is_dir():
        raise CompareError(f"--raw {raw} is not a folder")
    scratch = Path(tempfile.mkdtemp(prefix="ghc-compare-")).resolve()
    worktrees: list[Path] = []
    try:
        for guarded in (REPO, raw):
            if scratch == guarded or guarded in scratch.parents:
                raise CompareError("the temporary folder would be inside a repo; set TMPDIR")
        tree_a = resolve(ref, scratch, "tree-a", worktrees)
        tree_b = resolve(ref2, scratch, "tree-b", worktrees) if ref2 else Tree("working tree", REPO)
        for tree, out in ((tree_a, scratch / "out-a"), (tree_b, scratch / "out-b")):
            run_derive(tree, raw, out, scratch)
        return [
            *compare_files(scratch / "out-a" / "csv", scratch / "out-b" / "csv"),
            *compare_sqlite(
                scratch / "out-a" / "derived.sqlite", scratch / "out-b" / "derived.sqlite"
            ),
        ]
    finally:
        _clean_up(worktrees, scratch)


def _clean_up(worktrees: list[Path], scratch: Path) -> None:
    """Remove every temporary worktree and the scratch folder, even if one step fails."""
    for worktree in worktrees:
        try:
            _git("worktree", "remove", "--force", str(worktree))
        except CompareError as err:
            print(f"warning: {err}; run `git worktree prune`", file=sys.stderr)
    if worktrees:
        _git("worktree", "prune")
    _remove(scratch)


def report(differences: list[Difference], label_a: str, label_b: str) -> str:
    def count(n: int | None) -> str:
        return "missing" if n is None else str(n)

    width = max([len(d.name) for d in differences] + [4])
    lines = [
        f"A = {label_a}",
        f"B = {label_b}",
        "",
        f"{'name':<{width}}  {'rows A':>8}  {'rows B':>8}  {'only A':>7}  {'only B':>7}",
    ]
    for d in differences:
        lines.append(
            f"{d.name:<{width}}  {count(d.rows_a):>8}  {count(d.rows_b):>8}  "
            f"{d.only_a:>7}  {d.only_b:>7}{'' if d.same else '  DIFFERS'}"
        )
    changed = sum(1 for d in differences if not d.same)
    lines += ["", "identical" if not changed else f"{changed} of {len(differences)} differ"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, required=True, help="raw data folder (fetch layout)")
    parser.add_argument("--ref", required=True, help="git ref or source tree for version A")
    parser.add_argument(
        "--ref2", help="git ref or source tree for version B (default: this working tree)"
    )
    args = parser.parse_args(argv)
    try:
        differences = compare(args.raw, args.ref, args.ref2)
    except CompareError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    print(report(differences, args.ref, args.ref2 or "working tree"))
    return 0 if all(d.same for d in differences) else 1


if __name__ == "__main__":
    sys.exit(main())
