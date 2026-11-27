"""Day-level tables: daily, weight, sleep, sleep_stages, quality and weekly.

Weigh-ins are fetched with a 180-day lookback, so daily/ holds weight-only files
for days outside the fetched range. Those days get rows in weight.csv only: the
day-based tables cover fetched days, i.e. days with any data besides weight.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from ..tcx import exercise_id
from .values import (
    dig,
    local_date,
    local_iso,
    mean,
    minutes_between,
    monday,
    num,
    scaled,
    timestamp,
    weighted_mean,
)
from .workouts import WorkoutTables

PARTIAL_DAYS = 3
OUTLIER_WINDOW_DAYS = 28
OUTLIER_MIN_VALUES = 14
OUTLIER_MADS = 3.0
MAD_SCALE = 1.4826  # makes the MAD comparable to a standard deviation for normal data


@dataclass(frozen=True)
class OutlierRule:
    """Per-column settings. The minimum spread keeps integer-valued or very stable metrics
    (MAD near 0) from flagging ordinary small moves; low_ratio also flags any value below
    that share of the median, which catches sensor dropouts in a noisy baseline."""

    min_abs: float = 0.0
    min_share: float = 0.0
    low_ratio: float | None = None


OUTLIER_RULES = {
    "hrv_ms": OutlierRule(min_share=0.12, low_ratio=0.5),
    "resting_hr_bpm": OutlierRule(min_abs=3.0),
    "spo2_avg_pct": OutlierRule(min_abs=1.0),
}
WEIGHT_DUPLICATE_WINDOW = timedelta(minutes=10)
ZONES = ("LIGHT", "MODERATE", "VIGOROUS", "PEAK")

# Data types a full day is expected to have (floors is always empty for this band;
# run-vo2-max and weight only exist on some days).
EXPECTED_DAILY = (
    "steps",
    "distance",
    "active-energy-burned",
    "total-calories",
    "heart-rate",
    "active-minutes",
    "active-zone-minutes",
    "time-in-heart-rate-zone",
    "sedentary-period",
    "daily-resting-heart-rate",
    "daily-heart-rate-variability",
    "daily-oxygen-saturation",
    "daily-respiratory-rate",
    "daily-sleep-temperature-derivations",
    "daily-vo2-max",
    "daily-heart-rate-zones",
)


def load_days(data_dir: Path, category: str) -> dict[date, dict[str, Any]]:
    days = {}
    for path in sorted((Path(data_dir) / category).glob("*.json")):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        body = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(body, dict):
            days[day] = body
    return days


def _summary(metrics: dict[str, Any], data_type: str, key: str) -> dict[str, Any]:
    """The first element's payload of a listed daily summary."""
    for item in metrics.get(data_type) or []:
        payload = item.get(key) if isinstance(item, dict) else None
        if isinstance(payload, dict):
            return payload
    return {}


def _breakdown(
    metrics: dict[str, Any],
    data_type: str,
    path: tuple[str, ...],
    key: str,
    value: str,
    factor: float,
    names: tuple[str, ...],
) -> dict[str, float | None]:
    rollup = metrics.get(data_type)
    if not isinstance(rollup, dict):
        return {}
    found = {
        str(item.get(key)): scaled(item.get(value), factor)
        for item in dig(rollup, *path) or []
        if isinstance(item, dict)
    }
    return {name: found.get(name, 0.0) for name in names}


def weigh_ins(daily_files: dict[date, dict[str, Any]]) -> list[dict[str, Any]]:
    """Every weigh-in; the same weight within 10 min on the same day counts once."""
    samples = []
    for body in daily_files.values():
        for point in dig(body, "metrics", "weight") or []:
            moment = timestamp(dig(point, "weight", "sampleTime", "physicalTime"))
            grams = num(dig(point, "weight", "weightGrams"))
            if moment is not None and grams is not None:
                samples.append((moment, grams, str(dig(point, "dataSource", "platform") or "")))
    kept: list[tuple[datetime, float, set[str]]] = []
    for moment, grams, source in sorted(samples):
        duplicate = next(
            (
                k
                for k in kept
                if k[1] == grams
                and local_date(k[0]) == local_date(moment)
                and moment - k[0] <= WEIGHT_DUPLICATE_WINDOW
            ),
            None,
        )
        if duplicate:
            duplicate[2].add(source)
        else:
            kept.append((moment, grams, {source}))
    return [
        {
            "timestamp_local": local_iso(m),
            "weight_kg": g / 1000,
            "source": ";".join(sorted(src - {""})) or None,
        }
        for m, g, src in kept
    ]


