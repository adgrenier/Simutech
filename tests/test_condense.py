"""End-to-end tests of the condensing run on synthetic Raw files.

Only the public entry point (`condense`) and its output Parquet files are used.
"""
import math

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
