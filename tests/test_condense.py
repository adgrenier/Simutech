"""End-to-end tests of the condensing run on synthetic Raw files.

Only the public entry point (`condense`) and its output Parquet files are used.
"""
import math
import os
import time

import numpy as np
import pandas as pd

from simutech import condense

MISSING = -7999


def write_raw(folder, name, start_local, values, table_id=101):
    """Write a headerless Raw file. `start_local` is a naive logger-clock (UTC-4)
    timestamp on a whole second; `values` holds one row per 20 ms step."""
    lines = []
    for i, row in enumerate(values):
        t = start_local + pd.Timedelta(milliseconds=20 * i)
        sec = t.second + t.microsecond / 1e6
        hhmm = t.hour * 100 + t.minute
        cols = [table_id, t.year, t.dayofyear, hhmm, f"{sec:.2f}", *row]
        lines.append(",".join(str(c) for c in cols))
    (folder / name).write_text("\n".join(lines) + "\n")


def make_config(tmp_path, channels=("Bend_00", "Bend_90"), offset=-4):
    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    return raw, {
        "output_folder": str(tmp_path / "out"),
        "loggers": {
            "Tower_Bot": {
                "folder": str(raw),
                "file_pattern": "tow_bot_*.csv",
                "clock_offset_hours": offset,
                "channels": [{"name": n, "unit": "mV/V"} for n in channels],
            }
        },
    }


def read_day(tmp_path, day):
    return pd.read_parquet(tmp_path / "out" / "Tower_Bot" / f"Tower_Bot_{day}.parquet")


def test_exact_statistics_and_utc_second_stamps(tmp_path):
    raw, cfg = make_config(tmp_path)
    t0 = pd.Timestamp("2026-03-10 08:15:00")  # logger clock, UTC-4
    k = np.arange(100)  # two whole seconds
    sine = 3.0 + 2.0 * np.sin(2 * np.pi * 5 * k / 50)  # 5 Hz: whole cycles per second
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", t0, [[sine[i], 1.5] for i in range(100)])

    result = condense(cfg, raw, "Tower_Bot")

    df = read_day(tmp_path, "2026-03-10")
    sel = df[df.channel == "Bend_00"].sort_values("timestamp_utc")
    assert list(sel.timestamp_utc) == [pd.Timestamp("2026-03-10 12:15:00"),
                                       pd.Timestamp("2026-03-10 12:15:01")]
    row = sel.iloc[0]
    assert row.n_valid == 50
    assert math.isclose(row["mean"], 3.0, abs_tol=1e-12)
    assert math.isclose(row.ac_rms, 2.0 / math.sqrt(2), rel_tol=1e-12)
    assert math.isclose(row["max"], sine[:50].max(), rel_tol=1e-12)
    assert math.isclose(row["min"], sine[:50].min(), rel_tol=1e-12)
    # standard deviation is the sample one (n - 1); AC RMS is the population one
    assert math.isclose(row["std"], (2.0 / math.sqrt(2)) * math.sqrt(50 / 49), rel_tol=1e-12)

    const = df[df.channel == "Bend_90"].iloc[0]
    assert (const["mean"], const.ac_rms, const["min"], const["max"], const["std"]) == (1.5, 0.0, 1.5, 1.5, 0.0)
    assert result.skipped == []


def test_missing_samples_excluded_and_empty_second_absent(tmp_path):
    raw, cfg = make_config(tmp_path)
    rows = []
    for i in range(150):  # three seconds
        sec = i // 50
        if sec == 0:
            v = [MISSING if i % 2 else 4.0, 1.0]  # half of channel 0 missing
        elif sec == 1:
            v = [MISSING, 2.0]  # channel 0 entirely missing
        else:
            v = [-1.0 if i % 2 else 1.0, 3.0]
        rows.append(v)
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"), rows)

    condense(cfg, raw, "Tower_Bot")

    df = read_day(tmp_path, "2026-03-10")
    ch0 = df[df.channel == "Bend_00"].sort_values("timestamp_utc")
    assert list(ch0.timestamp_utc) == [pd.Timestamp("2026-03-10 12:15:00"),
                                       pd.Timestamp("2026-03-10 12:15:02")]
    first = ch0.iloc[0]
    assert (first.n_valid, first["mean"], first["min"], first["max"], first.ac_rms) == (25, 4.0, 4.0, 4.0, 0.0)
    assert (df[df.channel == "Bend_90"].n_valid == 50).all()


