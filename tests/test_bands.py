"""Band-limited RMS and 10-minute dominant frequency, through the condensing run.

Expected values come from signals of known amplitude and frequency: a sine of
amplitude A has RMS A / sqrt(2). Frequencies are chosen so the rolling window holds
whole cycles, and values are checked after the filter has settled.
"""
import math

import numpy as np
import pandas as pd

from simutech import condense
from test_condense import MISSING, make_config, write_raw

T0 = pd.Timestamp("2026-03-10 08:10:00")  # logger clock; 12:10:00 UTC
FS = 50
TOL = 0.01  # relative tolerance on band RMS and amplitudes, filter settled


def with_bands(cfg, bands0, **extra):
    """Bands on the first Channel of Tower_Bot; `extra` goes on the Logger."""
    logger = cfg["loggers"]["Tower_Bot"]
    logger["channels"][0]["bands"] = bands0
    logger.update(extra)
    return cfg


def sine(f, amp, n, offset=0.0, start=0):
    k = np.arange(start, start + n)
    return offset + amp * np.sin(2 * np.pi * f * k / FS)


def band(name, low, high):
    return {"name": name, "low_hz": low, "high_hz": high}


def rows(ch0, ch1=1.0):
    return [[float(v), ch1] for v in ch0]


def day(tmp_path, out="out"):
    p = tmp_path / out / "Tower_Bot" / "Tower_Bot_2026-03-10.parquet"
    df = pd.read_parquet(p)
    return df[df.channel == "Bend_00"].sort_values("timestamp_utc").reset_index(drop=True)


def test_band_rms_of_known_sines_in_and_out_of_band(tmp_path):
    raw, cfg = make_config(tmp_path)
    with_bands(cfg, [band("A", 0.15, 0.25), band("B", 4.0, 6.0)])
    n = 150 * FS
    # 0.2 Hz amplitude 2 (inside A), 5 Hz amplitude 3 (inside B), DC offset 3
    x = sine(0.2, 2.0, n, offset=3.0) + sine(5.0, 3.0, n)
    write_raw(raw, "tow_bot_2026_03_10_0810.csv", T0, rows(x))

    condense(cfg, raw, "Tower_Bot")

    df = day(tmp_path)
    settled = df[df.timestamp_utc >= pd.Timestamp("2026-03-10 12:11:00")]
    assert len(settled) == 90
    assert np.allclose(settled.band_A_rms, 2.0 / math.sqrt(2), rtol=TOL)
    assert np.allclose(settled.band_B_rms, 3.0 / math.sqrt(2), rtol=TOL)
    # existing statistics are untouched
    assert (df.n_valid == 50).all()
    assert df.band_A_rms.notna().all()


def test_filter_and_window_reset_after_a_gap(tmp_path):
    raw, cfg = make_config(tmp_path)
    with_bands(cfg, [band("A", 0.15, 0.25)])
    x = np.r_[sine(0.2, 2.0, 20 * FS), np.full(3 * FS, MISSING), np.full(5 * FS, 5.0)]
    write_raw(raw, "tow_bot_2026_03_10_0810.csv", T0, rows(x))

    condense(cfg, raw, "Tower_Bot")

    df = day(tmp_path)
    t = (df.timestamp_utc - pd.Timestamp("2026-03-10 12:10:00")).dt.total_seconds()
    assert list(t) == list(range(20)) + list(range(23, 28))  # no rows in the gap
    assert df.band_A_rms[t < 20].max() > 1.0  # sine seen before the gap
    # after the gap a constant has no band content; nothing carried over from the sine
    assert (df.band_A_rms[t >= 23] < 1e-9).all()


