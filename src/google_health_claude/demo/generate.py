"""Invented raw data in the layout fetch writes: a beginner's marathon build.

The data covers ``weeks`` training weeks from Monday 2026-10-05, plus the first
hours of the Monday after, the day of the last fetch (04:10 UTC, like the daily
job). Each week has 3-4 runs with a long run that grows, one strength session,
passive walks, daily rollups and summaries, sleep, and a few weigh-ins.

Edge cases that derive handles are placed so that every ``weeks >= 3`` has all
of them (weeks counted from 1):

* week 1: the Tuesday run's TCX repeats one timestamp; no SpO2 the night before
  Thursday; on Saturday a Health Connect walk that overlaps no Fitbit record.
* week 2: the easy run is on Monday 00:30 Berlin = Sunday 22:30 UTC (another UTC
  date and week); Wednesday has no time-in-heart-rate-zone rollup; Thursday's
  run loses GPS for 25 s; Saturday's run is recorded twice by the watch (3 s
  apart, merged by derive); Sunday's run-vo2-max arrives twice, then a nap.
* week 3: on Sunday 25 Oct the clocks go back; the night before spans the change.
* last week: Tuesday sick (resting-HR outlier, HRV dropout, no run); Wednesday
  no data at all; Saturday's strength TCX export came back empty, so no file
  (fetch does not store empty exports); Saturday to Monday are partial, Monday
  has only its first hours and no sleep yet.
* throughout: Health Connect copies of most runs, passive walks (which never get
  a TCX), passive strength sampled every 1-3 s, numbers as strings and "123s"
  durations (see shapes.py), rest days whose rollups leave zero zones and levels
  out, summary distances that differ from the TCX total, run-walk long runs.
* weight: two weigh-ins weeks before the start (weight-only daily files, as the
  180-day lookback produces), a duplicate from another platform 47 s later in
  week 2, two weigh-ins on the Monday of week 3.

Determinism: no wall-clock time and no unseeded randomness. Every day, night
and workout draws from its own ``random.Random`` seeded with a string such as
``"7:run:2026-10-06"``, so earlier days do not depend on ``weeks``. JSON goes
through ``store.write_if_changed`` and TCX through ``workout_tcx.gzip_bytes``
(gzip mtime 0), exactly as fetch writes them. A seed gives the same content on
every platform, and the same bytes on the same Python and zlib build: gzip's
compressed stream differs between builds (Python 3.11 and 3.12 write zlib's OS
byte into the header, Windows builds from 3.14 use zlib-ng).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import TIMEZONE
from ..store import day_path, write_bytes_if_changed, write_if_changed
from ..workout_tcx import INDEX_NAME, gzip_bytes, tcx_path
from . import shapes
from .route import RunPlan, simulate_run
from .shapes import JsonDict, Span, Trackpoint

START = date(2026, 10, 5)  # a Monday
DEFAULT_SEED = 7
DEFAULT_WEEKS = 6
MIN_WEEKS = 3  # the sick day in the last week needs 14 days of history for outlier flags
MAX_WEEKS = 26
FETCH_AT_UTC = time(4, 10)

TZ = ZoneInfo(TIMEZONE)
HR_ZONES = (("LIGHT", 30, 125), ("MODERATE", 126, 148), ("VIGOROUS", 149, 172), ("PEAK", 173, 220))
TUE, WED, THU, SAT, SUN = 1, 2, 3, 5, 6


def offset_of(moment: datetime) -> int:
    offset = moment.astimezone(TZ).utcoffset()
    return int(offset.total_seconds()) if offset else 0


def at(day: date, hour: int, minute: int = 0) -> datetime:
    """A wall-clock time in TZ as an aware UTC datetime."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).astimezone(timezone.utc)


def local_day(moment: datetime) -> date:
    return moment.astimezone(TZ).date()


def jitter(rng: random.Random, width: float) -> float:
    return (rng.random() - 0.5) * width


def zone_seconds(samples: list[tuple[int, int]]) -> list[int]:
    """Seconds per heart-rate zone from (heart rate, seconds held) samples."""
    totals = [0, 0, 0, 0]
    for hr, seconds in samples:
        for i, (_, low, high) in enumerate(HR_ZONES):
            if low <= hr <= high:
                totals[i] += seconds
    return totals


