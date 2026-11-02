"""End-to-end tests of the condensing run on synthetic TOA5 Raw files (2024-09 onward),
alone and mixed with headerless Raw files in one Logger's folders."""
import numpy as np
import pandas as pd

from simutech import condense

from test_condense import make_config, read_day, write_raw


def write_toa5(folder, name, start, values, names=("Bend_00", "Bend_90"), units=None):
    """Write a TOA5 file. `start` is a naive timestamp on the file's clock (UTC by default)."""
    units = units or ["mV/V"] * len(names)
    lines = ['"TOA5","TOW_BOT","CR6","14112","OS","CPU:Tow_Bot_009.CR6","44996","Tow_Bot_Data"',
             ",".join(f'"{c}"' for c in ("TIMESTAMP", "RECORD", *names)),
             ",".join(f'"{c}"' for c in ("TS", "RN", *units)),
             ",".join(f'"{c}"' for c in ("", "", *["Smp"] * len(names)))]
    for i, row in enumerate(values):
        t = start + pd.Timedelta(milliseconds=20 * i)
        text = t.strftime("%Y-%m-%d %H:%M:%S")
        if t.microsecond:  # the logger drops the fraction on whole seconds
            text += f"{t.microsecond / 1e6:.2f}"[1:]
        cells = ['"NAN"' if v is None else str(v) for v in row]
        lines.append(",".join([f'"{text}"', str(1000 + i), *cells]))
    (folder / name).write_text("\n".join(lines) + "\n")


def test_toa5_is_utc_by_default_with_subsecond_rows_and_nan_missing(tmp_path):
    raw, cfg = make_config(tmp_path)
    # starts mid-second (like 18:29:56.12), so the first second is short
    t0 = pd.Timestamp("2026-11-02 18:29:56.12")
    k = np.arange(100)
    sine = 3.0 + 2.0 * np.sin(2 * np.pi * 5 * k / 50)
    rows = [[sine[i], None if i % 2 else 1.5] for i in range(100)]
    write_toa5(raw, "tow_bot_data_2026_11_02_1829.csv", t0, rows)

    result = condense(cfg, raw, "Tower_Bot")

    df = read_day(tmp_path, "2026-11-02")
    ch0 = df[df.channel == "Bend_00"].sort_values("timestamp_utc")
    assert list(ch0.timestamp_utc)[:2] == [pd.Timestamp("2026-11-02 18:29:56"),
                                           pd.Timestamp("2026-11-02 18:29:57")]  # UTC, no shift
    assert list(ch0.n_valid)[:2] == [44, 50]  # 56.12 .. 56.98 = 44 samples
    ch1 = df[df.channel == "Bend_90"].sort_values("timestamp_utc")
    assert list(ch1.n_valid)[:2] == [22, 25]  # NAN excluded like -7999
    assert (ch1["mean"] == 1.5).all()
    assert result.skipped == [] and result.failed == []


def test_toa5_clock_offset_is_a_separate_per_logger_setting(tmp_path):
    raw, cfg = make_config(tmp_path, offset=-4)
    cfg["loggers"]["Tower_Bot"]["toa5_clock_offset_hours"] = -1
    write_toa5(raw, "tow_bot_data_2026_11_02_1829.csv", pd.Timestamp("2026-11-02 18:29:56"), [[1.0, 2.0]] * 50)
    write_raw(raw, "tow_bot_2026_11_02_1000.csv", pd.Timestamp("2026-11-02 10:00:00"), [[1.0, 2.0]] * 50)
    condense(cfg, raw, "Tower_Bot")
    stamps = sorted(read_day(tmp_path, "2026-11-02").timestamp_utc.unique())
    assert stamps == [pd.Timestamp("2026-11-02 14:00:00"), pd.Timestamp("2026-11-02 19:29:56")]


