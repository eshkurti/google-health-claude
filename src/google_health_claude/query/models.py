"""Typed rows of the derived CSV tables: one frozen, slotted dataclass per table.

Fields mirror derive/schema.py column by column, in CSV order, so a row can be
built positionally from a CSV line (tests/test_query.py checks the names and
types against the schema). Primary-key columns are never empty; every other
field is None where the CSV cell is empty, which means missing, never 0. Dates
and times stay ISO 8601 strings, exactly as in the CSV.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..derive.schema import (
    DAILY,
    PERSONAL_BESTS,
    QUALITY,
    RUN_FACTS,
    RUN_SPLITS,
    SLEEP,
    SLEEP_STAGES,
    WEEKLY,
    WEIGHT,
    WORKOUTS,
    Table,
)


@dataclass(frozen=True, slots=True)
class Workout:
    """A deduplicated FITBIT exercise session (workouts.csv)."""

    id: str
    date: str | None
    start_local: str | None
    end_local: str | None
    type: str | None
    recording_method: str | None
    is_training: bool | None
    has_gps: bool | None
    duration_min: float | None
    elapsed_min: float | None
    distance_km: float | None
    pace_min_km: float | None
    avg_hr_bpm: float | None
    peak_hr_bpm: float | None
    kcal: float | None
    steps: int | None
    cadence_spm: float | None
    stride_m: float | None
    elevation_gain_m: float | None
    min_light: float | None
    min_moderate: float | None
    min_vigorous: float | None
    min_peak: float | None
    hard_share: float | None
    azm_points: float | None
    run_vo2max: float | None
    merged_ids: str | None
    n_raw_merged: int | None
    n_hc_copies: int | None
    tcx_file: str | None


@dataclass(frozen=True, slots=True)
class RunSplit:
    """One per-km split of a GPS run (run_splits.csv)."""

    workout_id: str
    km_no: int
    start_local: str | None
    end_local: str | None
    distance_km: float | None
    duration_s: float | None
    pace_min_km: float | None
    avg_hr_bpm: float | None
    max_hr_bpm: float | None


@dataclass(frozen=True, slots=True)
class RunFacts:
    """Facts about one GPS run (run_facts.csv)."""

    workout_id: str
    date: str | None
    distance_km: float | None
    first_km_pace_min_km: float | None
    fastest_km_pace_min_km: float | None
    slowest_km_pace_min_km: float | None
    pace_fade_s_per_km: float | None
    slow_km_count: int | None
    hr_second_half_minus_first_half_bpm: float | None
    peak_hr_bpm: float | None
    gps_gap_total_s: float | None
    walk_s: float | None
    avg_hr_delta_vs_previous_run_bpm: float | None
    avg_hr_delta_vs_4wk_avg_bpm: float | None


@dataclass(frozen=True, slots=True)
class PersonalBest:
    """A best km split or best run over a target distance (personal_bests.csv)."""

    record: str
    target_km: float | None
    workout_id: str | None
    date: str | None
    distance_km: float | None
    duration_s: float | None
    pace_min_km: float | None


@dataclass(frozen=True, slots=True)
class Day:
    """One fetched day (daily.csv)."""

    date: str
    steps: int | None
    distance_km: float | None
    active_kcal: float | None
    total_kcal: float | None
    hr_avg_bpm: float | None
    hr_min_bpm: float | None
    hr_max_bpm: float | None
    resting_hr_bpm: float | None
    hrv_ms: float | None
    hrv_deep_rmssd_ms: float | None
    hrv_entropy: float | None
    hrv_nonrem_hr_bpm: float | None
    spo2_avg_pct: float | None
    spo2_lower_pct: float | None
    spo2_upper_pct: float | None
    spo2_std_pct: float | None
    resp_rate_brpm: float | None
    sleep_temp_vs_baseline_c: float | None
    vo2max: float | None
    vo2max_level: str | None
    sedentary_min: float | None
    active_light_min: float | None
    active_moderate_min: float | None
    active_vigorous_min: float | None
    zone_light_min: float | None
    zone_moderate_min: float | None
    zone_vigorous_min: float | None
    zone_peak_min: float | None
    zone_light_low_bpm: float | None
    zone_light_high_bpm: float | None
    zone_moderate_low_bpm: float | None
    zone_moderate_high_bpm: float | None
    zone_vigorous_low_bpm: float | None
    zone_vigorous_high_bpm: float | None
    zone_peak_low_bpm: float | None
    zone_peak_high_bpm: float | None
    azm_points: float | None
    weight_kg: float | None
    is_partial: bool | None


@dataclass(frozen=True, slots=True)
class WeighIn:
    """One weigh-in (weight.csv)."""

    timestamp_local: str
    weight_kg: float | None
    source: str | None


@dataclass(frozen=True, slots=True)
class SleepSession:
    """One sleep session, dated by the wake-up day (sleep.csv)."""

    id: str
    date: str | None
    is_main: bool | None
    bed_local: str | None
    wake_local: str | None
    time_in_bed_min: float | None
    asleep_h: float | None
    awake_min: float | None
    deep_min: float | None
    rem_min: float | None
    light_min: float | None
    latency_min: float | None
    short_awakenings_count: int | None
    short_awakenings_min: float | None
    efficiency_pct: float | None


@dataclass(frozen=True, slots=True)
class SleepStage:
    """One stage segment of a sleep session (sleep_stages.csv)."""

    sleep_id: str
    night: str | None
    stage: str | None
    start_local: str
    end_local: str | None
    minutes: float | None


@dataclass(frozen=True, slots=True)
class Week:
    """One Monday-to-Sunday week (weekly.csv)."""

    week_start: str
    week_end: str | None
    runs: int | None
    run_km: float | None
    long_run_km: float | None
    run_duration_min: float | None
    run_avg_hr_bpm: float | None
    run_hard_share: float | None
    azm_points: float | None
    sleep_h_avg: float | None
    resting_hr_avg_bpm: float | None
    hrv_avg_ms: float | None
    hrv_avg_ex_outliers_ms: float | None
    weight_kg_avg: float | None
    days_with_data: int | None
    is_complete: bool | None


@dataclass(frozen=True, slots=True)
class DayQuality:
    """Data checks for one day (quality.csv)."""

    date: str
    missing_metrics: str | None
    is_partial: bool | None
    hrv_outlier: bool | None
    resting_hr_outlier: bool | None
    spo2_outlier: bool | None
    tcx_empty: bool | None


# Each model with the table it mirrors, in schema order.
MODELS: tuple[tuple[Table, type], ...] = (
    (WORKOUTS, Workout),
    (RUN_SPLITS, RunSplit),
    (RUN_FACTS, RunFacts),
    (PERSONAL_BESTS, PersonalBest),
    (DAILY, Day),
    (WEIGHT, WeighIn),
    (SLEEP, SleepSession),
    (SLEEP_STAGES, SleepStage),
    (WEEKLY, Week),
    (QUALITY, DayQuality),
)
