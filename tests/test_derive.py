"""derive: synthetic raw data in, CSV/SQLite tables out (see derive_fixtures.py)."""

from __future__ import annotations

import csv
import gzip
import math
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise

import pytest
from derive_fixtures import AS_OF, COORD_MARKERS, build_dataset, read_points

from google_health_claude.cli import main
from google_health_claude.derive import derive
from google_health_claude.derive.days import OUTLIER_RULES, OutlierRule, outlier_flags
from google_health_claude.derive.output import cell
from google_health_claude.derive.points import Point, hr_stats, segment_altitudes
from google_health_claude.derive.schema import TABLES
from google_health_claude.derive.values import mean, num


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    base = tmp_path_factory.mktemp("derive")
    data = base / "data"
    facts = build_dataset(data)
    out = base / "derived"
    db = base / "derived.sqlite"
    report = derive(data, out, db, log=lambda m: None)

    def table(name):
        with (out / f"{name}.csv").open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    return {"data": data, "out": out, "db": db, "report": report, "table": table, "facts": facts}


def by(rows, key):
    return {r[key]: r for r in rows}


# -- values --------------------------------------------------------------------------


def test_numeric_strings_and_durations_become_numbers():
    assert num("8165") == 8165
    assert num("1422.323s") == pytest.approx(1422.323)
    assert num(12) == 12 and num(None) is None and num("n/a") is None and num(True) is None


# Exact binary values whose plain left-to-right sum (Python < 3.12) and compensated sum()
# (Python 3.12+) both round differently from the correctly rounded math.fsum: averages
# must not depend on the Python version (a golden digest once differed on 3.10/3.11).
UNSTABLE_SUM = [-3 * 2.0**40, -(2.0**53), 5 * 2.0**-56, 5.0]


def test_float_sums_in_derive_are_correctly_rounded_on_every_python():
    exact = math.fsum(UNSTABLE_SUM) / len(UNSTABLE_SUM)
    assert sum(UNSTABLE_SUM) / len(UNSTABLE_SUM) != exact  # the trap is real here
    assert mean(UNSTABLE_SUM) == exact
    points = [
        Point(datetime.fromtimestamp(t, timezone.utc), t, None, float(t), alt, True)
        for t, alt in enumerate(UNSTABLE_SUM)
    ]
    smoothed = segment_altitudes(points, [0, 1, 2, 3])  # all four within the 30 s window
    assert set(smoothed.values()) == {exact}


def test_cells_keep_missing_empty_and_zero_as_zero():
    assert cell(None, "REAL") == "" and cell(0.0, "REAL") == "0"
    assert cell(1.23456, "REAL") == "1.235" and cell(-0.0001, "REAL") == "0"
    assert cell(True, "BOOL") == "true" and cell(None, "BOOL") == ""


# -- workouts ---------------------------------------------------------------------------


def test_duplicated_fitbit_run_is_merged_and_health_connect_copies_are_counted(run):
    workouts = by(run["table"]("workouts"), "id")
    assert set(workouts) == {
        "runA",
        "runB1",
        "runC",
        "runD",
        "walk1",
        "str1",
        "card1",
    }  # no hcB/hcX/runB2
    b = workouts["runB1"]
    assert b["merged_ids"] == "runB2" and b["n_raw_merged"] == "2" and b["n_hc_copies"] == "1"
    assert workouts["runA"]["n_raw_merged"] == "1" and workouts["runA"]["n_hc_copies"] == "0"
    assert workouts["runA"]["merged_ids"] == ""


def test_passive_walk_is_not_training_and_runs_are(run):
    workouts = by(run["table"]("workouts"), "id")
    assert workouts["walk1"]["is_training"] == "false" and workouts["walk1"]["tcx_file"] == ""
    assert workouts["walk1"]["has_gps"] == "false"
    assert workouts["runA"]["is_training"] == "true" and workouts["runA"]["has_gps"] == "true"
    assert workouts["str1"]["is_training"] == "false" and workouts["str1"]["has_gps"] == "false"


