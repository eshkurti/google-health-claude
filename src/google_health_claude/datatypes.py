"""Which Google Health data types we pull, and how to query each of them.

Two kinds of queries are used:

* ``ListSpec``: list data points through a day filter (sleep, exercise sessions
  and the "daily" summaries such as resting heart rate and HRV).
* ``RollupSpec``: the ``dailyRollUp`` endpoint, which aggregates intraday data
  (steps, distance, heart rate, ...) into one value per day.

The API docs show filter field names both in snake_case and camelCase, so every
list spec produces both spellings; the fetcher tries them in order and
remembers which one the API accepts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


def _snake(name: str) -> str:
    return name.replace("-", "_")


def _camel(name: str) -> str:
    first, *rest = _snake(name).split("_")
    return first + "".join(part.capitalize() for part in rest)


@dataclass(frozen=True)
class ListSpec:
    data_type: str  # kebab-case id used in the URL, e.g. "daily-resting-heart-rate"
    category: str  # where the result is stored: "daily", "sleep" or "workouts"
    path: tuple[str, ...]  # field path inside the data type used for the day filter

    def filter_candidates(self, day: date) -> list[str]:
        """Filter expressions selecting data points that belong to ``day``."""
        start, end = day.isoformat(), (day + timedelta(days=1)).isoformat()
        candidates = []
        for convert in (_snake, _camel):
            field = ".".join([convert(self.data_type), *(convert(p) for p in self.path)])
            candidates.append(f'{field} >= "{start}" AND {field} < "{end}"')
        return candidates


@dataclass(frozen=True)
class RollupSpec:
    data_type: str
    max_days: int = 90  # the API limits the range of one dailyRollUp request


LIST_SPECS: tuple[ListSpec, ...] = (
    ListSpec("daily-resting-heart-rate", "daily", ("date",)),
    ListSpec("daily-heart-rate-variability", "daily", ("date",)),
    ListSpec("daily-oxygen-saturation", "daily", ("date",)),
    ListSpec("daily-respiratory-rate", "daily", ("date",)),
    ListSpec("daily-sleep-temperature-derivations", "daily", ("date",)),
    ListSpec("daily-vo2-max", "daily", ("date",)),
    # A night belongs to the day you woke up.
    ListSpec("sleep", "sleep", ("interval", "civil_end_time")),
    ListSpec("exercise", "workouts", ("interval", "civil_start_time")),
)

ROLLUP_SPECS: tuple[RollupSpec, ...] = (
    RollupSpec("steps"),
    RollupSpec("distance"),
    RollupSpec("floors"),
    RollupSpec("active-zone-minutes"),
    RollupSpec("active-energy-burned"),
    RollupSpec("sedentary-period"),
    RollupSpec("time-in-heart-rate-zone"),
    RollupSpec("active-minutes", max_days=14),
    RollupSpec("total-calories", max_days=14),
    RollupSpec("heart-rate", max_days=14),
)