def test_short_second_keeps_true_count(tmp_path):
    raw, cfg = make_config(tmp_path)
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"), [[2.0, 1.0]] * 30)
    condense(cfg, raw, "Tower_Bot")
    df = read_day(tmp_path, "2026-03-10")
    assert list(df[df.channel == "Bend_00"].n_valid) == [30]


def test_one_parquet_per_utc_day_split_at_utc_midnight(tmp_path):
    raw, cfg = make_config(tmp_path)
    # 19:59:59 logger clock = 23:59:59 UTC on 10 March; the next second is 11 March UTC.
    write_raw(raw, "tow_bot_2026_03_10_1959.csv", pd.Timestamp("2026-03-10 19:59:59"), [[1.0, 1.0]] * 100)
    result = condense(cfg, raw, "Tower_Bot")
    d10, d11 = read_day(tmp_path, "2026-03-10"), read_day(tmp_path, "2026-03-11")
    assert list(d10.timestamp_utc.unique()) == [pd.Timestamp("2026-03-10 23:59:59")]
    assert list(d11.timestamp_utc.unique()) == [pd.Timestamp("2026-03-11 00:00:00")]
    assert sorted(p.name for p in result.summary_files) == ["Tower_Bot_2026-03-10.parquet",
                                                            "Tower_Bot_2026-03-11.parquet"]


def test_clock_offset_is_a_per_logger_setting_without_dst(tmp_path):
    raw, cfg = make_config(tmp_path, offset=-5)
    write_raw(raw, "tow_bot_2026_07_01_0800.csv", pd.Timestamp("2026-07-01 08:00:00"), [[1.0, 1.0]] * 50)
    condense(cfg, raw, "Tower_Bot")
    df = read_day(tmp_path, "2026-07-01")
    assert df.timestamp_utc.iloc[0] == pd.Timestamp("2026-07-01 13:00:00")


def test_column_count_mismatch_skipped_and_listed(tmp_path):
    raw, cfg = make_config(tmp_path)
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"), [[1.0, 1.0]] * 50)
    write_raw(raw, "tow_bot_2026_03_10_0825.csv", pd.Timestamp("2026-03-10 08:25:00"), [[1.0, 1.0, 1.0]] * 50)
    (raw / "notes.txt").write_text("stray")
    result = condense(cfg, raw, "Tower_Bot")
    assert [p.name for p in result.skipped] == ["tow_bot_2026_03_10_0825.csv"]
    df = read_day(tmp_path, "2026-03-10")
    assert list(df.timestamp_utc.unique()) == [pd.Timestamp("2026-03-10 12:15:00")]


def test_rerun_gives_identical_output(tmp_path):
    raw, cfg = make_config(tmp_path)
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"),
              [[float(i % 7), 1.0] for i in range(100)])
    condense(cfg, raw, "Tower_Bot")
    first = read_day(tmp_path, "2026-03-10")
    condense(cfg, raw, "Tower_Bot")
    pd.testing.assert_frame_equal(first, read_day(tmp_path, "2026-03-10"))


def test_clipping_count_uses_configurable_threshold(tmp_path):
    raw, cfg = make_config(tmp_path)
    cfg["loggers"]["Tower_Bot"]["clipping_threshold"] = 10.0
    rows = [[10.0 if i < 5 else (-10.0 if i < 8 else 1.0), 9.99] for i in range(50)]
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"), rows)
    condense(cfg, raw, "Tower_Bot")
    df = read_day(tmp_path, "2026-03-10")
    assert list(df[df.channel == "Bend_00"].n_clipped) == [8]  # either rail
    assert list(df[df.channel == "Bend_90"].n_clipped) == [0]


def test_clipping_default_threshold_and_missing_never_clips(tmp_path):
    raw, cfg = make_config(tmp_path)
    rows = [[MISSING if i < 10 else 1.0, 7950.0 if i < 4 else 1.0] for i in range(50)]
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"), rows)
    condense(cfg, raw, "Tower_Bot")
    df = read_day(tmp_path, "2026-03-10")
    assert list(df[df.channel == "Bend_00"].n_clipped) == [0]
    assert list(df[df.channel == "Bend_90"].n_clipped) == [4]