def test_summary_distance_is_official_not_the_tcx_total(run):
    a = by(run["table"]("workouts"), "id")["runA"]
    assert float(a["distance_km"]) == 2.52
    assert run["facts"]["track_a_total_m"] / 1000 != pytest.approx(2.52, abs=0.001)


def test_workout_units_and_derived_columns(run):
    a = by(run["table"]("workouts"), "id")["runA"]
    assert a["date"] == "2026-11-15" and a["start_local"] == "2026-11-15T10:00:00+01:00"
    assert float(a["duration_min"]) == pytest.approx(860 / 60, abs=0.001)
    assert float(a["elevation_gain_m"]) == 12
    assert float(a["min_vigorous"]) == pytest.approx(500 / 60, abs=0.001)
    assert float(a["hard_share"]) == pytest.approx(600 / 860, abs=0.001)
    assert float(a["peak_hr_bpm"]) == 155 and float(a["avg_hr_bpm"]) == 148
    assert float(a["pace_min_km"]) == pytest.approx(860 / 2.52 / 60, abs=0.001)
    assert a["tcx_file"] == "tcx/2026-11-15/runA.tcx.gz"


def test_late_utc_run_is_dated_in_berlin(run):
    d = by(run["table"]("workouts"), "id")["runD"]
    assert d["date"] == "2026-11-23" and d["start_local"] == "2026-11-23T00:30:00+01:00"


def test_duplicate_run_vo2max_entries_merge_onto_the_one_run(run):
    workouts = by(run["table"]("workouts"), "id")
    assert workouts["runB1"]["run_vo2max"] == "43.6"
    assert all(w["run_vo2max"] == "" for k, w in workouts.items() if k != "runB1")


def test_zero_trackpoint_tcx_is_handled(run):
    card = by(run["table"]("workouts"), "id")["card1"]
    assert card["has_gps"] == "false" and card["peak_hr_bpm"] == ""
    assert by(run["table"]("quality"), "date")["2026-11-18"]["tcx_empty"] == "true"
    assert by(run["table"]("quality"), "date")["2026-11-15"]["tcx_empty"] == "false"


# -- runs -----------------------------------------------------------------------------------


def test_every_split_belongs_to_a_workout_and_sums_to_its_distance(run):
    workouts = by(run["table"]("workouts"), "id")
    splits = run["table"]("run_splits")
    assert {s["workout_id"] for s in splits} == {"runA", "runB1", "runC", "runD"}
    for workout_id in {s["workout_id"] for s in splits}:
        assert workout_id in workouts
        total = sum(float(s["distance_km"]) for s in splits if s["workout_id"] == workout_id)
        assert total == pytest.approx(float(workouts[workout_id]["distance_km"]), rel=0.01)


def test_split_heart_rate_comes_from_the_split_time_window(run):
    splits = [s for s in run["table"]("run_splits") if s["workout_id"] == "runC"]
    assert [s["km_no"] for s in splits] == ["1", "2", "3"]
    assert float(splits[0]["avg_hr_bpm"]) == pytest.approx(150, abs=0.01)
    assert float(splits[2]["avg_hr_bpm"]) == pytest.approx(160, abs=0.01)
    assert 150 < float(splits[1]["avg_hr_bpm"]) < 160 and float(splits[1]["max_hr_bpm"]) == 160


