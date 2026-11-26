"""Day-level tables: daily, sleep, sleep_stages, quality and weekly."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from ..tcx import exercise_id
from .values import (
    dig, local_iso, mean, minutes_between, monday, num, scaled, timestamp, weighted_mean, ymd,
)
from .workouts import WorkoutTables

PARTIAL_DAYS = 3
OUTLIER_WINDOW_DAYS = 28
OUTLIER_MIN_VALUES = 7
OUTLIER_MADS = 3.0
MAD_FLOOR_SHARE = 0.01
WEIGHT_DUPLICATE_WINDOW = timedelta(minutes=10)
ZONES = ("LIGHT", "MODERATE", "VIGOROUS", "PEAK")

# Data types a full day is expected to have (floors is always empty for this band;
# run-vo2-max and weight only exist on some days).
EXPECTED_DAILY = (
    "steps", "distance", "active-energy-burned", "total-calories", "heart-rate", "active-minutes",
    "active-zone-minutes", "time-in-heart-rate-zone", "sedentary-period",
    "daily-resting-heart-rate", "daily-heart-rate-variability", "daily-oxygen-saturation",
    "daily-respiratory-rate", "daily-sleep-temperature-derivations", "daily-vo2-max",
    "daily-heart-rate-zones",
)


def load_days(data_dir: Path, category: str) -> dict[date, dict]:
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


def _summary(metrics: dict, data_type: str, key: str) -> dict:
    """The first element's payload of a listed daily summary."""
    for item in metrics.get(data_type) or []:
        payload = item.get(key) if isinstance(item, dict) else None
        if isinstance(payload, dict):
            return payload
    return {}


def _breakdown(metrics: dict, data_type: str, path: tuple[str, ...], key: str, value: str,
               factor: float, names: tuple[str, ...]) -> dict[str, float | None]:
    rollup = metrics.get(data_type)
    if not isinstance(rollup, dict):
        return {}
    found = {str(item.get(key)): scaled(item.get(value), factor)
             for item in dig(rollup, *path) or [] if isinstance(item, dict)}
    return {name: found.get(name, 0.0) for name in names}


def weight_kg(points: list) -> float | None:
    """Mean of the day's weigh-ins; the same weight within 10 min counts once."""
    samples = []
    for point in points or []:
        moment = timestamp(dig(point, "weight", "sampleTime", "physicalTime"))
        grams = num(dig(point, "weight", "weightGrams"))
        if moment is not None and grams is not None:
            samples.append((moment, grams))
    kept: list[tuple[datetime, float]] = []
    for moment, grams in sorted(samples):
        if any(g == grams and moment - m <= WEIGHT_DUPLICATE_WINDOW for m, g in kept):
            continue
        kept.append((moment, grams))
    return mean(g / 1000 for _, g in kept)