def test_rolling_window_length_is_configurable_and_defaults_to_10_seconds(tmp_path):
    amp, cut = 2.0, 60  # 5 Hz sine for 60 s, then silence (zeros)
    x = np.r_[sine(5.0, amp, cut * FS), np.zeros(15 * FS)]
    for window, extra in [(10, {}), (5, {"band_window_seconds": 5})]:
        (tmp_path / f"w{window}").mkdir()
        raw, cfg = make_config(tmp_path / f"w{window}")
        with_bands(cfg, [band("B", 3.0, 8.0)], **extra)
        write_raw(raw, "tow_bot_2026_03_10_0810.csv", T0, rows(x))
        condense(cfg, raw, "Tower_Bot")
        df = day(tmp_path / f"w{window}")
        k = 2  # seconds after the cut: the window still holds (window - k - 1) seconds of sine
        row = df[df.timestamp_utc == pd.Timestamp("2026-03-10 12:10:00") + pd.Timedelta(seconds=cut + k)]
        expected = amp / math.sqrt(2) * math.sqrt((window - k - 1) / window)
        assert math.isclose(row.band_B_rms.iloc[0], expected, rel_tol=0.05)  # the filter rings briefly at the cut


def test_incremental_run_equals_full_run_across_consecutive_files(tmp_path):
    raw, cfg = make_config(tmp_path)
    with_bands(cfg, [band("A", 0.15, 0.25), band("B", 4.0, 6.0)])
    n = 60 * FS
    x = sine(0.2, 2.0, 2 * n, offset=1.0) + sine(5.0, 1.0, 2 * n)
    write_raw(raw, "tow_bot_2026_03_10_0810.csv", T0, rows(x[:n]))
    condense(cfg, raw, "Tower_Bot")
    write_raw(raw, "tow_bot_2026_03_10_0811.csv", T0 + pd.Timedelta(seconds=60), rows(x[n:]))
    condense(cfg, raw, "Tower_Bot")
    incremental = day(tmp_path)

    condense(dict(cfg, output_folder=str(tmp_path / "out_full")), raw, "Tower_Bot")
    full = day(tmp_path, "out_full")
    assert len(full) == 120 and full.band_A_rms.notna().all()
    pd.testing.assert_frame_equal(incremental, full, rtol=1e-9, atol=1e-12)


def dominant(tmp_path, out="out"):
    return pd.read_parquet(tmp_path / out / "Tower_Bot" / "dominant_Tower_Bot.parquet")


def test_dominant_frequency_and_amplitude_per_ten_minute_window(tmp_path):
    raw, cfg = make_config(tmp_path)
    with_bands(cfg, [band("A", 0.10, 0.30), band("B", 0.30, 0.40)])
    n = 600 * FS + FS  # one full UTC window plus one second to close it
    # tower mode at 0.342 Hz (between FFT bins), a stronger 10 Hz line outside the bands
    x = sine(0.342, 0.8, n, offset=2.0) + sine(10.0, 5.0, n)
    write_raw(raw, "tow_bot_2026_03_10_0810.csv", T0, rows(x))

    condense(cfg, raw, "Tower_Bot")

    d = dominant(tmp_path)
    assert len(d) == 1  # one closed window, only the Channel that has bands
    row = d.iloc[0]
    assert row.window_start_utc == pd.Timestamp("2026-03-10 12:10:00")
    assert row.channel == "Bend_00"
    assert abs(row.dominant_hz - 0.342) < 0.002
    assert math.isclose(row.amplitude, 0.8, rel_tol=TOL)
    assert row.n_valid == 600 * FS


def test_dominant_window_spanning_two_incremental_runs_equals_full_run(tmp_path):
    raw, cfg = make_config(tmp_path)
    with_bands(cfg, [band("A", 0.10, 0.30), band("B", 0.30, 0.40)])
    n = 600 * FS + FS
    x = sine(0.342, 0.8, n, offset=2.0)
    half = 300 * FS
    write_raw(raw, "tow_bot_2026_03_10_0810.csv", T0, rows(x[:half]))
    condense(cfg, raw, "Tower_Bot")
    assert not (tmp_path / "out" / "Tower_Bot" / "dominant_Tower_Bot.parquet").exists()
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", T0 + pd.Timedelta(minutes=5), rows(x[half:]))
    condense(cfg, raw, "Tower_Bot")
    incremental = dominant(tmp_path)

    condense(dict(cfg, output_folder=str(tmp_path / "out_full")), raw, "Tower_Bot")
    pd.testing.assert_frame_equal(incremental, dominant(tmp_path, "out_full"))
    assert len(incremental) == 1