def test_run_facts(run):
    facts = by(run["table"]("run_facts"), "workout_id")
    assert set(facts) == {"runA", "runB1", "runC", "runD"}
    a, c, d = facts["runA"], facts["runC"], facts["runD"]
    assert float(a["gps_gap_total_s"]) == 21 and float(d["gps_gap_total_s"]) == 8
    assert float(facts["runB1"]["gps_gap_total_s"]) == 0
    assert 50 <= float(a["walk_s"]) <= 70  # the 60 s walk, not the gap
    assert float(c["pace_fade_s_per_km"]) > 15  # slowed from 3.3 to 2.9 m/s
    assert float(c["hr_second_half_minus_first_half_bpm"]) == pytest.approx(9.4, abs=0.5)
    assert float(c["avg_hr_delta_vs_previous_run_bpm"]) == 5  # 155 vs runB 150
    assert float(c["avg_hr_delta_vs_4wk_avg_bpm"]) == 6  # 155 vs mean(148, 150)
    assert float(d["avg_hr_delta_vs_4wk_avg_bpm"]) == pytest.approx(
        150 - (148 + 150 + 155) / 3, abs=0.001
    )
    assert a["avg_hr_delta_vs_previous_run_bpm"] == "" and a["first_km_pace_min_km"] != ""
    assert float(c["fastest_km_pace_min_km"]) < float(c["slowest_km_pace_min_km"])
    assert a["slow_km_count"] == "0"


def test_personal_bests(run):
    bests = by(run["table"]("personal_bests"), "record")
    assert set(bests) == {"best_1km_split"}  # no run reaches 5 km
    assert bests["best_1km_split"]["workout_id"] == "runC"  # 3.3 m/s, the fastest km


def test_heart_rate_is_time_weighted_with_irregular_sampling(run):
    points = read_points(run["data"] / "tcx" / "2026-11-17" / "str1.tcx.gz")
    avg, peak = hr_stats(points)
    plain_mean = sum(p.hr for p in points) / len(points)
    assert avg == pytest.approx(115, abs=0.5)  # 100 held 3 s, 160 held 1 s
    assert plain_mean == pytest.approx(130, abs=0.5)
    assert peak == 160


def corrupt_gzip(content: bytes) -> bytes:
    """A gzip file with an intact header whose deflate stream is broken (zlib.error)."""
    good = gzip.compress(content, mtime=0)
    return good[:10] + bytes([0xFF]) + good[11:]


def test_tcx_with_a_corrupt_deflate_stream_is_unreadable(tmp_path):
    path = tmp_path / "broken.tcx.gz"
    path.write_bytes(corrupt_gzip(b"<TrainingCenterDatabase/>"))
    assert read_points(path) is None


def test_corrupt_tcx_gives_a_warning_not_a_crash(tmp_path):
    data = tmp_path / "data"
    build_dataset(data)
    tcx = data / "tcx" / "2026-11-15" / "runA.tcx.gz"
    tcx.write_bytes(corrupt_gzip(gzip.decompress(tcx.read_bytes())))
    report = derive(data, tmp_path / "out", as_of=AS_OF, log=lambda m: None)
    assert report.warnings == ["unreadable TCX for the RUNNING workout on 2026-11-15"]


# -- days -------------------------------------------------------------------------------------


def test_daily_values_units_and_missing(run):
    daily = by(run["table"]("daily"), "date")
    d = daily["2026-11-16"]
    assert d["steps"] == "8280" and float(d["distance_km"]) == 6.028
    assert d["azm_points"] == "82" and float(d["zone_vigorous_min"]) == 30
    assert d["zone_moderate_low_bpm"] == "117" and d["zone_peak_high_bpm"] == "220"
    assert float(d["sleep_temp_vs_baseline_c"]) == 0.25 and d["vo2max_level"] == "AVERAGE"
    assert daily["2026-11-12"]["spo2_avg_pct"] == ""  # missing stays empty
    assert "2026-11-04" not in daily