def test_defaults_headerless_minus_4_toa5_utc(tmp_path):
    raw, cfg = make_config(tmp_path)
    del cfg["loggers"]["Tower_Bot"]["clock_offset_hours"]
    write_toa5(raw, "tow_bot_data_2026_11_02_1829.csv", pd.Timestamp("2026-11-02 18:29:56"), [[1.0, 2.0]] * 50)
    write_raw(raw, "tow_bot_2026_11_02_1000.csv", pd.Timestamp("2026-11-02 10:00:00"), [[1.0, 2.0]] * 50)
    condense(cfg, raw, "Tower_Bot")
    stamps = sorted(read_day(tmp_path, "2026-11-02").timestamp_utc.unique())
    assert stamps == [pd.Timestamp("2026-11-02 14:00:00"), pd.Timestamp("2026-11-02 18:29:56")]


def test_toa5_header_mismatch_skipped_and_listed(tmp_path):
    raw, cfg = make_config(tmp_path)
    write_toa5(raw, "tow_bot_data_2026_11_02_1829.csv", pd.Timestamp("2026-11-02 18:29:56"),
               [[1.0, 2.0]] * 50, names=("Bend_00", "Other"))
    write_toa5(raw, "tow_bot_data_2026_11_02_1834.csv", pd.Timestamp("2026-11-02 18:34:56"),
               [[1.0, 2.0]] * 50, units=["mV/V", "mV"])
    write_toa5(raw, "tow_bot_data_2026_11_02_1839.csv", pd.Timestamp("2026-11-02 18:39:56"),
               [[1.0, 2.0]] * 50)
    result = condense(cfg, raw, "Tower_Bot")
    assert sorted(p.name for p in result.skipped) == ["tow_bot_data_2026_11_02_1829.csv",
                                                      "tow_bot_data_2026_11_02_1834.csv"]
    assert list(read_day(tmp_path, "2026-11-02").timestamp_utc.unique()) == [pd.Timestamp("2026-11-02 18:39:56")]


def test_toa5_and_headerless_duplicate_seconds_keep_later_file(tmp_path):
    raw, cfg = make_config(tmp_path)
    # headerless clock 08:15:00-01 (UTC-4) = 12:15:00-01 UTC; the TOA5 file covers 12:15:01-02 UTC
    write_raw(raw, "tow_bot_2026_11_02_0815.csv", pd.Timestamp("2026-11-02 08:15:00"), [[1.0, 1.0]] * 100)
    write_toa5(raw, "tow_bot_data_2026_11_02_1215.csv", pd.Timestamp("2026-11-02 12:15:01"), [[5.0, 5.0]] * 100)
    condense(cfg, raw, "Tower_Bot")
    df = read_day(tmp_path, "2026-11-02")
    ch = df[df.channel == "Bend_00"].sort_values("timestamp_utc")
    assert list(ch["mean"]) == [1.0, 5.0, 5.0]  # second 12:15:01 from the later file
    assert (ch.n_valid == 50).all()
    again = condense(cfg, raw, "Tower_Bot")  # one checkpoint covers both formats
    assert again.processed == [] and len(again.already_done) == 2


def test_partial_toa5_file_is_pending(tmp_path):
    raw, cfg = make_config(tmp_path)
    p = raw / "tow_bot_data_2026_11_02_1829.csv"
    write_toa5(raw, p.name, pd.Timestamp("2026-11-02 18:29:56"), [[1.0, 2.0]] * 50)
    p.write_text(p.read_text().rstrip("\n") + '\n"2026-11-02 18:30:56",5,1.0')  # cut-off last row
    result = condense(cfg, raw, "Tower_Bot")
    assert [q.name for q in result.pending] == [p.name]
    assert result.failed == []


def test_file_pattern_may_be_a_list(tmp_path):
    raw, cfg = make_config(tmp_path)
    cfg["loggers"]["Tower_Bot"]["file_pattern"] = ["tow_bot_2*.csv", "tow_bot_data_*.csv"]
    write_toa5(raw, "tow_bot_data_2026_11_02_1829.csv", pd.Timestamp("2026-11-02 18:29:56"), [[1.0, 2.0]] * 50)
    write_raw(raw, "tow_bot_2026_11_02_1000.csv", pd.Timestamp("2026-11-02 10:00:00"), [[1.0, 2.0]] * 50)
    result = condense(cfg, raw, "Tower_Bot")
    assert len(result.processed) == 2