def test_stuck_sensor_flagged_after_window_of_zero_std_seconds(tmp_path):
    raw, cfg = make_config(tmp_path)
    cfg["loggers"]["Tower_Bot"]["stuck_window_seconds"] = 3
    rows = []
    for sec in range(8):
        for i in range(50):
            # ch0 constant in seconds 1-3 (run of 3) and 5-6 (run of 2); ch1 always varies
            flat = sec in (1, 2, 3, 5, 6)
            rows.append([2.0 if flat else float(i % 5), float(i % 3)])
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"), rows)
    condense(cfg, raw, "Tower_Bot")
    df = read_day(tmp_path, "2026-03-10").sort_values("timestamp_utc")
    ch0 = df[df.channel == "Bend_00"]
    assert list(ch0.stuck) == [False, True, True, True, False, False, False, False]
    assert not df[df.channel == "Bend_90"].stuck.any()


def test_stuck_default_window_is_five_seconds(tmp_path):
    raw, cfg = make_config(tmp_path)
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"),
              [[1.0, 1.0]] * 300 + [[float(i % 2), 3.0] for i in range(50)])
    condense(cfg, raw, "Tower_Bot")
    df = read_day(tmp_path, "2026-03-10").sort_values("timestamp_utc")
    ch0 = df[df.channel == "Bend_00"]
    assert list(ch0.stuck) == [True] * 6 + [False]  # 6 flat seconds, then one varying
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"),
              [[1.0, 1.0]] * 200 + [[float(i % 2), 0.0] for i in range(50)])
    condense(cfg, raw, "Tower_Bot")
    ch0 = read_day(tmp_path, "2026-03-10").query("channel == 'Bend_00'").sort_values("timestamp_utc")
    assert not ch0.stuck.any()  # 4 flat seconds is below the window


def test_duplicate_seconds_across_files_keep_later_files_rows(tmp_path):
    raw, cfg = make_config(tmp_path)
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"),
              [[1.0, 1.0]] * 150)  # seconds 0, 1, 2
    write_raw(raw, "tow_bot_2026_03_10_0816.csv", pd.Timestamp("2026-03-10 08:15:02"),
              [[5.0, 5.0]] * 100)  # restarted file: seconds 2, 3
    condense(cfg, raw, "Tower_Bot")
    df = read_day(tmp_path, "2026-03-10")
    ch0 = df[df.channel == "Bend_00"].sort_values("timestamp_utc")
    assert list(ch0["mean"]) == [1.0, 1.0, 5.0, 5.0]
    assert list(ch0.n_valid) == [50, 50, 50, 50]  # never double-counted


# --- incremental, idempotent runs ---------------------------------------------------

def age(path, seconds=3600):
    """Make a Raw file look like it was last written `seconds` ago."""
    t = time.time() - seconds
    os.utime(path, (t, t))


def out_files(tmp_path):
    return sorted((tmp_path / "out" / "Tower_Bot").glob("*.parquet"))


def test_rerun_only_processes_new_files_and_keeps_earlier_output(tmp_path):
    raw, cfg = make_config(tmp_path)
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"), [[1.0, 1.0]] * 50)
    first = condense(cfg, raw, "Tower_Bot")
    assert first.summary["processed"] == 1
    day_before = read_day(tmp_path, "2026-03-10")

    again = condense(cfg, raw, "Tower_Bot")
    assert (again.summary["processed"], again.summary["already_done"]) == (0, 1)
    pd.testing.assert_frame_equal(day_before, read_day(tmp_path, "2026-03-10"))

    write_raw(raw, "tow_bot_2026_03_10_0825.csv", pd.Timestamp("2026-03-10 08:25:00"), [[2.0, 2.0]] * 50)
    later = condense(cfg, raw, "Tower_Bot")
    assert (later.summary["processed"], later.summary["already_done"]) == (1, 1)
    df = read_day(tmp_path, "2026-03-10")
    assert list(df[df.channel == "Bend_00"].sort_values("timestamp_utc")["mean"]) == [1.0, 2.0]


def test_incremental_output_equals_single_full_run(tmp_path):
    raw, cfg = make_config(tmp_path)
    a, b = "tow_bot_2026_03_10_0815.csv", "tow_bot_2026_03_10_0825.csv"
    write_raw(raw, a, pd.Timestamp("2026-03-10 08:15:00"), [[float(i % 5), 1.0] for i in range(100)])
    condense(cfg, raw, "Tower_Bot")
    write_raw(raw, b, pd.Timestamp("2026-03-10 08:25:00"), [[float(i % 3), 2.0] for i in range(100)])
    condense(cfg, raw, "Tower_Bot")
    incremental = read_day(tmp_path, "2026-03-10")

    full_cfg = dict(cfg, output_folder=str(tmp_path / "out_full"))
    condense(full_cfg, raw, "Tower_Bot")
    full = pd.read_parquet(tmp_path / "out_full" / "Tower_Bot" / "Tower_Bot_2026-03-10.parquet")
    pd.testing.assert_frame_equal(incremental, full)


