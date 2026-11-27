"""demo: invented raw data in fetch's layout, deterministic, with every derive edge case."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
import shutil
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path

import pytest

from google_health_claude.cli import main
from google_health_claude.demo import DEFAULT_WEEKS, MIN_WEEKS, generate
from google_health_claude.demo.route import ANCHOR_LAT, ANCHOR_LON
from google_health_claude.derive import derive
from google_health_claude.derive.points import read_points
from google_health_claude.derive.values import TZ, timestamp
from google_health_claude.fetch import run as fetch
from google_health_claude.fetch.store import load_json
from google_health_claude.fetch.tcx import INDEX_NAME, gzip_bytes
from google_health_claude.records import tcx_eligible

SEED = 5  # not the golden seed: these checks must hold for any seed


# The shortest data set and the default one: weeks between the second and the last
# follow the ordinary schedule, which only the longer one has.
@pytest.fixture(scope="module", params=[MIN_WEEKS, DEFAULT_WEEKS], ids=lambda w: f"{w}weeks")
def demo(request, tmp_path_factory):
    base = tmp_path_factory.mktemp("demo")
    raw = base / "raw"
    generate(raw, SEED, request.param)
    out = base / "out"
    report = derive(raw, out, log=lambda m: None)

    def table(name):
        with (out / f"{name}.csv").open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    return {"raw": raw, "table": table, "report": report}


def digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
    return h.hexdigest()


def by(rows, key):
    return {r[key]: r for r in rows}


def records(raw: Path, category: str, key: str):
    for path in sorted((raw / category).glob("*.json")):
        for item in load_json(path)[key]:
            yield date.fromisoformat(path.stem), item


def start_of(record) -> datetime:
    return timestamp(record["exercise"]["interval"]["startTime"])


# -- determinism and layout ------------------------------------------------------------


def test_same_seed_gives_the_same_bytes_and_another_seed_does_not(tmp_path):
    # Exact bytes, gzip included, hold within one Python and zlib build; across
    # builds the golden test compares TCX files by their decompressed content.
    for name, seed in (("a", 3), ("b", 3), ("c", 4)):
        generate(tmp_path / name, seed, MIN_WEEKS)
    assert digest(tmp_path / "a") == digest(tmp_path / "b")
    assert digest(tmp_path / "a") != digest(tmp_path / "c")


def test_files_are_exactly_what_fetch_writes(demo, tmp_path):
    copy = tmp_path / "raw"
    shutil.copytree(demo["raw"], copy)
    # Saving every file again through fetch's own merge code changes nothing.
    for path in sorted((copy / "daily").glob("*.json")):
        day = date.fromisoformat(path.stem)
        assert not fetch._save_daily(copy, day, load_json(path)["metrics"]), path.name
    for category, key in (("sleep", "sessions"), ("workouts", "workouts")):
        for path in sorted((copy / category).glob("*.json")):
            day = date.fromisoformat(path.stem)
            assert not fetch._save_list(copy, category, key, day, load_json(path)[key])
    assert digest(copy) == digest(demo["raw"])


def test_records_sit_in_the_files_fetch_puts_them_in(demo):
    for path in sorted((demo["raw"] / "daily").glob("*.json")):
        day = date.fromisoformat(path.stem)
        metrics = load_json(path)["metrics"]
        for data_type in ("weight", "run-vo2-max"):
            for point in metrics.get(data_type, []):
                assert fetch.sample_day(point, data_type) == day
    for day, record in records(demo["raw"], "workouts", "workouts"):
        assert start_of(record).astimezone(TZ).date() == day  # civil start date
    for day, session in records(demo["raw"], "sleep", "sessions"):
        end = timestamp(session["sleep"]["interval"]["endTime"])
        assert end.astimezone(TZ).date() == day  # wake-up date

    # TCX: gzip with mtime 0 as fetch writes it, only for eligible workouts, all indexed.
    workouts = {
        r["name"].rsplit("/", 1)[-1]: r for _, r in records(demo["raw"], "workouts", "workouts")
    }
    for folder in sorted((demo["raw"] / "tcx").iterdir()):
        files = {p.name[: -len(".tcx.gz")]: p for p in folder.glob("*.tcx.gz")}
        assert set(load_json(folder / INDEX_NAME)) == set(files)
        for ex_id, path in files.items():
            content = path.read_bytes()
            assert gzip_bytes(gzip.decompress(content)) == content
            assert tcx_eligible(workouts[ex_id])
    meta = load_json(demo["raw"] / "_meta.json")
    assert set(meta) == {"last_fetch_utc", "window", "api_errors", "incomplete_days", "tcx"}


def test_the_route_is_an_invented_loop_far_from_bavaria(demo):
    assert not (47.2 <= ANCHOR_LAT <= 50.6 and 8.9 <= ANCHOR_LON <= 13.9)  # Bavaria's box
    positions = []
    for path in (demo["raw"] / "tcx").glob("*/*.tcx.gz"):
        xml = gzip.decompress(path.read_bytes()).decode()
        lats = re.findall(r"<LatitudeDegrees>([^<]+)<", xml)
        lons = re.findall(r"<LongitudeDegrees>([^<]+)<", xml)
        positions += zip(map(float, lats), map(float, lons), strict=True)
    assert positions
    assert all(
        abs(lat - ANCHOR_LAT) < 0.02 and abs(lon - ANCHOR_LON) < 0.025 for lat, lon in positions
    )


# -- edge cases, as derive sees them -------------------------------------------------------


def test_workout_edge_cases(demo):
    raw, workouts = demo["raw"], by(demo["table"]("workouts"), "id")
    raw_records = [r for _, r in records(raw, "workouts", "workouts")]
    hc = [r for r in raw_records if r["dataSource"]["platform"] == "HEALTH_CONNECT"]
    assert hc and not any(r["name"].rsplit("/", 1)[-1] in workouts for r in hc)
    assert any(w["n_hc_copies"] == "1" for w in workouts.values())
    assert any(r["exercise"]["exerciseType"] == "WALKING" for r in hc)  # overlaps nothing

    merged = [w for w in workouts.values() if w["merged_ids"]]
    assert len(merged) == 1 and merged[0]["n_raw_merged"] == "2"

    # Monday 00:30 Berlin is Sunday in UTC, and the run counts in the Monday's week.
    fitbit = [r for r in raw_records if r["dataSource"]["platform"] == "FITBIT"]
    midnight = [r for r in fitbit if start_of(r).date() != start_of(r).astimezone(TZ).date()]
    assert len(midnight) == 1
    row = workouts[midnight[0]["name"].rsplit("/", 1)[-1]]
    assert row["start_local"].endswith("T00:30:00+02:00")
    week = by(demo["table"]("weekly"), "week_start")[row["date"]]
    assert int(week["runs"]) >= 1

    offsets = {w["start_local"][-6:] for w in workouts.values()}
    assert offsets == {"+02:00", "+01:00"}  # the clocks go back during the data

    facts = demo["table"]("run_facts")
    assert sum(float(f["gps_gap_total_s"]) > 5 for f in facts) == 1
    assert any(float(f["walk_s"]) >= 60 for f in facts)  # run-walk long runs

    walks = [w for w in workouts.values() if w["type"] == "WALKING"]
    assert walks and all(w["is_training"] == "false" and not w["tcx_file"] for w in walks)

    runs = [w for w in workouts.values() if w["type"] == "RUNNING"]
    assert all(w["has_gps"] == "true" for w in runs)
    assert sum(1 for w in workouts.values() if w["run_vo2max"]) >= 3
    assert {r["record"] for r in demo["table"]("personal_bests")} >= {
        "best_1km_split",
        "best_run_5k",
    }


def test_tcx_edge_cases(demo):
    raw, workouts = demo["raw"], by(demo["table"]("workouts"), "id")
    repeated = 0
    for path in (raw / "tcx").glob("*/*.tcx.gz"):
        times = re.findall(r"<Time>([^<]+)<", gzip.decompress(path.read_bytes()).decode())
        repeated += len(times) - len(set(times))
        assert len(read_points(path)) == len(set(times))  # the first of a repeat wins
    assert repeated == 1

    strength = next(
        w for w in workouts.values() if w["type"] == "STRENGTH_TRAINING" and w["tcx_file"]
    )
    points = read_points(raw / strength["tcx_file"])
    assert {round(b.ts - a.ts) for a, b in pairwise(points)} == {1, 2, 3}

    with_tcx = [w for w in workouts.values() if w["type"] == "RUNNING"]
    tcx_km = [read_points(raw / w["tcx_file"])[-1].dist_m / 1000 for w in with_tcx]
    assert all(
        float(w["distance_km"]) != pytest.approx(km, abs=0.001)
        for w, km in zip(with_tcx, tcx_km, strict=True)
    )  # the summary distance is not the TCX total


def test_day_edge_cases(demo):
    daily, quality = by(demo["table"]("daily"), "date"), by(demo["table"]("quality"), "date")
    days = sorted(daily)
    first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    assert (last - first).days + 1 == len(days) + 1  # exactly one day without any data

    flagged = {
        k: [d for d, q in quality.items() if q[k] == "true"]
        for k in ("hrv_outlier", "resting_hr_outlier")
    }
    assert (
        len(flagged["resting_hr_outlier"]) == 1
        and flagged["hrv_outlier"] == flagged["resting_hr_outlier"]
    )
    assert quality[days[0]]["resting_hr_outlier"] == ""  # no history yet

    assert [d for d in days if daily[d]["is_partial"] == "true"] == days[-3:]
    assert "sleep" in quality[days[-1]]["missing_metrics"] and daily[days[-1]]["hrv_ms"] == ""
    assert [d for d, q in quality.items() if q["tcx_empty"] == "true"] == [days[-3]]

    complete = days[:-3]
    no_spo2 = [d for d in complete if daily[d]["spo2_avg_pct"] == ""]
    no_zones = [d for d in complete if daily[d]["zone_light_min"] == ""]
    assert len(no_spo2) == 1 and "daily-oxygen-saturation" in quality[no_spo2[0]]["missing_metrics"]
    assert (
        len(no_zones) == 1 and "time-in-heart-rate-zone" in quality[no_zones[0]]["missing_metrics"]
    )
    rest_days = [
        d for d in complete if d not in no_zones and daily[d]["active_vigorous_min"] == "0"
    ]
    assert rest_days and all(daily[d]["zone_peak_min"] == "0" for d in rest_days)  # left out = 0

    sleep = demo["table"]("sleep")
    assert [s["is_main"] for s in sleep].count("false") == 1  # one nap
    dst_night = [
        s for s in sleep if s["bed_local"].endswith("+02:00") and s["wake_local"].endswith("+01:00")
    ]
    assert len(dst_night) == 1

    weight = demo["table"]("weight")
    assert sum(1 for w in weight if w["timestamp_local"] < days[0]) == 2  # 180-day lookback
    assert [w["source"] for w in weight].count("FITBIT;FITBIT_WEB_API") == 1
    two = [d for d in days if sum(1 for w in weight if w["timestamp_local"][:10] == d) == 2]
    assert len(two) == 1


def test_raw_numbers_are_strings_and_durations(demo):
    path = sorted((demo["raw"] / "daily").glob("*.json"))[-5]
    metrics = json.loads(path.read_text(encoding="utf-8"))["metrics"]
    assert isinstance(metrics["steps"]["steps"]["countSum"], str)
    assert metrics["sedentary-period"]["sedentaryPeriod"]["durationSum"].endswith("s")
    day = by(demo["table"]("daily"), "date")[path.stem]
    assert int(day["steps"]) == int(metrics["steps"]["steps"]["countSum"])


# -- command line --------------------------------------------------------------------------


def test_cli_demo_writes_once_and_refuses_bad_input(tmp_path, capsys):
    out = tmp_path / "raw"
    assert main(["demo", "--out", str(out), "--weeks", str(MIN_WEEKS), "--seed", "2"]) == 0
    assert "Every value is invented" in capsys.readouterr().err
    assert (out / "_meta.json").exists()
    assert main(["demo", "--out", str(out)]) == 1
    assert "not an empty folder" in capsys.readouterr().err
    assert main(["demo", "--out", str(tmp_path / "x"), "--weeks", str(MIN_WEEKS - 1)]) == 1
    assert "--weeks must be between" in capsys.readouterr().err
