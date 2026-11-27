"""`probe`: try every data type once for a single day and report what works.

Use it before the first real fetch. It prints one line per data type and can
save the raw responses so the data format can be inspected. Saved samples
contain personal health data, so keep them out of git.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from ..api.client import HealthApi, HealthApiError
from ..fetch.datatypes import LIST_SPECS, ROLLUP_SPECS
from ..fetch.lists import list_for_day


@dataclass(frozen=True)
class _Output:
    """One aligned line per check; raw replies saved only when ``save_dir`` is given."""

    save_dir: Path | None

    def line(self, status: str, name: str, detail: str = "") -> None:
        print(f"{status:<6} {name:<38} {detail}")

    def save(self, name: str, payload: object) -> None:
        if self.save_dir:
            self.save_dir.mkdir(parents=True, exist_ok=True)
            (self.save_dir / f"{name}.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )


def probe(client: HealthApi, day: date, save_dir: Path | None = None) -> int:
    """Returns the number of data types that failed."""
    out = _Output(save_dir)
    failures = _probe_identity(client, out)
    print(f"\nProbing {day} ...\n")
    failures += _probe_lists(client, day, out)
    failures += _probe_rollups(client, day, out)
    print(
        "\nrefresh_token_expires_in reported by Google:",
        client.refresh_token_expires_in,
        file=sys.stdout,
    )
    return failures


def _probe_identity(client: HealthApi, out: _Output) -> int:
    try:
        identity = client.identity()
    except HealthApiError as err:
        out.line("FAIL", "identity", str(err))
        return 1
    out.line("OK", "identity", "account is linked to Google Health")
    out.save("identity", identity)
    return 0


def _probe_lists(client: HealthApi, day: date, out: _Output) -> int:
    failures = 0
    variant_cache: dict[str, int] = {}
    for spec in LIST_SPECS:
        try:
            points = list_for_day(client, spec, day, variant_cache)
        except HealthApiError as err:
            out.line("FAIL", spec.data_type, str(err))
            failures += 1
            continue
        spelling = ("snake_case", "camelCase")[variant_cache.get(spec.data_type, 0)]
        out.line(
            "OK" if points else "EMPTY",
            spec.data_type,
            f"{len(points)} point(s), filter {spelling}",
        )
        if points:
            out.save(spec.data_type, points)
    return failures


def _probe_rollups(client: HealthApi, day: date, out: _Output) -> int:
    failures = 0
    for rollup in ROLLUP_SPECS:
        name = rollup.data_type + " (rollup)"
        try:
            points = client.daily_rollup(rollup.data_type, day, day + timedelta(days=1))
        except HealthApiError as err:
            out.line("FAIL", name, str(err))
            failures += 1
            continue
        out.line("OK" if points else "EMPTY", name, f"{len(points)} point(s)")
        if points:
            out.save(rollup.data_type + "_rollup", points)
    return failures