def test_checkpoint_lives_outside_the_raw_folder(tmp_path):
    raw, cfg = make_config(tmp_path)
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"), [[1.0, 1.0]] * 50)
    before = sorted(p.name for p in raw.iterdir())
    condense(cfg, raw, "Tower_Bot")
    assert sorted(p.name for p in raw.iterdir()) == before


def test_start_date_ignores_earlier_files(tmp_path):
    raw, cfg = make_config(tmp_path)
    write_raw(raw, "tow_bot_2026_03_09_0815.csv", pd.Timestamp("2026-03-09 08:15:00"), [[1.0, 1.0]] * 50)
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"), [[1.0, 1.0]] * 50)
    cfg["start_date"] = "2026-03-10"
    result = condense(cfg, raw, "Tower_Bot")
    assert [p.name for p in out_files(tmp_path)] == ["Tower_Bot_2026-03-10.parquet"]
    assert result.summary["processed"] == 1

    # the start date can also be passed per call, and moving it earlier picks up the backlog
    result = condense(cfg, raw, "Tower_Bot", start_date="2026-03-01")
    assert result.summary["processed"] == 1  # only the file cut by the old start date is added
    assert result.summary["already_done"] == 1
    assert [p.name for p in out_files(tmp_path)] == ["Tower_Bot_2026-03-09.parquet",
                                                     "Tower_Bot_2026-03-10.parquet"]


def test_recently_modified_file_is_retried_on_a_later_run(tmp_path):
    raw, cfg = make_config(tmp_path)
    cfg["loggers"]["Tower_Bot"]["settle_seconds"] = 60
    name = "tow_bot_2026_03_10_0815.csv"
    write_raw(raw, name, pd.Timestamp("2026-03-10 08:15:00"), [[1.0, 1.0]] * 50)  # just written
    result = condense(cfg, raw, "Tower_Bot")
    assert [p.name for p in result.pending] == [name]
    assert out_files(tmp_path) == []

    age(raw / name)
    result = condense(cfg, raw, "Tower_Bot")
    assert result.pending == [] and result.summary["processed"] == 1
    assert len(out_files(tmp_path)) == 1


def test_incomplete_last_row_is_retried_until_complete(tmp_path):
    raw, cfg = make_config(tmp_path)
    name = "tow_bot_2026_03_10_0815.csv"
    write_raw(raw, name, pd.Timestamp("2026-03-10 08:15:00"), [[1.0, 1.0]] * 50)
    full = (raw / name).read_text()
    (raw / name).write_text(full[:-6])  # cut mid-row, no trailing newline
    age(raw / name)
    result = condense(cfg, raw, "Tower_Bot")
    assert [p.name for p in result.pending] == [name]
    assert result.skipped == [] and out_files(tmp_path) == []

    (raw / name).write_text(full)
    age(raw / name)
    result = condense(cfg, raw, "Tower_Bot")
    assert result.pending == [] and result.summary["processed"] == 1
    assert read_day(tmp_path, "2026-03-10").n_valid.iloc[0] == 50


def test_failing_file_is_reported_and_run_continues(tmp_path):
    raw, cfg = make_config(tmp_path)
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"), [[1.0, 1.0]] * 50)
    write_raw(raw, "tow_bot_2026_03_10_0825.csv", pd.Timestamp("2026-03-10 08:25:00"), [["abc", 1.0]] * 50)
    write_raw(raw, "tow_bot_2026_03_10_0835.csv", pd.Timestamp("2026-03-10 08:35:00"), [[3.0, 1.0]] * 50)
    result = condense(cfg, raw, "Tower_Bot")
    assert [p.name for p, _ in result.failed] == ["tow_bot_2026_03_10_0825.csv"]
    assert result.summary == {"processed": 2, "already_done": 0, "skipped": 0, "pending": 0, "failed": 1}
    df = read_day(tmp_path, "2026-03-10")
    assert sorted(df[df.channel == "Bend_00"]["mean"]) == [1.0, 3.0]