def daily_row(
    day: date, metrics: dict[str, Any], is_partial: bool, weight_kg: float | None
) -> dict[str, Any]:
    hr = dig(metrics, "heart-rate", "heartRate") or {}
    hrv = _summary(metrics, "daily-heart-rate-variability", "dailyHeartRateVariability")
    spo2 = _summary(metrics, "daily-oxygen-saturation", "dailyOxygenSaturation")
    temp = _summary(
        metrics, "daily-sleep-temperature-derivations", "dailySleepTemperatureDerivations"
    )
    vo2 = _summary(metrics, "daily-vo2-max", "dailyVo2Max")
    nightly, baseline = (
        num(temp.get("nightlyTemperatureCelsius")),
        num(temp.get("baselineTemperatureCelsius")),
    )

    # The API leaves zero-valued entries out of these breakdowns (an absent zone
    # always coincides with zero Active Zone Minutes for it), so with the day's
    # rollup present an absent level or zone is 0; without the rollup it stays empty.
    active = _breakdown(
        metrics,
        "active-minutes",
        ("activeMinutes", "activeMinutesRollupByActivityLevel"),
        "activityLevel",
        "activeMinutesSum",
        1,
        ("LIGHT", "MODERATE", "VIGOROUS"),
    )
    in_zone = _breakdown(
        metrics,
        "time-in-heart-rate-zone",
        ("timeInHeartRateZone", "timeInHeartRateZones"),
        "heartRateZone",
        "duration",
        1 / 60,
        ZONES,
    )
    bounds = {
        str(item.get("heartRateZoneType")): (
            num(item.get("minBeatsPerMinute")),
            num(item.get("maxBeatsPerMinute")),
        )
        for item in _summary(metrics, "daily-heart-rate-zones", "dailyHeartRateZones").get(
            "heartRateZones"
        )
        or []
        if isinstance(item, dict)
    }
    azm = dig(metrics, "active-zone-minutes", "activeZoneMinutes")
    azm_parts = (
        [
            num(azm.get(k))
            for k in ("sumInFatBurnHeartZone", "sumInCardioHeartZone", "sumInPeakHeartZone")
        ]
        if isinstance(azm, dict)
        else []
    )
    azm_present = [v for v in azm_parts if v is not None]

    row: dict[str, Any] = {
        "date": day.isoformat(),
        "steps": num(dig(metrics, "steps", "steps", "countSum")),
        "distance_km": scaled(dig(metrics, "distance", "distance", "millimetersSum"), 1e-6),
        "active_kcal": num(dig(metrics, "active-energy-burned", "activeEnergyBurned", "kcalSum")),
        "total_kcal": num(dig(metrics, "total-calories", "totalCalories", "kcalSum")),
        "hr_avg_bpm": num(hr.get("beatsPerMinuteAvg")),
        "hr_min_bpm": num(hr.get("beatsPerMinuteMin")),
        "hr_max_bpm": num(hr.get("beatsPerMinuteMax")),
        "resting_hr_bpm": num(
            _summary(metrics, "daily-resting-heart-rate", "dailyRestingHeartRate").get(
                "beatsPerMinute"
            )
        ),
        "hrv_ms": num(hrv.get("averageHeartRateVariabilityMilliseconds")),
        "hrv_deep_rmssd_ms": num(
            hrv.get("deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds")
        ),
        "hrv_entropy": num(hrv.get("entropy")),
        "hrv_nonrem_hr_bpm": num(hrv.get("nonRemHeartRateBeatsPerMinute")),
        "spo2_avg_pct": num(spo2.get("averagePercentage")),
        "spo2_lower_pct": num(spo2.get("lowerBoundPercentage")),
        "spo2_upper_pct": num(spo2.get("upperBoundPercentage")),
        "spo2_std_pct": num(spo2.get("standardDeviationPercentage")),
        "resp_rate_brpm": num(
            _summary(metrics, "daily-respiratory-rate", "dailyRespiratoryRate").get(
                "breathsPerMinute"
            )
        ),
        "sleep_temp_vs_baseline_c": nightly - baseline
        if nightly is not None and baseline is not None
        else None,
        "vo2max": num(vo2.get("vo2Max")),
        "vo2max_level": vo2.get("cardioFitnessLevel") or None,
        "sedentary_min": scaled(
            dig(metrics, "sedentary-period", "sedentaryPeriod", "durationSum"), 1 / 60
        ),
        "active_light_min": active.get("LIGHT"),
        "active_moderate_min": active.get("MODERATE"),
        "active_vigorous_min": active.get("VIGOROUS"),
        "azm_points": sum(azm_present) if azm_present else None,
        "weight_kg": weight_kg,
        "is_partial": is_partial,
    }
    for zone in ZONES:
        z = zone.lower()
        low, high = bounds.get(zone, (None, None))
        row[f"zone_{z}_min"] = in_zone.get(zone)
        row[f"zone_{z}_low_bpm"] = low
        row[f"zone_{z}_high_bpm"] = high
    return row


