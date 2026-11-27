"""Command line: auth | probe | probe-tcx | probe-more | fetch | backfill | derive | demo | query."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .api.client import AuthError, HealthApi, HealthClient
from .api.credentials import CredentialsError, load_credentials
from .config import TIMEZONE
from .demo import DEFAULT_SEED, DEFAULT_WEEKS, MAX_WEEKS, MIN_WEEKS
from .fetch.run import FetchReport, fetch_range


def today() -> date:
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def _date(text: str) -> date:
    return date.fromisoformat(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="google_health_claude", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--client-secret",
            type=Path,
            help="Google OAuth client JSON (not needed if GOOGLE_CLIENT_ID/SECRET are set)",
        )
        p.add_argument("--token-file", type=Path, help="local token file written by `auth`")

    p_auth = sub.add_parser("auth", help="one-time browser sign-in; saves a refresh token locally")
    p_auth.add_argument("--client-secret", type=Path, required=True)
    p_auth.add_argument("--token-file", type=Path)

    p_probe = sub.add_parser("probe", help="try every data type for one day and report what works")
    common(p_probe)
    p_probe.add_argument("--date", type=_date, help="day to probe (default: yesterday)")
    p_probe.add_argument(
        "--save-samples", type=Path, help="save raw responses here (contains health data!)"
    )

    p_tcx = sub.add_parser(
        "probe-tcx",
        help="export a stored GPS run, strength and passive walk as TCX and describe the files",
    )
    common(p_tcx)
    p_tcx.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="data repo root or its data/ folder; workouts are picked from workouts/*.json",
    )
    p_tcx.add_argument(
        "--save-samples",
        type=Path,
        default=Path("samples") / "tcx",
        help="where raw TCX replies are saved (contains health data!) (default: samples/tcx)",
    )

    p_more = sub.add_parser(
        "probe-more",
        help="try heart-rate samples, zones, body metrics and every other readable type (prints only)",
    )
    common(p_more)
    p_more.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="data repo root or its data/ folder; the latest GPS run is taken from workouts/*.json",
    )

    p_fetch = sub.add_parser("fetch", help="fetch the last N days (default 3) into --data-dir")
    common(p_fetch)
    p_fetch.add_argument("--data-dir", type=Path, required=True)
    p_fetch.add_argument("--days", type=int, default=3)
    p_fetch.add_argument("--end", type=_date, help="last day to fetch (default: today)")

    p_back = sub.add_parser("backfill", help="fetch a long history, skipping days already stored")
    common(p_back)
    p_back.add_argument("--data-dir", type=Path, required=True)
    p_back.add_argument("--since", type=_date, required=True)
    p_back.add_argument("--until", type=_date, help="default: yesterday")
    p_back.add_argument("--force", action="store_true", help="re-fetch days that already exist")

    p_derive = sub.add_parser(
        "derive",
        help="build CSV tables (and optionally SQLite) from the fetched files; no network",
    )
    p_derive.add_argument(
        "--data-dir", type=Path, required=True, help="data repo root or its data/ folder"
    )
    p_derive.add_argument(
        "--out", type=Path, required=True, help="folder for the CSV files (outside the data folder)"
    )
    p_derive.add_argument(
        "--sqlite", type=Path, help="also write all tables plus exercise_points to this SQLite file"
    )
    p_derive.add_argument(
        "--as-of",
        type=_date,
        help="date of the last fetch; it and the 2 days before are partial (default: from _meta.json)",
    )

    p_demo = sub.add_parser(
        "demo",
        help="write invented raw data (a beginner's marathon build) in fetch's layout; no network",
    )
    p_demo.add_argument(
        "--out", type=Path, required=True, help="new or empty folder for the raw files"
    )
    p_demo.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"same seed, same data (default {DEFAULT_SEED})",
    )
    p_demo.add_argument(
        "--weeks",
        type=int,
        default=DEFAULT_WEEKS,
        help=f"training weeks from Monday 2026-10-05, {MIN_WEEKS}-{MAX_WEEKS} (default {DEFAULT_WEEKS})",
    )

    p_query = sub.add_parser(
        "query", help="print derived tables as JSON, filtered by date; reads only derive's output"
    )
    tables = p_query.add_subparsers(dest="table", required=True)
    for name, text in QUERY_TABLES.items():
        q = tables.add_parser(name, help=text)
        q.add_argument(
            "--derived", type=Path, required=True, help="the folder derive wrote (its --out)"
        )
        if name in RANGED_QUERIES:
            q.add_argument("--start", type=_date, help="first day, inclusive (default: the first)")
            q.add_argument("--end", type=_date, help="last day, inclusive (default: the last)")
        if name == "freshness":
            q.add_argument(
                "--meta", type=Path, help="_meta.json of the data folder, for last_fetch_utc"
            )
    return parser


QUERY_TABLES = {
    "daily": "one row per day",
    "sleep": "sleep sessions by wake-up date, with stages and is_partial",
    "runs": "RUNNING workouts with facts, splits and is_partial",
    "weekly": "Monday-to-Sunday weeks that overlap the range",
    "pbs": "personal bests",
    "freshness": "first and last day, partial days and (with --meta) the last fetch time",
}
RANGED_QUERIES = ("daily", "sleep", "runs", "weekly")


def _warn(message: str) -> None:
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") else "warning: "
    print(prefix + message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)


Command = Callable[[argparse.Namespace], int]
ClientCommand = Callable[[argparse.Namespace, HealthApi], int]


def _with_client(run: ClientCommand) -> Command:
    """Commands that call the API: load the credentials, turn setup errors into one line."""

    def command(args: argparse.Namespace) -> int:
        try:
            client = HealthClient(load_credentials(args.client_secret, args.token_file))
            return run(args, client)
        except (CredentialsError, AuthError, FileNotFoundError) as err:
            print(f"error: {err}", file=sys.stderr)
            return 1

    return command


def _auth(args: argparse.Namespace) -> int:
    from .api.auth import run_auth

    return run_auth(args.client_secret, args.token_file)


def _demo(args: argparse.Namespace) -> int:
    from .demo import generate

    try:
        demo = generate(args.out, args.seed, args.weeks)
    except ValueError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print(
        f"Wrote {demo.files} files for {demo.first_day} .. {demo.as_of} "
        f"({demo.workouts} workout records, {demo.nights} sleep sessions) to {args.out}. "
        "Every value is invented.",
        file=sys.stderr,
    )
    return 0


def _derive(args: argparse.Namespace) -> int:
    from .derive import derive

    try:
        derive(args.data_dir, args.out, args.sqlite, args.as_of)
    except (FileNotFoundError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


def _query(args: argparse.Namespace) -> int:
    from .query import HealthData

    data = HealthData(args.derived, getattr(args, "meta", None))
    answers: dict[str, Callable[[], Any]] = {
        "daily": lambda: data.daily(args.start, args.end),
        "sleep": lambda: data.sleep(args.start, args.end),
        "runs": lambda: data.runs(args.start, args.end),
        "weekly": lambda: data.weekly(args.start, args.end),
        "pbs": data.personal_bests,
        "freshness": data.freshness,
    }
    try:
        answer = answers[args.table]()
    except (FileNotFoundError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    if isinstance(answer, list):
        print(json.dumps([dataclasses.asdict(row) for row in answer], indent=2))
    else:
        print(json.dumps(dataclasses.asdict(answer), indent=2))
    return 0


def _probe(args: argparse.Namespace, client: HealthApi) -> int:
    from .devtools.probe import probe

    day = args.date or today() - timedelta(days=1)
    return 1 if probe(client, day, args.save_samples) else 0


def _probe_tcx(args: argparse.Namespace, client: HealthApi) -> int:
    from .devtools.probe_tcx import probe_tcx

    return 1 if probe_tcx(client, args.data_dir, args.save_samples) else 0


def _probe_more(args: argparse.Namespace, client: HealthApi) -> int:
    from .devtools.probe_more import probe_more

    return 1 if probe_more(client, args.data_dir, today()) else 0


def _fetch(args: argparse.Namespace, client: HealthApi) -> int:
    end = args.end or today()
    start = end - timedelta(days=max(args.days, 1) - 1)
    return _report_fetch(client, fetch_range(client, start, end, args.data_dir))


def _backfill(args: argparse.Namespace, client: HealthApi) -> int:
    end = args.until or today() - timedelta(days=1)
    report = fetch_range(client, args.since, end, args.data_dir, skip_existing=not args.force)
    return _report_fetch(client, report)


def _report_fetch(client: HealthApi, report: FetchReport) -> int:
    """Warnings for the Actions log; fail only if not a single request worked."""
    if client.refresh_token_expires_in is not None:
        print(f"refresh_token_expires_in = {client.refresh_token_expires_in}", file=sys.stderr)
    for data_type, message in sorted(report.errors.items()):
        _warn(f"{data_type}: {message}")
    if report.tcx.failed:
        _warn(f"TCX: {report.tcx.failed} download(s) failed; see tcx.failures in _meta.json")
    if report.requests_ok == 0:
        print("error: every request failed, nothing was fetched.", file=sys.stderr)
        return 1
    return 0


COMMANDS: dict[str, Command] = {
    "auth": _auth,
    "demo": _demo,
    "derive": _derive,
    "query": _query,
    "probe": _with_client(_probe),
    "probe-tcx": _with_client(_probe_tcx),
    "probe-more": _with_client(_probe_more),
    "fetch": _with_client(_fetch),
    "backfill": _with_client(_backfill),
}