def daily_row(day: date, metrics: dict, is_partial: bool) -> dict[str, Any]:
    hr = dig(metrics, "heart-rate", "heartRate") or {}
    hrv = _summary(metrics, "daily-heart-rate-variability", "dailyHeartRateVariability")
    spo2 = _summary(metrics, "daily-oxygen-saturation", "dailyOxygenSaturation")
    temp = _summary(metrics, "daily-sleep-temperature-derivations", "dailySleepTemperatureDerivations")
    vo2 = _summary(metrics, "daily-vo2-max", "dailyVo2Max")
    nightly, baseline = num(temp.get("nightlyTemperatureCelsius")), num(temp.get("baselineTemperatureCelsius"))

    # The API leaves zero-valued entries out of these breakdowns (an absent zone
    # always coincides with zero Active Zone Minutes for it), so with the day's
    # rollup present an absent level or zone is 0; without the rollup it stays empty.
    active = _breakdown(metrics, "active-minutes", ("activeMinutes", "activeMinutesRollupByActivityLevel"),
                        "activityLevel", "activeMinutesSum", 1, ("LIGHT", "MODERATE", "VIGOROUS"))
    in_zone = _breakdown(metrics, "time-in-heart-rate-zone", ("timeInHeartRateZone", "timeInHeartRateZones"),
                         "heartRateZone", "duration", 1 / 60, ZONES)
    bounds = {
        str(item.get("heartRateZoneType")): (num(item.get("minBeatsPerMinute")), num(item.get("maxBeatsPerMinute")))
        for item in _summary(metrics, "daily-heart-rate-zones", "dailyHeartRateZones").get("heartRateZones") or []
        if isinstance(item, dict)
    }
    azm = dig(metrics, "active-zone-minutes", "activeZoneMinutes")
    azm_parts = [num(azm.get(k)) for k in ("sumInFatBurnHeartZone", "sumInCardioHeartZone", "sumInPeakHeartZone")] \
        if isinstance(azm, dict) else []
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
        "resting_hr_bpm": num(_summary(metrics, "daily-resting-heart-rate", "dailyRestingHeartRate").get("beatsPerMinute")),
        "hrv_ms": num(hrv.get("averageHeartRateVariabilityMilliseconds")),
        "hrv_deep_rmssd_ms": num(hrv.get("deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds")),
        "hrv_entropy": num(hrv.get("entropy")),
        "hrv_nonrem_hr_bpm": num(hrv.get("nonRemHeartRateBeatsPerMinute")),
        "spo2_avg_pct": num(spo2.get("averagePercentage")),
        "spo2_lower_pct": num(spo2.get("lowerBoundPercentage")),
        "spo2_upper_pct": num(spo2.get("upperBoundPercentage")),
        "spo2_std_pct": num(spo2.get("standardDeviationPercentage")),
        "resp_rate_brpm": num(_summary(metrics, "daily-respiratory-rate", "dailyRespiratoryRate").get("breathsPerMinute")),
        "sleep_temp_vs_baseline_c": nightly - baseline if nightly is not None and baseline is not None else None,
        "vo2max": num(vo2.get("vo2Max")),
        "vo2max_level": vo2.get("cardioFitnessLevel") or None,
        "sedentary_min": scaled(dig(metrics, "sedentary-period", "sedentaryPeriod", "durationSum"), 1 / 60),
        "active_light_min": active.get("LIGHT"),
        "active_moderate_min": active.get("MODERATE"),
        "active_vigorous_min": active.get("VIGOROUS"),
        "azm_points": sum(azm_present) if azm_present else None,
        "weight_kg": weight_kg(metrics.get("weight")),
        "is_partial": is_partial,
    }
    for zone in ZONES:
        z = zone.lower()
        low, high = bounds.get(zone, (None, None))
        row[f"zone_{z}_min"] = in_zone.get(zone)
        row[f"zone_{z}_low_bpm"] = low
        row[f"zone_{z}_high_bpm"] = high
    return row


def sleep_rows(nights: dict[date, dict]) -> tuple[list[dict], list[dict]]:
    sessions, stages = [], []
    seen: set[str] = set()
    for night, body in sorted(nights.items()):
        for item in body.get("sessions") or []:
            sleep = item.get("sleep") if isinstance(item, dict) else None
            if not isinstance(sleep, dict):
                continue
            bed = timestamp(dig(sleep, "interval", "startTime"))
            wake = timestamp(dig(sleep, "interval", "endTime"))
            sleep_id = exercise_id(str(item.get("name") or "")) or f"{night.isoformat()}_{local_iso(bed)}"
            if sleep_id in seen:
                continue
            seen.add(sleep_id)
            summary = sleep.get("summary") or {}
            by_stage = {str(s.get("type")): num(s.get("minutes"))
                        for s in summary.get("stagesSummary") or [] if isinstance(s, dict)}
            short = [a for a in sleep.get("shortAwakenings") or [] if isinstance(a, dict)]
            short_min = [minutes_between(timestamp(a.get("startTime")), timestamp(a.get("endTime"))) for a in short]
            in_bed = minutes_between(bed, wake)
            asleep_min = num(summary.get("minutesAsleep"))
            sessions.append({
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
                "efficiency_pct": asleep_min / in_bed * 100 if asleep_min is not None and in_bed else None,
            })
            for stage in sleep.get("stages") or []:
                if not isinstance(stage, dict):
                    continue
                start, end = timestamp(stage.get("startTime")), timestamp(stage.get("endTime"))
                if start is None:
                    continue
                stages.append({
                    "sleep_id": sleep_id, "night": night.isoformat(), "stage": stage.get("type"),
                    "start_local": local_iso(start), "end_local": local_iso(end),
                    "minutes": minutes_between(start, end),
                })
    stages.sort(key=lambda r: (r["sleep_id"], r["start_local"]))
    return sessions, stages


def outlier_flags(series: dict[date, float]) -> dict[date, bool | None]:
    """True if a value is more than 3 MAD from the median of the 28 days before it."""
    flags: dict[date, bool | None] = {}
    for day, value in series.items():
        window = [series[d] for d in (day - timedelta(days=i) for i in range(1, OUTLIER_WINDOW_DAYS + 1))
                  if d in series]
        if len(window) < OUTLIER_MIN_VALUES:
            flags[day] = None
            continue
        center = median(window)
        mad = max(median(abs(v - center) for v in window), MAD_FLOOR_SHARE * abs(center))
        flags[day] = abs(value - center) > OUTLIER_MADS * mad
    return flags