def sleep_rows(
    nights: dict[date, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sessions, stages = [], []
    seen: set[str] = set()
    for night, body in sorted(nights.items()):
        for item in body.get("sessions") or []:
            sleep = item.get("sleep") if isinstance(item, dict) else None
            if not isinstance(sleep, dict):
                continue
            bed = timestamp(dig(sleep, "interval", "startTime"))
            wake = timestamp(dig(sleep, "interval", "endTime"))
            sleep_id = (
                exercise_id(str(item.get("name") or "")) or f"{night.isoformat()}_{local_iso(bed)}"
            )
            if sleep_id in seen:
                continue
            seen.add(sleep_id)
            summary = sleep.get("summary") or {}
            by_stage = {
                str(s.get("type")): num(s.get("minutes"))
                for s in summary.get("stagesSummary") or []
                if isinstance(s, dict)
            }
            short = [a for a in sleep.get("shortAwakenings") or [] if isinstance(a, dict)]
            short_min = [
                minutes_between(timestamp(a.get("startTime")), timestamp(a.get("endTime")))
                for a in short
            ]
            in_bed = minutes_between(bed, wake)
            asleep_min = num(summary.get("minutesAsleep"))
            sessions.append(
                {
                    "id": sleep_id,
                    "date": night.isoformat(),
                    "is_main": bool(dig(sleep, "metadata", "mainSleep")),
                    "bed_local": local_iso(bed),
                    "wake_local": local_iso(wake),
                    "time_in_bed_min": in_bed,
                    "asleep_h": asleep_min / 60 if asleep_min is not None else None,
                    "awake_min": num(summary.get("minutesAwake")),
                    "deep_min": by_stage.get("DEEP"),
                    "rem_min": by_stage.get("REM"),
                    "light_min": by_stage.get("LIGHT"),
                    "latency_min": num(summary.get("minutesToFallAsleep")),
                    "short_awakenings_count": len(short),
                    "short_awakenings_min": sum(m for m in short_min if m is not None),
                    "efficiency_pct": asleep_min / in_bed * 100
                    if asleep_min is not None and in_bed
                    else None,
                }
            )
            for stage in sleep.get("stages") or []:
                if not isinstance(stage, dict):
                    continue
                start, end = timestamp(stage.get("startTime")), timestamp(stage.get("endTime"))
                if start is None:
                    continue
                stages.append(
                    {
                        "sleep_id": sleep_id,
                        "night": night.isoformat(),
                        "stage": stage.get("type"),
                        "start_local": local_iso(start),
                        "end_local": local_iso(end),
                        "minutes": minutes_between(start, end),
                    }
                )
    stages.sort(key=lambda r: (r["sleep_id"], r["start_local"]))
    return sessions, stages


def outlier_flags(series: dict[date, float], rule: OutlierRule) -> dict[date, bool | None]:
    """True if a value is more than 3 spreads from the median of the 28 days before it,
    or below rule.low_ratio x median.

    spread = max(1.4826 x MAD, rule.min_abs, rule.min_share x |median|). None when the
    window holds fewer than 14 values.
    """
    flags: dict[date, bool | None] = {}
    for day, value in series.items():
        window = [
            series[d]
            for d in (day - timedelta(days=i) for i in range(1, OUTLIER_WINDOW_DAYS + 1))
            if d in series
        ]
        if len(window) < OUTLIER_MIN_VALUES:
            flags[day] = None
            continue
        center = median(window)
        mad = median(abs(v - center) for v in window)
        spread = max(MAD_SCALE * mad, rule.min_abs, rule.min_share * abs(center))
        too_low = rule.low_ratio is not None and value < rule.low_ratio * center
        flags[day] = too_low or abs(value - center) > OUTLIER_MADS * spread
    return flags


def build_days(
    data_dir: Path, workouts: WorkoutTables, as_of: date
) -> dict[str, list[dict[str, Any]]]:
    daily_files = load_days(data_dir, "daily")
    nights = load_days(data_dir, "sleep")
    workout_days = {date.fromisoformat(w["date"]) for w in workouts.workouts}

    def fetched(day: date) -> bool:
        metrics = (daily_files.get(day) or {}).get("metrics") or {}
        return (
            any(v for k, v in metrics.items() if k != "weight")
            or day in nights
            or day in workout_days
        )

    all_days = sorted(d for d in set(daily_files) | set(nights) | workout_days if fetched(d))
    partial_from = as_of - timedelta(days=PARTIAL_DAYS - 1)
    weight = weigh_ins(daily_files)
    weight_by_day: dict[str, list[float]] = {}
    for w in weight:
        weight_by_day.setdefault(w["timestamp_local"][:10], []).append(w["weight_kg"])

    daily = [
        daily_row(
            day,
            (daily_files.get(day) or {}).get("metrics") or {},
            day >= partial_from,
            mean(weight_by_day.get(day.isoformat(), [])),
        )
        for day in all_days
    ]
    sleep, stages = sleep_rows(nights)

    by_day = {date.fromisoformat(r["date"]): r for r in daily}
    flags = {
        column: outlier_flags(
            {d: r[column] for d, r in by_day.items() if r[column] is not None}, rule
        )
        for column, rule in OUTLIER_RULES.items()
    }
    sleep_days = {r["date"] for r in sleep}
    tcx_needed: dict[str, list[bool]] = {}
    for s, w in zip(workouts.sessions, workouts.workouts):
        if s.needs_tcx:
            tcx_needed.setdefault(w["date"], []).append(not s.points)
    quality = []
    for day in all_days:
        metrics = (daily_files.get(day) or {}).get("metrics") or {}
        missing = [t for t in EXPECTED_DAILY if not metrics.get(t)]
        if day.isoformat() not in sleep_days:
            missing.append("sleep")
        empties = tcx_needed.get(day.isoformat())
        quality.append(
            {
                "date": day.isoformat(),
                "missing_metrics": ";".join(missing) or None,
                "is_partial": day >= partial_from,
                "hrv_outlier": flags["hrv_ms"].get(day),
                "resting_hr_outlier": flags["resting_hr_bpm"].get(day),
                "spo2_outlier": flags["spo2_avg_pct"].get(day),
                "tcx_empty": any(empties) if empties else None,
            }
        )

    weekly = weekly_rows(daily, sleep, quality, workouts.workouts, weight_by_day, as_of)
    return {
        "daily": daily,
        "weight": weight,
        "sleep": sleep,
        "sleep_stages": stages,
        "quality": quality,
        "weekly": weekly,
    }


def weekly_rows(
    daily: list[dict[str, Any]],
    sleep: list[dict[str, Any]],
    quality: list[dict[str, Any]],
    workouts: list[dict[str, Any]],
    weight_by_day: dict[str, list[float]],
    as_of: date,
) -> list[dict[str, Any]]:
    if not daily:
        return []
    by_day = {r["date"]: r for r in daily}
    hrv_flag = {r["date"]: r["hrv_outlier"] for r in quality}
    first, last = (
        monday(date.fromisoformat(daily[0]["date"])),
        monday(date.fromisoformat(daily[-1]["date"])),
    )
    rows = []
    week = first
    while week <= last:
        days = [(week + timedelta(days=i)).isoformat() for i in range(7)]
        rows_in = [by_day[d] for d in days if d in by_day]
        with_data = sum(1 for r in rows_in if r["steps"] is not None or r["hr_avg_bpm"] is not None)
        runs = [
            w for w in workouts if w["date"] in days and w["type"] == "RUNNING" and w["is_training"]
        ]
        has_any = bool(with_data or runs)
        zone_runs = [w for w in runs if w["hard_share"] is not None]
        hard = sum(w["min_vigorous"] + w["min_peak"] for w in zone_runs)
        zone_total = sum(
            sum(w[f"min_{z}"] or 0 for z in ("light", "moderate", "vigorous", "peak"))
            for w in zone_runs
        )
        main_sleep = [r["asleep_h"] for r in sleep if r["date"] in days and r["is_main"]]
        azm = [r["azm_points"] for r in rows_in if r["azm_points"] is not None]

        def total(key: str) -> float | None:
            values = [w[key] for w in runs if w[key] is not None]
            return sum(values) if values else (0.0 if has_any and not runs else None)

        rows.append(
            {
                "week_start": days[0],
                "week_end": days[-1],
                "runs": len(runs) if has_any else None,
                "run_km": total("distance_km"),
                "long_run_km": max(
                    (w["distance_km"] for w in runs if w["distance_km"] is not None), default=None
                ),
                "run_duration_min": total("duration_min"),
                "run_avg_hr_bpm": weighted_mean((w["avg_hr_bpm"], w["duration_min"]) for w in runs),
                "run_hard_share": hard / zone_total if zone_total else None,
                "azm_points": sum(azm) if azm else None,
                "sleep_h_avg": mean(main_sleep),
                "resting_hr_avg_bpm": mean(r["resting_hr_bpm"] for r in rows_in),
                "hrv_avg_ms": mean(r["hrv_ms"] for r in rows_in),
                "hrv_avg_ex_outliers_ms": mean(
                    r["hrv_ms"] for r in rows_in if hrv_flag.get(r["date"]) is not True
                ),
                "weight_kg_avg": mean(mean(weight_by_day[d]) for d in days if d in weight_by_day),
                "days_with_data": with_data,
                "is_complete": with_data == 7
                and not any(r["is_partial"] for r in rows_in)
                and date.fromisoformat(days[-1]) < as_of,
            }
        )
        week += timedelta(days=7)
    return rows