def test_zones_left_out_of_a_present_rollup_are_zero_but_a_missing_rollup_is_empty(run):
    daily = by(run["table"]("daily"), "date")
    zero, missing = daily["2026-11-11"], daily["2026-11-13"]
    assert (
        zero["zone_vigorous_min"] == "0"
        and zero["zone_peak_min"] == "0"
        and zero["zone_light_min"] != ""
    )
    assert zero["active_moderate_min"] == "0" and zero["active_vigorous_min"] == "0"
    assert missing["zone_light_min"] == "" and missing["zone_peak_min"] == ""
    assert daily["2026-11-12"]["active_moderate_min"] == "0"  # the fixture never sends MODERATE


def test_same_day_weigh_ins_merge_duplicates_then_average(run):
    daily = by(run["table"]("daily"), "date")
    assert float(daily["2026-11-10"]["weight_kg"]) == 78.501  # (78.001 + 79.001) / 2
    assert daily["2026-11-11"]["weight_kg"] == ""


def test_weigh_ins_go_to_weight_csv_with_duplicates_merged(run):
    weight = run["table"]("weight")
    assert [(r["timestamp_local"], r["weight_kg"], r["source"]) for r in weight] == [
        ("2026-09-03T07:30:00+02:00", "80.5", "FITBIT"),  # summer time: UTC+2
        ("2026-09-07T07:45:00+02:00", "80.25", "FITBIT"),
        (
            "2026-11-10T07:00:00+01:00",
            "78.001",
            "FITBIT;FITBIT_WEB_API",
        ),  # duplicate 47 s later merged
        ("2026-11-10T19:00:00+01:00", "79.001", "FITBIT"),
    ]


def test_weight_only_days_stay_out_of_the_day_tables(run):
    daily, quality, weekly = run["table"]("daily"), run["table"]("quality"), run["table"]("weekly")
    assert daily[0]["date"] == "2026-10-19" and quality[0]["date"] == "2026-10-19"
    assert not any(r["date"].startswith("2026-09") for r in daily + quality)
    assert weekly[0]["week_start"] == "2026-10-19"  # no rows for the September weigh-in weeks
    assert all(r["weight_kg"] == "" for r in daily if r["date"] != "2026-11-10")
    assert by(weekly, "week_start")["2026-11-09"]["weight_kg_avg"] == "78.501"


def test_last_three_days_are_partial(run):
    daily = by(run["table"]("daily"), "date")
    assert run["report"].as_of == AS_OF
    assert [d for d, r in daily.items() if r["is_partial"] == "true"] == [
        "2026-11-21",
        "2026-11-22",
        "2026-11-23",
    ]


def test_sleep_rows_and_stages(run):
    sleep = by(run["table"]("sleep"), "id")
    s = sleep["s15"]
    assert (
        s["date"] == "2026-11-15"
        and s["is_main"] == "true"
        and sleep["nap15"]["is_main"] == "false"
    )
    assert s["bed_local"] == "2026-11-14T23:30:00+01:00" and float(s["asleep_h"]) == 7
    assert s["deep_min"] == "60" and s["latency_min"] == "10"
    assert s["short_awakenings_count"] == "2" and s["short_awakenings_min"] == "3"
    assert float(s["efficiency_pct"]) == pytest.approx(420 / 450 * 100, abs=0.001)
    stages = [r for r in run["table"]("sleep_stages") if r["sleep_id"] == "s15"]
    assert sum(float(r["minutes"]) for r in stages) == 450 and stages[0]["stage"] == "AWAKE"


def test_hrv_outlier_is_flagged_on_its_day_only(run):
    quality = run["table"]("quality")
    assert [q["date"] for q in quality if q["hrv_outlier"] == "true"] == ["2026-11-20"]
    assert not any(
        q["resting_hr_outlier"] == "true" or q["spo2_outlier"] == "true" for q in quality
    )
    assert by(quality, "date")["2026-10-19"]["hrv_outlier"] == ""  # no history yet
    assert "daily-oxygen-saturation" in by(quality, "date")["2026-11-12"]["missing_metrics"]


