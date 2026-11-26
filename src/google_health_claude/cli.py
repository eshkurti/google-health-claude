"""Command line: auth | probe | fetch | backfill."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .client import AuthError, HealthClient
from .config import TIMEZONE
from .credentials import CredentialsError, load_credentials
from .fetch import fetch_range


def today() -> date:
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def _date(text: str) -> date:
    return date.fromisoformat(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="google_health_claude", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--client-secret", type=Path, help="Google OAuth client JSON (not needed if GOOGLE_CLIENT_ID/SECRET are set)")
        p.add_argument("--token-file", type=Path, help="local token file written by `auth`")

    p_auth = sub.add_parser("auth", help="one-time browser sign-in; saves a refresh token locally")
    p_auth.add_argument("--client-secret", type=Path, required=True)
    p_auth.add_argument("--token-file", type=Path)

    p_probe = sub.add_parser("probe", help="try every data type for one day and report what works")
    common(p_probe)
    p_probe.add_argument("--date", type=_date, help="day to probe (default: yesterday)")
    p_probe.add_argument("--save-samples", type=Path, help="save raw responses here (contains health data!)")

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
    return parser


def _warn(message: str) -> None:
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") else "warning: "
    print(prefix + message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "auth":
        from .auth import run_auth

        return run_auth(args.client_secret, args.token_file)

    try:
        client = HealthClient(load_credentials(args.client_secret, args.token_file))

        if args.command == "probe":
            from .probe import probe

            day = args.date or today() - timedelta(days=1)
            return 1 if probe(client, day, args.save_samples) else 0

        if args.command == "fetch":
            end = args.end or today()
            start = end - timedelta(days=max(args.days, 1) - 1)
            report = fetch_range(client, start, end, args.data_dir)
        else:
            end = args.until or today() - timedelta(days=1)
            report = fetch_range(client, args.since, end, args.data_dir, skip_existing=not args.force)
    except (CredentialsError, AuthError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    if client.refresh_token_expires_in is not None:
        print(f"refresh_token_expires_in = {client.refresh_token_expires_in}", file=sys.stderr)
    for data_type, message in sorted(report.errors.items()):
        _warn(f"{data_type}: {message}")
    if report.requests_ok == 0:
        print("error: every request failed, nothing was fetched.", file=sys.stderr)
        return 1
    return 0
