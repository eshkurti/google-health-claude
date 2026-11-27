# Changelog

## 0.2.0

Every existing command and option works as in 0.1.0; `derive` gives the same tables
for the same raw data (checked with `scripts/compare_real.py` against real data).

### Added

- `query {daily,sleep,runs,weekly,pbs,freshness} --derived DIR`: the derived tables as
  JSON, filtered by an inclusive `--start`/`--end` date range. Runs come with their
  splits and facts, nights with their sleep stages, and both say whether their day is
  still partial. `freshness --meta PATH` adds the time of the last fetch.
- `google_health_claude.query.HealthData`: the same answers as typed, frozen
  dataclasses, one per CSV table, read fresh on every call.
- `demo --out DIR [--seed N] [--weeks N]`: invented raw data in fetch's layout, to try
  `derive` without a Fitbit.
- A golden test that pins the generator and `derive` byte for byte, and
  `scripts/compare_real.py`, which compares `derive` across two code versions on real
  data without printing any value.
- CI on Python 3.10 to 3.15 and Windows: ruff, strict mypy for every module, pytest
  with a 91% branch-coverage floor, and tests that pin the CLI contract and the
  import rules between the package's layers.

### Changed

- The code is split into layers: `api/` (HTTP, credentials, sign-in), `fetch/` (the
  only writer of the data folder), `derive/` (reads the data folder only in
  `derive/load.py` and computes the tables without I/O), `query/`, `demo/` and
  `devtools/` (the probes).
- `derive` sums floats with `math.fsum`, so its output no longer depends on the
  Python version.

### Fixed

- A TCX file whose compressed data is corrupt is reported as unreadable instead of
  stopping `derive`.

## 0.1.0

First version: `auth`, `probe`, `probe-tcx`, `probe-more`, `fetch`, `backfill` and
`derive`.