def _last_flag(baseline, value, column):
    """Flag of `value` on the day after a run of daily `baseline` values."""
    days = [date(2026, 11, 1) + timedelta(days=i) for i in range(len(baseline) + 1)]
    return outlier_flags(dict(zip(days, [*baseline, value], strict=True)), OUTLIER_RULES[column])[
        days[-1]
    ]


def test_resting_hr_66_against_a_70_baseline_is_not_an_outlier():
    # Integer readings: the MAD is 0 for a flat baseline and 1 for 69-71; the 3 bpm floor holds.
    assert _last_flag([70] * 20, 66, "resting_hr_bpm") is False
    assert _last_flag([69, 70, 71] * 7, 66, "resting_hr_bpm") is False
    assert _last_flag([69, 70, 71] * 7, 80, "resting_hr_bpm") is True


def test_hrv_88_against_a_65_to_70_baseline_is_not_an_outlier():
    # Evenly spread 65-70 (median 67.5): the 12% floor gives a threshold of +-24.3 ms.
    assert _last_flag([65, 66, 67, 68, 69, 70] * 3, 88, "hrv_ms") is False
    assert _last_flag([65, 70] * 10, 88, "hrv_ms") is False


def test_hrv_dropout_is_flagged_even_in_a_noisy_baseline():
    # A first-weeks baseline with an earlier dropout (16) and low early readings:
    # median 52, spread 14.1, so 15 ms is only 2.6 spreads low. The MAD rule alone
    # misses it; the low-ratio check (below 50% of the median) catches it.
    baseline = [28, 39, 16, 47, 32, 63, 67, 54, 43, 62, 60, 58, 66, 53, 48, 51]
    days = [date(2026, 11, 1) + timedelta(days=i) for i in range(len(baseline) + 1)]
    series = dict(zip(days, [*baseline, 15], strict=True))
    assert outlier_flags(series, OutlierRule(min_share=0.12))[days[-1]] is False
    assert _last_flag(baseline, 15, "hrv_ms") is True
    assert _last_flag([65, 66, 67, 68, 69, 70] * 3, 14.9, "hrv_ms") is True


def test_outliers_need_14_values_of_history():
    assert _last_flag([70] * 13, 95, "resting_hr_bpm") is None
    assert _last_flag([70] * 14, 95, "resting_hr_bpm") is True


def test_weeks_run_monday_to_sunday(run):
    weekly = by(run["table"]("weekly"), "week_start")
    assert next(iter(weekly)) == "2026-10-19" and weekly["2026-11-09"]["week_end"] == "2026-11-15"
    nov9, nov16, nov23 = weekly["2026-11-09"], weekly["2026-11-16"], weekly["2026-11-23"]
    assert (
        nov9["runs"] == "1" and float(nov9["run_km"]) == 2.52
    )  # Sunday runA; passive walk not counted
    assert nov16["runs"] == "2"  # Monday duplicate counted once, plus runC
    assert nov23["runs"] == "1"  # runD: Sunday 23:30 UTC is Monday in Berlin
    assert nov9["is_complete"] == "true" and nov9["days_with_data"] == "7"
    assert nov16["is_complete"] == "false"  # 21 and 22 Nov are partial
    assert nov23["is_complete"] == "false"  # current week
    assert (
        weekly["2026-11-02"]["days_with_data"] == "6"
        and weekly["2026-11-02"]["is_complete"] == "false"
    )
    assert float(weekly["2026-11-16"]["hrv_avg_ms"]) > float(
        weekly["2026-11-16"]["hrv_avg_ex_outliers_ms"]
    )
    assert float(weekly["2026-11-09"]["sleep_h_avg"]) == 7  # the nap is not averaged in


# -- outputs ---------------------------------------------------------------------------------


def test_no_coordinates_anywhere(run):
    files = [*run["out"].iterdir(), run["db"]]
    for path in files:
        content = path.read_bytes()
        for marker in COORD_MARKERS:
            assert marker.encode() not in content, path.name
    with closing(sqlite3.connect(run["db"])) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(exercise_points)")}
    assert not columns & {"lat", "lon", "latitude", "longitude", "position"}