def build_days(data_dir: Path, workouts: WorkoutTables, as_of: date) -> dict[str, list[dict]]:
    daily_files = load_days(data_dir, "daily")
    nights = load_days(data_dir, "sleep")
    workout_days = {date.fromisoformat(w["date"]) for w in workouts.workouts}
    all_days = sorted(set(daily_files) | set(nights) | workout_days)
    partial_from = as_of - timedelta(days=PARTIAL_DAYS - 1)

    daily = [
        daily_row(day, (daily_files.get(day) or {}).get("metrics") or {}, day >= partial_from)
        for day in all_days
    ]
    sleep, stages = sleep_rows(nights)

    by_day = {date.fromisoformat(r["date"]): r for r in daily}
    flags = {
        column: outlier_flags({d: r[column] for d, r in by_day.items() if r[column] is not None})
        for column in ("hrv_ms", "resting_hr_bpm", "spo2_avg_pct")
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
        quality.append({
            "date": day.isoformat(),
            "missing_metrics": ";".join(missing) or None,
            "is_partial": day >= partial_from,
            "hrv_outlier": flags["hrv_ms"].get(day),
            "resting_hr_outlier": flags["resting_hr_bpm"].get(day),
            "spo2_outlier": flags["spo2_avg_pct"].get(day),
            "tcx_empty": any(empties) if empties else None,
        })

    weekly = weekly_rows(daily, sleep, quality, workouts.workouts, as_of)
    return {"daily": daily, "sleep": sleep, "sleep_stages": stages, "quality": quality, "weekly": weekly}


def weekly_rows(daily: list[dict], sleep: list[dict], quality: list[dict], workouts: list[dict],
                as_of: date) -> list[dict]:
    if not daily:
        return []
    by_day = {r["date"]: r for r in daily}
    hrv_flag = {r["date"]: r["hrv_outlier"] for r in quality}
    first, last = monday(date.fromisoformat(daily[0]["date"])), monday(date.fromisoformat(daily[-1]["date"]))
    rows = []
    week = first
    while week <= last:
        days = [(week + timedelta(days=i)).isoformat() for i in range(7)]
        rows_in = [by_day[d] for d in days if d in by_day]
        with_data = sum(1 for r in rows_in if r["steps"] is not None or r["hr_avg_bpm"] is not None)
        runs = [w for w in workouts if w["date"] in days and w["type"] == "RUNNING" and w["is_training"]]
        has_any = bool(with_data or runs)
        zone_runs = [w for w in runs if w["hard_share"] is not None]
        hard = sum(w["min_vigorous"] + w["min_peak"] for w in zone_runs)
        zone_total = sum(sum(w[f"min_{z}"] or 0 for z in ("light", "moderate", "vigorous", "peak"))
                         for w in zone_runs)
        main_sleep = [r["asleep_h"] for r in sleep if r["date"] in days and r["is_main"]]
        azm = [r["azm_points"] for r in rows_in if r["azm_points"] is not None]

        def total(key: str) -> float | None:
            values = [w[key] for w in runs if w[key] is not None]
            return sum(values) if values else (0.0 if has_any and not runs else None)

        rows.append({
            "week_start": days[0],
            "week_end": days[-1],
            "runs": len(runs) if has_any else None,
            "run_km": total("distance_km"),
            "long_run_km": max((w["distance_km"] for w in runs if w["distance_km"] is not None), default=None),
            "run_duration_min": total("duration_min"),
            "run_avg_hr_bpm": weighted_mean((w["avg_hr_bpm"], w["duration_min"]) for w in runs),
            "run_hard_share": hard / zone_total if zone_total else None,
            "azm_points": sum(azm) if azm else None,
            "sleep_h_avg": mean(main_sleep),
            "resting_hr_avg_bpm": mean(r["resting_hr_bpm"] for r in rows_in),
            "hrv_avg_ms": mean(r["hrv_ms"] for r in rows_in),
            "hrv_avg_ex_outliers_ms": mean(r["hrv_ms"] for r in rows_in if hrv_flag.get(r["date"]) is not True),
            "weight_kg_avg": mean(r["weight_kg"] for r in rows_in),
            "days_with_data": with_data,
            "is_complete": with_data == 7 and not any(r["is_partial"] for r in rows_in)
                           and date.fromisoformat(days[-1]) < as_of,
        })
        week += timedelta(days=7)
    return rows
