"""`probe`: try every data type once for a single day and report what works.

Use it before the first real fetch. It prints one line per data type and can
save the raw responses so the data format can be inspected. Saved samples
contain personal health data, so keep them out of git.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .client import HealthApiError
from .datatypes import LIST_SPECS, ROLLUP_SPECS
from .fetch import list_for_day


def probe(client: Any, day: date, save_dir: Path | None = None) -> int:
    """Returns the number of data types that failed."""
    failures = 0

    def out(status: str, name: str, detail: str = "") -> None:
        print(f"{status:<6} {name:<38} {detail}")

    def save(name: str, payload: Any) -> None:
        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)
            (save_dir / f"{name}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    try:
        identity = client.identity()
        out("OK", "identity", "account is linked to Google Health")
        save("identity", identity)
    except HealthApiError as err:
        out("FAIL", "identity", str(err))
        failures += 1

    print(f"\nProbing {day} ...\n")
    variant_cache: dict[str, int] = {}
    for spec in LIST_SPECS:
        try:
            points = list_for_day(client, spec, day, variant_cache)
        except HealthApiError as err:
            out("FAIL", spec.data_type, str(err))
            failures += 1
            continue
        spelling = ("snake_case", "camelCase")[variant_cache.get(spec.data_type, 0)]
        out("OK" if points else "EMPTY", spec.data_type, f"{len(points)} point(s), filter {spelling}")
        if points:
            save(spec.data_type, points)

    for spec in ROLLUP_SPECS:
        try:
            points = client.daily_rollup(spec.data_type, day, day + timedelta(days=1))
        except HealthApiError as err:
            out("FAIL", spec.data_type + " (rollup)", str(err))
            failures += 1
            continue
        out("OK" if points else "EMPTY", spec.data_type + " (rollup)", f"{len(points)} point(s)")
        if points:
            save(spec.data_type + "_rollup", points)

    print(
        "\nrefresh_token_expires_in reported by Google:",
        client.refresh_token_expires_in,
        file=sys.stdout,
    )
    return failures