def test_sqlite_has_every_table_and_valid_foreign_keys(run):
    conn = sqlite3.connect(run["db"])
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert names == {t.name for t in TABLES} | {"notes"}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO run_splits (workout_id, km_no) VALUES ('nope', 1)")
        assert conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0] == 7
    finally:
        conn.close()


def test_exercise_points_do_not_bridge_gps_gaps(run):
    with closing(sqlite3.connect(run["db"])) as conn:
        rows = conn.execute(
            "SELECT t_s, speed_m_s, alt_m FROM exercise_points WHERE workout_id='runA' ORDER BY t_s"
        ).fetchall()
        passive = conn.execute(
            "SELECT COUNT(*), COUNT(speed_m_s), COUNT(hr_bpm) FROM exercise_points WHERE workout_id='str1'"
        ).fetchone()
    speeds = [s for _, s, _ in rows if s is not None]
    assert max(speeds) < 3.05  # the 57 m jump across the 21 s gap never shows up as speed
    assert not any(200 < t < 221 for t, _, _ in rows)  # nothing interpolated into the gap
    # Smoothing never mixes the altitude before the gap (~50 m) with the one after it (~80 m).
    assert all(abs(alt - 50) <= 3 for t, _, alt in rows if t <= 200)
    assert all(abs(alt - 80) <= 3 for t, _, alt in rows if t >= 221)
    with closing(sqlite3.connect(run["db"])) as conn:
        short_gap = conn.execute(
            "SELECT t_s, alt_m FROM exercise_points WHERE workout_id='runD' ORDER BY t_s"
        ).fetchall()
    assert all(abs(alt - 50) <= 3 for t, alt in short_gap if t <= 100)
    assert all(abs(alt - 80) <= 3 for t, alt in short_gap if t >= 108)
    assert passive[0] > 0 and passive[1] == 0 and passive[2] == passive[0]


def test_rerun_is_byte_stable_and_notes_are_never_overwritten(run, tmp_path):
    notes = run["out"] / "notes.csv"
    assert notes.read_text(encoding="utf-8") == "date,rpe_1_10,feeling,pain,shoes,other\n"
    notes.write_text(
        "date,rpe_1_10,feeling,pain,shoes,other\n2026-11-19,7,tired,,,\n", encoding="utf-8"
    )
    before = {p.name: p.read_bytes() for p in run["out"].iterdir()}
    db = tmp_path / "again.sqlite"
    report = derive(run["data"], run["out"], db, log=lambda m: None)
    assert report.files_changed == 0 and not report.notes_created
    assert {p.name: p.read_bytes() for p in run["out"].iterdir()} == before
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT date, rpe_1_10, feeling FROM notes").fetchall() == [
            ("2026-11-19", "7", "tired")
        ]


def test_output_inside_the_data_folder_is_refused(run):
    with pytest.raises(ValueError, match="must not be inside the data folder"):
        derive(run["data"], run["data"] / "derived", log=lambda m: None)


def test_cli_derive(run, tmp_path, capsys):
    assert (
        main(
            [
                "derive",
                "--data-dir",
                str(run["data"].parent),
                "--out",
                str(tmp_path / "o"),
                "--as-of",
                "2026-11-23",
            ]
        )
        == 0
    )
    assert (tmp_path / "o" / "workouts.csv").exists() and (tmp_path / "o" / "schema.md").exists()
    assert "workouts 7" in capsys.readouterr().err
    assert (
        main(["derive", "--data-dir", str(tmp_path / "missing"), "--out", str(tmp_path / "x")]) == 1
    )


def test_strength_fixture_is_irregular(run):
    times = [t for t, _, _ in run["facts"]["strength_points"]]
    assert {b - a for a, b in pairwise(times)} == {1, 3}