def test_stuck_run_spanning_two_incremental_runs_is_flagged(tmp_path):
    raw, cfg = make_config(tmp_path)
    cfg["loggers"]["Tower_Bot"]["stuck_window_seconds"] = 3
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"), [[1.0, 1.0]] * 100)
    condense(cfg, raw, "Tower_Bot")
    assert not read_day(tmp_path, "2026-03-10").stuck.any()  # 2 flat seconds so far
    write_raw(raw, "tow_bot_2026_03_10_0816.csv", pd.Timestamp("2026-03-10 08:15:02"), [[1.0, 1.0]] * 50)
    condense(cfg, raw, "Tower_Bot")
    assert read_day(tmp_path, "2026-03-10").stuck.all()


def test_earlier_start_date_redoes_only_files_the_old_start_date_cut(tmp_path):
    raw, cfg = make_config(tmp_path)
    # logger 19:59:59 on 03-10 is UTC 23:59:59: its first second is on 03-10, its second on 03-11
    write_raw(raw, "tow_bot_2026_03_10_1959.csv", pd.Timestamp("2026-03-10 19:59:59"), [[1.0, 1.0]] * 100)
    write_raw(raw, "tow_bot_2026_03_12_0815.csv", pd.Timestamp("2026-03-12 08:15:00"), [[2.0, 2.0]] * 50)
    write_raw(raw, "tow_bot_2026_03_05_0815.csv", pd.Timestamp("2026-03-05 08:15:00"), [[3.0, 3.0]] * 50)
    condense(cfg, raw, "Tower_Bot", start_date="2026-03-11")
    assert len(read_day(tmp_path, "2026-03-11")) == 2  # one second of the spanning file + 03-12 file

    result = condense(cfg, raw, "Tower_Bot", start_date="2026-03-10")
    # the spanning file lost a second to the old cut and is redone; the 03-12 file is not
    assert [p.name for p in result.processed] == ["tow_bot_2026_03_10_1959.csv"]
    assert sorted(read_day(tmp_path, "2026-03-10").timestamp_utc) == [
        pd.Timestamp("2026-03-10 23:59:59")] * 2

    result = condense(cfg, raw, "Tower_Bot", start_date="2026-03-01")
    assert [p.name for p in result.processed] == ["tow_bot_2026_03_05_0815.csv"]


def test_parallel_processing_gives_the_same_output_as_serial(tmp_path):
    def run(workers, out):
        (tmp_path / out).mkdir()
        raw, cfg = make_config(tmp_path / out)
        cfg["loggers"]["Tower_Bot"]["workers"] = workers
        for d in range(1, 7):
            write_raw(raw, f"tow_bot_2026_03_0{d}_0815.csv", pd.Timestamp(f"2026-03-0{d} 08:15:00"),
                      [[d + i * 0.01, d] for i in range(100)])
        condense(cfg, raw, "Tower_Bot")
        return {p.name: pd.read_parquet(p) for p in (tmp_path / out / "out" / "Tower_Bot").glob("*.parquet")}

    serial, parallel = run(1, "a"), run(4, "b")
    assert sorted(serial) == sorted(parallel) and len(serial) == 6
    for name in serial:
        pd.testing.assert_frame_equal(serial[name], parallel[name])


def test_cached_listing_is_reused_until_refreshed(tmp_path):
    raw, cfg = make_config(tmp_path)
    cfg["listing_cache_hours"] = 24
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", pd.Timestamp("2026-03-10 08:15:00"), [[1.0, 1.0]] * 50)
    assert condense(cfg, raw, "Tower_Bot").summary["processed"] == 1
    assert (tmp_path / "out" / "Tower_Bot" / "listing.json").exists()

    write_raw(raw, "tow_bot_2026_03_11_0815.csv", pd.Timestamp("2026-03-11 08:15:00"), [[1.0, 1.0]] * 50)
    result = condense(cfg, raw, "Tower_Bot")  # the share is not listed again
    assert result.summary["processed"] == 0 and result.summary["already_done"] == 1

    result = condense(cfg, raw, "Tower_Bot", refresh_listing=True)
    assert [p.name for p in result.processed] == ["tow_bot_2026_03_11_0815.csv"]


def test_progress_callback_reports_each_raw_file(tmp_path):
    raw, cfg = make_config(tmp_path)
    for d in (10, 11):
        write_raw(raw, f"tow_bot_2026_03_{d}_0815.csv", pd.Timestamp(f"2026-03-{d} 08:15:00"), [[1.0, 1.0]] * 50)
    seen = []
    condense(cfg, raw, "Tower_Bot", progress=seen.append)
    assert len(seen) == 2 and "2/2" in seen[-1]
