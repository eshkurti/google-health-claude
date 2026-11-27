"""scripts/compare_real.py on the golden raw data: counts only, never values, and cleans up."""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RAW = REPO / "tests" / "golden" / "raw"
GIT = shutil.which("git")


def _load():
    spec = importlib.util.spec_from_file_location(
        "compare_real", REPO / "scripts" / "compare_real.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["compare_real"] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


compare_real = _load()


@pytest.fixture
def scratch_dirs(monkeypatch):
    """Records each temporary folder the script makes, to check it is gone afterwards."""
    made = []
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(**kwargs):
        path = real_mkdtemp(**kwargs)
        made.append(Path(path))
        return path

    monkeypatch.setattr(compare_real.tempfile, "mkdtemp", mkdtemp)
    return made


def assert_no_values(output: str) -> None:
    assert not re.search(r"\d{4}-\d{2}-\d{2}", output)  # no dates or timestamps
    assert not re.search(r"\b(run|walk|strength|sleep|nap)-\d", output)  # no record ids
    assert not re.search(r"\d+\.\d+", output)  # no decimals: only counts are printed


def test_working_tree_against_itself_is_identical(capsys, scratch_dirs):
    assert compare_real.main(["--raw", str(RAW), "--ref", str(REPO), "--ref2", str(REPO)]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith("identical")
    for name in ("workouts.csv", "schema.md", "sqlite:exercise_points", "sqlite:notes"):
        assert name in out
    assert "DIFFERS" not in out
    assert_no_values(out)
    assert scratch_dirs and not any(p.exists() for p in scratch_dirs)


def test_a_changed_derive_is_counted_but_not_shown(tmp_path, capsys, scratch_dirs):
    tree = tmp_path / "changed"
    shutil.copytree(REPO / "src", tree / "src", ignore=shutil.ignore_patterns("__pycache__"))
    output_py = tree / "src" / "google_health_claude" / "derive" / "output.py"
    text = output_py.read_text(encoding="utf-8")
    assert "DECIMALS = 3" in text
    output_py.write_text(text.replace("DECIMALS = 3", "DECIMALS = 1"), encoding="utf-8")

    assert compare_real.main(["--raw", str(RAW), "--ref", str(tree), "--ref2", str(REPO)]) == 1
    out = capsys.readouterr().out
    rows = {line.split()[0]: line for line in out.splitlines() if line[:1].isalpha()}
    assert "DIFFERS" in rows["workouts.csv"] and "DIFFERS" in rows["sqlite:exercise_points"]
    assert "DIFFERS" not in rows["schema.md"] and "DIFFERS" not in rows["notes.csv"]
    workouts = rows["workouts.csv"].split()
    assert workouts[1] == workouts[2] and int(workouts[3]) > 0  # same rows, changed lines
    assert_no_values(out)
    assert not any(p.exists() for p in scratch_dirs)


def _worktrees() -> str:
    return compare_real._git("worktree", "list", "--porcelain")


@pytest.mark.skipif(GIT is None or not (REPO / ".git").exists(), reason="needs git and a checkout")
def test_git_refs_use_temporary_worktrees_that_are_removed(capsys, scratch_dirs):
    before = _worktrees()
    assert compare_real.main(["--raw", str(RAW), "--ref", "HEAD", "--ref2", "HEAD"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("identical")
    assert _worktrees() == before
    assert not any(p.exists() for p in scratch_dirs)

    assert compare_real.main(["--raw", str(RAW), "--ref", "no-such-ref-xyz"]) == 2
    assert "neither a source folder nor a git ref" in capsys.readouterr().err
    assert _worktrees() == before
