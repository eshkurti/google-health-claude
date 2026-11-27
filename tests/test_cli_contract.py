"""The CLI is a contract: the data repo's workflow and Emil's commands depend on it.

The daily GitHub Actions job runs ``python -m google_health_claude fetch --data-dir
data --days N`` and ``derive --data-dir data --out derived``. Every existing
subcommand and option is pinned here (flag names, destination, default, whether
it is required, value type), so moving or splitting code cannot silently change
the live pipeline. New subcommands may be added; existing ones may not change.
Help texts are free to change.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from google_health_claude.cli import build_parser

# (flags, dest, default, required, kind); kind is "path", "int", "date", "str" or "flag".
Option = tuple[tuple[str, ...], str, Any, bool, str]

CREDENTIAL_OPTIONS: tuple[Option, ...] = (
    (("--client-secret",), "client_secret", None, False, "path"),
    (("--token-file",), "token_file", None, False, "path"),
)

PINNED: dict[str, tuple[Option, ...]] = {
    "auth": (
        (("--client-secret",), "client_secret", None, True, "path"),
        (("--token-file",), "token_file", None, False, "path"),
    ),
    "probe": (
        *CREDENTIAL_OPTIONS,
        (("--date",), "date", None, False, "date"),
        (("--save-samples",), "save_samples", None, False, "path"),
    ),
    "probe-tcx": (
        *CREDENTIAL_OPTIONS,
        (("--data-dir",), "data_dir", None, True, "path"),
        (("--save-samples",), "save_samples", Path("samples") / "tcx", False, "path"),
    ),
    "probe-more": (
        *CREDENTIAL_OPTIONS,
        (("--data-dir",), "data_dir", None, True, "path"),
    ),
    "fetch": (
        *CREDENTIAL_OPTIONS,
        (("--data-dir",), "data_dir", None, True, "path"),
        (("--days",), "days", 3, False, "int"),
        (("--end",), "end", None, False, "date"),
    ),
    "backfill": (
        *CREDENTIAL_OPTIONS,
        (("--data-dir",), "data_dir", None, True, "path"),
        (("--since",), "since", None, True, "date"),
        (("--until",), "until", None, False, "date"),
        (("--force",), "force", False, False, "flag"),
    ),
    "derive": (
        (("--data-dir",), "data_dir", None, True, "path"),
        (("--out",), "out", None, True, "path"),
        (("--sqlite",), "sqlite", None, False, "path"),
        (("--as-of",), "as_of", None, False, "date"),
    ),
    "demo": (
        (("--out",), "out", None, True, "path"),
        (("--seed",), "seed", 7, False, "int"),
        (("--weeks",), "weeks", 6, False, "int"),
    ),
}

# The exact command lines of the data repo's workflow (its .github/workflows/fetch.yml).
WORKFLOW_COMMANDS = (
    ["fetch", "--data-dir", "data", "--days", "3"],
    ["derive", "--data-dir", "data", "--out", "derived"],
)


def _subcommands() -> dict[str, argparse.ArgumentParser]:
    parser = build_parser()
    for action in parser._actions:  # argparse keeps subparsers only here
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("the CLI has no subcommands")


def _kind(action: argparse.Action) -> str:
    if isinstance(action, argparse._StoreTrueAction):
        return "flag"
    if action.type is None:
        return "str"
    if action.type is Path:
        return "path"
    if action.type is int:
        return "int"
    if isinstance(action.type("2026-10-05"), date):
        return "date"
    raise AssertionError(f"unexpected option type for {action.option_strings}")


def _options(sub: argparse.ArgumentParser) -> tuple[Option, ...]:
    return tuple(
        (tuple(a.option_strings), a.dest, a.default, a.required, _kind(a))
        for a in sub._actions
        if a.option_strings and not isinstance(a, argparse._HelpAction)
    )


def test_existing_subcommands_are_still_there() -> None:
    missing = sorted(set(PINNED) - set(_subcommands()))
    assert not missing, f"subcommands removed: {missing}"


@pytest.mark.parametrize("name", sorted(PINNED))
def test_options_of_existing_subcommands_are_unchanged(name: str) -> None:
    assert _options(_subcommands()[name]) == PINNED[name]


@pytest.mark.parametrize("argv", WORKFLOW_COMMANDS, ids=lambda argv: argv[0])
def test_workflow_command_lines_parse(argv: list[str]) -> None:
    args = build_parser().parse_args(argv)
    assert args.command == argv[0]
    assert args.data_dir == Path("data")