@dataclass
class DayLoad:
    """What a day's workouts add to its daily rollups."""

    steps: int = 0
    distance_m: float = 0.0
    kcal: int = 0
    zone_s: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    peak_hr: int = 0
    walk_min: int = 0


@dataclass
class DemoReport:
    first_day: date
    as_of: date
    files: int
    workouts: int
    nights: int


@dataclass
class Dataset:
    seed: int
    weeks: int
    daily: dict[date, JsonDict] = field(default_factory=dict)
    sleep: dict[date, list[JsonDict]] = field(default_factory=dict)
    workouts: dict[date, list[JsonDict]] = field(default_factory=dict)
    tcx: dict[date, dict[str, tuple[bytes, str]]] = field(default_factory=dict)
    load: dict[date, DayLoad] = field(default_factory=dict)
    run_vo2: dict[date, list[JsonDict]] = field(default_factory=dict)
    weights: dict[date, list[JsonDict]] = field(default_factory=dict)
    long_run_days: set[date] = field(default_factory=set)
    tcx_empty_days: list[date] = field(default_factory=list)

    # -- calendar -----------------------------------------------------------------------

    @property
    def as_of(self) -> date:
        return START + timedelta(days=7 * self.weeks)

    def day(self, week: int, weekday: int) -> date:
        return START + timedelta(days=7 * week + weekday)

    @property
    def sick_day(self) -> date:
        return self.day(self.weeks - 1, TUE)

    @property
    def missing_day(self) -> date:
        return self.day(self.weeks - 1, WED)

    def rng(self, *parts: object) -> random.Random:
        # Seeded on purpose: reproducible invented data, nothing security-related.
        return random.Random(":".join(str(p) for p in (self.seed, *parts)))  # noqa: S311

    def loaded(self, day: date) -> DayLoad:
        return self.load.setdefault(day, DayLoad())

    def progress(self, day: date) -> float:
        return (day - START).days / (7 * self.weeks)

    # -- workouts -----------------------------------------------------------------------

    def _store(self, record: JsonDict, start: datetime, tcx: bytes | None) -> None:
        day = local_day(start)
        self.workouts.setdefault(day, []).append(record)
        if tcx is not None:
            ex_id = record["name"].rsplit("/", 1)[-1]
            end = record["exercise"]["interval"]["endTime"]
            self.tcx.setdefault(day, {})[ex_id] = (tcx, end)

    def run(
        self,
        start: datetime,
        km: float,
        speed: float,
        *,
        long: bool = False,
        vo2_copies: int = 0,
        hc_copy: bool | None = None,
        recorded_twice: bool = False,
        gap_at: float | None = None,
        repeat_at_s: int | None = None,
    ) -> None:
        day = local_day(start)
        ex_id = f"run-{day:%Y%m%d}"
        rng = self.rng("run", day)
        distance_m = km * 1000 + jitter(rng, 300)
        gap_s = None
        if gap_at is not None:
            gap_from = round(distance_m * gap_at / speed)
            gap_s = (gap_from, gap_from + 26)  # no fixes for the 25 s in between
        plan = RunPlan(
            distance_m=distance_m,
            speed_m_s=speed,
            fitness=self.progress(day),
            walk_every_s=1500 if long else None,
            gap_s=gap_s,
            repeat_at_s=repeat_at_s,
        )
        track = simulate_run(plan, rng)
        summary_m = round(track.device_distance_m * (1 + jitter(rng, 0.016)), 1)
        zones = zone_seconds([(hr, 1) for hr in track.hr_seconds])
        active = track.duration_s
        steps = round(active / 60 * (162 + rng.random() * 6))
        kcal = round(summary_m / 1000 * 72 + active / 120)
        gps = [
            (p.t_s, p.dist_m) for p in track.points if p.lat is not None and p.dist_m is not None
        ]

        def record(record_id: str, begin: datetime) -> JsonDict:
            return shapes.exercise(
                record_id,
                "RUNNING",
                begin,
                active,
                offset_of=offset_of,
                distance_m=summary_m,
                avg_hr=track.avg_hr,
                kcal=kcal,
                step_count=steps,
                zone_s=zones,
                azm=(zones[1] + 2 * (zones[2] + zones[3])) // 60,
                elevation_mm=round(track.elevation_gain_m * 1000),
                split_list=shapes.splits(begin, gps, offset_of=offset_of),
            )

        self._store(record(ex_id, start), start, shapes.tcx_document(start, track.points))
        if recorded_twice:
            again = start + timedelta(seconds=3)
            self._store(
                record(f"{ex_id}-b", again), again, shapes.tcx_document(again, track.points)
            )
        if hc_copy is None:
            hc_copy = rng.random() < 0.75
        if hc_copy:
            begin = start + timedelta(seconds=5 + int(rng.random() * 10))
            copy = shapes.exercise(
                f"hc-{ex_id}",
                "RUNNING",
                begin,
                active + int(jitter(rng, 20)),
                offset_of=offset_of,
                platform="HEALTH_CONNECT",
                distance_m=round(summary_m * (1 + jitter(rng, 0.08)), 1),
                step_count=steps + int(jitter(rng, 200)),
            )
            self._store(copy, begin, None)
        for n in range(vo2_copies):
            value = round(41.6 + 1.6 * plan.fitness + jitter(rng, 0.6), 1)
            if n:
                value = self.run_vo2[day][-1]["runVo2Max"]["runVo2Max"]  # an exact repeat
            stamp = start + timedelta(seconds=3 * n)
            self.run_vo2.setdefault(day, []).append(
                shapes.run_vo2_max(value, stamp, offset_of=offset_of)
            )

        load = self.loaded(day)
        load.steps += steps
        load.distance_m += summary_m
        load.kcal += kcal
        load.zone_s = [a + b for a, b in zip(load.zone_s, zones, strict=True)]
        load.peak_hr = max(load.peak_hr, track.peak_hr)
        if long:
            self.long_run_days.add(day)

    def strength(self, start: datetime, *, tcx_empty: bool = False) -> None:
        day = local_day(start)
        rng = self.rng("strength", day)
        points: list[Trackpoint] = []
        held: list[tuple[int, int]] = []
        t, duration = 0, 2400 + int(rng.random() * 600)
        while t <= duration:
            in_set = (t // 90) % 2 == 1  # 90 s sets, 90 s rests
            hr = round((124 if in_set else 98) + jitter(rng, 8))
            step = 1 + int(rng.random() * 3)  # passive sessions sample every 1-3 s
            points.append(Trackpoint(float(t), hr))
            held.append((hr, step))
            t += step
        active = int(points[-1].t_s)
        avg_hr = round(sum(hr * s for hr, s in held) / sum(s for _, s in held))
        zones = zone_seconds(held)
        ex_id = f"strength-{day:%Y%m%d}"
        record = shapes.exercise(
            ex_id,
            "STRENGTH_TRAINING",
            start,
            active,
            offset_of=offset_of,
            method="PASSIVELY_MEASURED",
            avg_hr=avg_hr,
            kcal=round(active / 60 * 4.5),
            zone_s=zones,
            azm=(zones[1] + 2 * (zones[2] + zones[3])) // 60,
        )
        if tcx_empty:
            self.tcx_empty_days.append(day)
        self._store(
            record, start, None if tcx_empty else shapes.tcx_document(start, points, "Other")
        )
        load = self.loaded(day)
        load.kcal += round(active / 60 * 4.5)
        load.zone_s = [a + b for a, b in zip(load.zone_s, zones, strict=True)]

    def walk(self, start: datetime, *, platform: str = "FITBIT") -> None:
        day = local_day(start)
        rng = self.rng("walk", platform, day)
        active = 1500 + int(rng.random() * 600)
        distance_m = round(active * (1.3 + rng.random() * 0.15), 1)
        steps = round(active / 60 * (105 + rng.random() * 10))
        ex_id = f"{'walk' if platform == 'FITBIT' else 'hc-walk'}-{day:%Y%m%d}"
        if platform != "FITBIT":
            record = shapes.exercise(
                ex_id,
                "WALKING",
                start,
                active,
                offset_of=offset_of,
                platform=platform,
                distance_m=distance_m,
                step_count=steps,
            )
            self._store(record, start, None)
            return
        record = shapes.exercise(
            ex_id,
            "WALKING",
            start,
            active,
            offset_of=offset_of,
            method="PASSIVELY_MEASURED",
            distance_m=distance_m,
            avg_hr=round(98 + rng.random() * 10),
            kcal=round(active / 60 * 4),
            step_count=steps,
            zone_s=(active, 0, 0, 0),
            azm=0,
        )
        self._store(record, start, None)  # passive walks never get a TCX
        load = self.loaded(day)
        load.steps += steps
        load.distance_m += distance_m
        load.kcal += round(active / 60 * 4)
        load.walk_min += active // 60

    # -- the plan ------------------------------------------------------------------------

    def plan_week(self, w: int) -> None:
        rng = self.rng("week", w)
        easy, steady, long_speed = 2.45 + 0.02 * w, 2.75 + 0.02 * w, 2.35 + 0.02 * w
        long_km = min(9 + 2 * w - (3 if w % 4 == 3 else 0), 30)
        last = w == self.weeks - 1

        if w == 0:
            self.run(at(self.day(w, TUE), 18, 30), 5.0, easy, repeat_at_s=600)
            self.walk(at(self.day(w, SAT), 10, 0), platform="HEALTH_CONNECT")
        elif w == 1:
            self.run(at(self.day(w, 0), 0, 30), 5.0, easy)  # Sunday 22:30 UTC
            self.run(at(self.day(w, SAT), 9, 30), 4.5, easy, recorded_twice=True)
        elif not last:
            self.run(at(self.day(w, TUE), 18, 30 + int(rng.random() * 20)), 5.0 + 0.3 * w, easy)
            if rng.random() < 0.5:
                self.run(at(self.day(w, SAT), 9, 30), 4.5 + 0.2 * w, easy)

        if last:
            self.run(at(self.day(w, THU), 18, 30), 4.0, easy)  # comeback after the sick day
            self.strength(at(self.day(w, SAT), 10, 0), tcx_empty=True)
        else:
            self.strength(at(self.day(w, WED), 19, 30))
            self.run(
                at(self.day(w, THU), 18, 30),
                6.0 + 0.4 * w,
                steady,
                vo2_copies=1,
                gap_at=0.4 if w == 1 else None,
            )
        self.run(
            at(self.day(w, SUN), 9, 0),
            long_km,
            long_speed,
            long=True,
            vo2_copies=2 if w == 1 else 1,
            hc_copy=True if w == 0 else None,
        )
        for weekday in (0, WED, 4):  # lunch walks on Monday, Wednesday, Friday
            day = self.day(w, weekday)
            if day not in (self.sick_day, self.missing_day) and rng.random() < 0.5:
                self.walk(at(day, 12, 30 + int(rng.random() * 15)))

    def weigh_ins(self) -> None:
        def weigh(stamp: datetime, n: int = 0, platform: str = "FITBIT", extra: int = 0) -> None:
            day = local_day(stamp)
            rng = self.rng("weight", day)
            grams = round(78400 - 25 * (day - START).days + jitter(rng, 500)) + extra
            self.weights.setdefault(day, []).append(
                shapes.weight(f"w-{day:%Y%m%d}-{n}", platform, grams, stamp, offset_of=offset_of)
            )

        for days_before, hour, minute in ((46, 6, 40), (23, 7, 5)):
            weigh(at(START - timedelta(days=days_before), hour, minute))
        for w in range(self.weeks):
            monday = self.day(w, 0)
            if w == 1:
                weigh(at(monday, 6, 45))
                weight = self.weights[monday][-1]["weight"]["weightGrams"]
                self.weights[monday].append(
                    shapes.weight(
                        f"w-{monday:%Y%m%d}-1",
                        "FITBIT_WEB_API",
                        weight,
                        at(monday, 6, 45) + timedelta(seconds=47),
                        offset_of=offset_of,
                    )
                )
            elif w == 2:
                weigh(at(monday, 6, 40))
                weigh(at(monday, 19, 30), n=1, extra=600)
            elif w == 0 or self.rng("weigh", w).random() < 0.7:
                weigh(at(monday, 6, 40))

    # -- days -------------------------------------------------------------------------------

    def daily_metrics(self, day: date) -> JsonDict:
        rng = self.rng("daily", day)
        load = self.load.get(day, DayLoad())
        progress = self.progress(day)
        sick = day == self.sick_day
        base_steps = int((5000 if day.weekday() >= SAT else 6000) + rng.random() * 5000)
        if sick:
            base_steps = 2200 + int(rng.random() * 800)
        # Sick: +14 bpm clears the outlier threshold (3 x a 3 bpm floor) for any seed.
        rhr = round(58 - 3 * progress + jitter(rng, 2.4)) + (14 if sick else 0)
        hrv = 43 + 4 * progress + jitter(rng, 10)
        if day - timedelta(days=1) in self.long_run_days:
            hrv -= 4
        if sick:
            hrv *= 0.42  # a dropout: below half the usual value
        moderate_s = load.zone_s[1] + (int(rng.random() * 400) if rng.random() < 0.5 else 0)
        zone_s = [86400 - 1800 - moderate_s - sum(load.zone_s[2:]), moderate_s, *load.zone_s[2:]]
        vigorous_min = (load.zone_s[2] + load.zone_s[3]) // 60
        active_kcal = round(380 + rng.random() * 180 + load.kcal, 1)
        nightly_temp = 33.6 + 0.1 * progress + jitter(rng, 0.5) + (0.8 if sick else 0)
        spo2 = round(96.2 + jitter(rng, 1.4), 1)
        vo2 = round(41.5 + 1.5 * progress, 1)

        metrics: JsonDict = {
            "steps": shapes.steps(day, base_steps + load.steps),
            "distance": shapes.distance(day, round((base_steps * 0.72 + load.distance_m) * 1000)),
            "active-energy-burned": shapes.active_energy_burned(day, active_kcal),
            "total-calories": shapes.total_calories(day, round(1710 + active_kcal, 1)),
            "heart-rate": shapes.heart_rate(
                day,
                round(66 + jitter(rng, 5) + (4 if load.peak_hr else 0), 1),
                rhr - 5 - int(rng.random() * 3),
                load.peak_hr or 112 + int(rng.random() * 20),
            ),
            "active-minutes": shapes.active_minutes(
                day,
                {
                    "LIGHT": 150 + int(rng.random() * 110) + load.walk_min,
                    "MODERATE": moderate_s // 60,
                    "VIGOROUS": vigorous_min,
                },
            ),
            "active-zone-minutes": shapes.active_zone_minutes(
                day, moderate_s // 60, 2 * (load.zone_s[2] // 60), 2 * (load.zone_s[3] // 60)
            ),
            "time-in-heart-rate-zone": shapes.time_in_heart_rate_zone(
                day, dict(zip(shapes.ZONES, zone_s, strict=True))
            ),
            "sedentary-period": shapes.sedentary_period(
                day, int((9.5 + rng.random() * 2.5) * 3600) + (14400 if sick else 0)
            ),
            "daily-resting-heart-rate": shapes.resting_heart_rate(day, rhr),
            "daily-heart-rate-variability": shapes.heart_rate_variability(
                day,
                round(hrv, 1),
                round(hrv * 1.12 + jitter(rng, 4), 1),
                round(2.3 + rng.random() * 0.5, 2),
                rhr + 3 + int(rng.random() * 3),
            ),
            "daily-oxygen-saturation": shapes.oxygen_saturation(
                day,
                spo2,
                round(spo2 - 1.8 - rng.random() * 0.6, 1),
                round(spo2 + 1.2 + rng.random() * 0.6, 1),
                round(0.9 + rng.random() * 0.6, 2),
            ),
            "daily-respiratory-rate": shapes.respiratory_rate(
                day, round(14.8 + jitter(rng, 1.2) + (1.6 if sick else 0), 1)
            ),
            "daily-sleep-temperature-derivations": shapes.sleep_temperature(
                day,
                round(nightly_temp, 2),
                round(33.6 + 0.1 * progress, 2),
                round(0.25 + rng.random() * 0.15, 2),
            ),
            "daily-vo2-max": shapes.vo2_max(
                day, vo2, round(1.1 + rng.random() * 0.4, 2), "AVERAGE" if vo2 < 42.5 else "GOOD"
            ),
            "daily-heart-rate-zones": shapes.heart_rate_zones(day, HR_ZONES),
        }
        if day == self.day(0, THU):
            del metrics["daily-oxygen-saturation"]
        if day == self.day(1, WED):
            del metrics["time-in-heart-rate-zone"]
        return metrics

    def first_hours(self, day: date) -> JsonDict:
        """The day of the last fetch: only the hours before 05:10, nothing computed after waking."""
        rng = self.rng("daily", day)
        steps = 20 + int(rng.random() * 60)
        rhr = round(55 + jitter(rng, 2))
        return {
            "steps": shapes.steps(day, steps),
            "distance": shapes.distance(day, round(steps * 0.72 * 1000)),
            "active-energy-burned": shapes.active_energy_burned(day, round(rng.random() * 4, 1)),
            "total-calories": shapes.total_calories(day, round(368 + rng.random() * 6, 1)),
            "heart-rate": shapes.heart_rate(
                day, round(rhr + 1 + rng.random() * 2, 1), rhr - 4, rhr + 18
            ),
            "active-minutes": shapes.active_minutes(day, {"LIGHT": 0, "MODERATE": 0}),
            "active-zone-minutes": shapes.active_zone_minutes(day, 0, 0, 0),
            "time-in-heart-rate-zone": shapes.time_in_heart_rate_zone(day, {"LIGHT": 18600}),
            "sedentary-period": shapes.sedentary_period(day, 600 + int(rng.random() * 300)),
            "daily-heart-rate-zones": shapes.heart_rate_zones(day, HR_ZONES),
        }

    # -- sleep --------------------------------------------------------------------------------

    def night(self, wake_day: date) -> JsonDict:
        rng = self.rng("sleep", wake_day)
        weekend = wake_day.weekday() >= SAT  # Friday and Saturday nights
        bed = at(wake_day - timedelta(days=1), 22, 40) + timedelta(
            minutes=int(rng.random() * 50) + (40 if weekend else 0)
        )
        wake = at(wake_day, 7, 20) if weekend else at(wake_day, 6, 25)
        wake += timedelta(minutes=int(rng.random() * (40 if weekend else 25)))
        if wake_day == self.sick_day:
            wake += timedelta(minutes=70)
        blocks = self._stage_blocks(rng, int((wake - bed).total_seconds() // 60))
        return self._session(f"sleep-{wake_day:%Y%m%d}", bed, blocks, rng, main=True)

    def nap(self, day: date) -> JsonDict:
        rng = self.rng("nap", day)
        blocks = [("AWAKE", 4), ("LIGHT", 26 + int(rng.random() * 6)), ("AWAKE", 2)]
        return self._session(f"nap-{day:%Y%m%d}", at(day, 15, 5), blocks, rng, main=False)

    @staticmethod
    def _stage_blocks(rng: random.Random, total: int) -> list[tuple[str, int]]:
        latency = 6 + int(rng.random() * 12)
        after = 1 + int(rng.random() * 4)
        blocks = [("AWAKE", latency)]
        used, cycle = latency, 0
        while used < total - after:
            for stage, minutes in (
                ("LIGHT", 25 + rng.random() * 20),
                ("DEEP", max(35 - 8 * cycle, 6) + rng.random() * 8),
                ("LIGHT", 8 + rng.random() * 8),
                ("REM", min(12 + 6 * cycle, 35) + rng.random() * 8),
            ):
                length = min(int(minutes), total - after - used)
                if length <= 0:
                    break
                blocks.append((stage, length))
                used += length
            if rng.random() < 0.7 and used + 10 < total - after - 30:
                blocks.append(("AWAKE", 3 + int(rng.random() * 7)))
                used = sum(m for _, m in blocks)
            cycle += 1
        blocks.append(("AWAKE", total - used))
        return blocks

    @staticmethod
    def _session(
        sleep_id: str,
        bed: datetime,
        blocks: list[tuple[str, int]],
        rng: random.Random,
        *,
        main: bool,
    ) -> JsonDict:
        stages: list[Span] = []
        awakenings: list[Span] = []
        cursor = bed
        for stage, minutes in blocks:
            end = cursor + timedelta(minutes=minutes)
            stages.append(Span(stage, cursor, end))
            if stage == "LIGHT" and minutes >= 5 and len(awakenings) < 4 and rng.random() < 0.35:
                begin = cursor + timedelta(minutes=2)
                awakenings.append(Span("LIGHT", begin, begin + timedelta(minutes=1)))
            cursor = end
        totals = {name: [0, 0] for name in ("AWAKE", "LIGHT", "DEEP", "REM")}
        for stage, minutes in blocks:
            totals[stage][0] += minutes
            totals[stage][1] += 1
        in_bed = sum(m for _, m in blocks)
        summary = shapes.SleepSummary(
            asleep_min=in_bed - totals["AWAKE"][0],
            awake_min=totals["AWAKE"][0],
            in_period_min=in_bed - blocks[0][1],
            to_fall_asleep_min=blocks[0][1],
            after_wake_up_min=blocks[-1][1],
            stages=tuple((name, m, n) for name, (m, n) in totals.items()),
        )
        return shapes.sleep_session(
            sleep_id, bed, cursor, stages, summary, awakenings, main=main, offset_of=offset_of
        )

    # -- assembly -------------------------------------------------------------------------------

    def build(self) -> None:
        for w in range(self.weeks):
            self.plan_week(w)
        self.weigh_ins()
        day = START
        while day <= self.as_of:
            if day != self.missing_day:
                metrics = self.first_hours(day) if day == self.as_of else self.daily_metrics(day)
                if day in self.run_vo2:
                    metrics["run-vo2-max"] = self.run_vo2[day]
                self.daily[day] = metrics
                if day < self.as_of:
                    self.sleep[day] = [self.night(day)]
            day += timedelta(days=1)
        self.sleep[self.day(1, SUN)].append(self.nap(self.day(1, SUN)))
        for day, points in self.weights.items():
            self.daily.setdefault(day, {})["weight"] = points

    def meta(self) -> JsonDict:
        window = [self.as_of - timedelta(days=d) for d in (2, 1, 0)]
        fetched = sum(len(self.tcx.get(d, {})) for d in window)
        empty = sum(1 for d in self.tcx_empty_days if d in window)
        return {
            "last_fetch_utc": shapes.z(datetime.combine(self.as_of, FETCH_AT_UTC, timezone.utc)),
            "window": [window[0].isoformat(), window[-1].isoformat()],
            "api_errors": {},
            "incomplete_days": [],
            "tcx": {"fetched": fetched, "skipped": 0, "empty": empty, "failed": 0, "failures": []},
        }

    def write(self, out: Path) -> int:
        files = 0
        for day, metrics in sorted(self.daily.items()):
            payload: JsonDict = {"date": day.isoformat(), "metrics": metrics}
            files += write_if_changed(day_path(out, "daily", day), payload)
        lists: tuple[tuple[str, str, dict[date, list[JsonDict]]], ...] = (
            ("sleep", "sessions", self.sleep),
            ("workouts", "workouts", self.workouts),
        )
        for category, key, by_day in lists:
            for day, records in sorted(by_day.items()):
                payload = {"date": day.isoformat(), key: sorted(records, key=_record_order)}
                files += write_if_changed(day_path(out, category, day), payload)
        for day, entries in sorted(self.tcx.items()):
            for ex_id, (xml, _) in sorted(entries.items()):
                files += write_bytes_if_changed(tcx_path(out, day, ex_id), gzip_bytes(xml))
            index = {ex_id: end for ex_id, (_, end) in sorted(entries.items())}
            files += write_if_changed(out / "tcx" / day.isoformat() / INDEX_NAME, index)
        files += write_if_changed(out / "_meta.json", self.meta())
        return files


def _record_order(record: JsonDict) -> tuple[str, str]:
    body = record.get("exercise") or record.get("sleep") or {}
    return str(body.get("interval", {}).get("startTime", "")), str(record.get("name", ""))


def generate(out_dir: Path, seed: int = DEFAULT_SEED, weeks: int = DEFAULT_WEEKS) -> DemoReport:
    """Write the demo data set into ``out_dir``, which must be empty or not exist."""
    if not MIN_WEEKS <= weeks <= MAX_WEEKS:
        raise ValueError(f"--weeks must be between {MIN_WEEKS} and {MAX_WEEKS}")
    out_dir = Path(out_dir)
    if out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir())):
        raise ValueError(f"{out_dir} is not an empty folder; demo never overwrites files")
    data = Dataset(seed, weeks)
    data.build()
    files = data.write(out_dir)
    return DemoReport(
        first_day=START,
        as_of=data.as_of,
        files=files,
        workouts=sum(len(items) for items in data.workouts.values()),
        nights=sum(len(items) for items in data.sleep.values()),
    )
