"""Listing data points through a day filter, trying both filter spellings.

The docs show filter field names in snake_case and camelCase. Each ListSpec
yields both; the first one the API accepts (no HTTP 400) is remembered per data
type in ``variant_cache`` and tried first from then on.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..api.client import HealthApi, HealthApiError
from .datatypes import ListSpec


def list_for_day(
    client: HealthApi, spec: ListSpec, day: date, variant_cache: dict[str, int]
) -> list[dict[str, Any]]:
    """List a day's points, trying the filter spellings until one is accepted."""
    return list_range(client, spec, day, day + timedelta(days=1), variant_cache)


def list_range(
    client: HealthApi,
    spec: ListSpec,
    start: date,
    end_exclusive: date,
    variant_cache: dict[str, int],
) -> list[dict[str, Any]]:
    """List the points of [start, end_exclusive), trying the filter spellings."""
    candidates = spec.range_filter_candidates(start, end_exclusive)
    first = variant_cache.get(spec.data_type, 0)
    order = [first] + [i for i in range(len(candidates)) if i != first]
    last_error: HealthApiError | None = None
    for index in order:
        try:
            points = client.list_data_points(spec.data_type, candidates[index])
        except HealthApiError as err:
            if err.status != 400:
                raise
            last_error = err
            continue
        variant_cache[spec.data_type] = index
        return points
    if last_error is None:  # unreachable: every ListSpec yields two filter spellings
        raise RuntimeError(f"no filter candidates for {spec.data_type}")
    raise last_error
