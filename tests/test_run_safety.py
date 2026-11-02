"""Late files, batching, crash safety, channel keys, report counts and the compare window.

Everything goes through `condense` / `compare` on synthetic Raw files; a crash is simulated
by making the checkpoint write fail (the last step of a batch).
"""
import datetime
import json
import sys

import numpy as np
import pandas as pd
import pytest

from simutech import condense
from simutech.compare import compare
from test_bands import FS, T0, band, rows, sine, with_bands
from test_compare import make as make_compare_config
from test_condense import MISSING, make_config, write_raw
from test_loggers import write_raw as write_logger_raw

condense_module = sys.modules["simutech.condense"]


def out_dir(tmp):
    return tmp / "out" / "Tower_Bot"


def all_output(folder):
    return {p.name: pd.read_parquet(p) for p in sorted(folder.glob("*.parquet"))}


def assert_same_output(a, b):
    assert sorted(a) == sorted(b) and a
    for name in a:
        pd.testing.assert_frame_equal(a[name], b[name], rtol=1e-9, atol=1e-12)


def banded_files(raw, n_files=5, seconds=60):
    """Consecutive one-minute files of one continuous signal."""
    n = seconds * FS
    x = sine(0.2, 2.0, n * n_files, offset=1.0) + sine(5.0, 1.0, n * n_files)
    for i in range(n_files):
        write_raw(raw, f"tow_bot_2026_03_10_08{10 + i}.csv", T0 + pd.Timedelta(seconds=seconds * i),
                  rows(x[i * n:(i + 1) * n]))


def banded_config(tmp_path, **extra):
    raw, cfg = make_config(tmp_path)
    with_bands(cfg, [band("A", 0.15, 0.25), band("B", 4.0, 6.0)], **extra)
    return raw, cfg


def test_late_partial_file_does_not_overwrite_a_fuller_dominant_window(tmp_path):
    for label, late_seconds in (("above", 330), ("below", 100)):  # above / below 50% valid
        sub = tmp_path / label
        sub.mkdir()
        raw, cfg = make_config(sub)
        with_bands(cfg, [band("A", 0.10, 0.30), band("B", 0.30, 0.40)])
        n = 600 * FS + FS
        write_raw(raw, "tow_bot_2026_03_10_0810.csv", T0, rows(sine(0.342, 0.8, n, offset=2.0)))
        condense(cfg, raw, "Tower_Bot")
        # a late (backfilled) file covering part of the same window, then a newer file
        write_raw(raw, "tow_bot_2026_03_10_0811.csv", T0 + pd.Timedelta(seconds=60),
                  rows(sine(0.342, 0.8, late_seconds * FS, offset=2.0, start=60 * FS)))
        condense(cfg, raw, "Tower_Bot")
        write_raw(raw, "tow_bot_2026_03_10_0825.csv", T0 + pd.Timedelta(minutes=15),
                  rows(np.ones(10 * FS)))
        condense(cfg, raw, "Tower_Bot")

        d = pd.read_parquet(out_dir(sub) / "dominant_Tower_Bot.parquet")
        first = d[d.window_start_utc == pd.Timestamp("2026-03-10 12:10:00")]
        assert len(first) == 1 and first.n_valid.iloc[0] == 600 * FS, label


def test_batched_run_equals_one_shot_and_survives_an_interrupted_run(tmp_path, monkeypatch):
    one = tmp_path / "one"
    one.mkdir()
    raw, cfg = banded_config(one)
    banded_files(raw)
    condense(cfg, raw, "Tower_Bot")
    expected = all_output(out_dir(one))

    batched = tmp_path / "batched"
    batched.mkdir()
    raw2, cfg2 = banded_config(batched, batch_files=2)
    banded_files(raw2)
    condense(cfg2, raw2, "Tower_Bot")
    assert_same_output(all_output(out_dir(batched)), expected)

    # an interrupted batched run keeps the batches already finished and a rerun completes it
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    raw3, cfg3 = banded_config(crashed, batch_files=2)
    banded_files(raw3)
    real, calls = condense_module._save_checkpoint, []

    def second_save_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk gone")
        return real(*args, **kwargs)

    monkeypatch.setattr(condense_module, "_save_checkpoint", second_save_fails)
    with pytest.raises(OSError):
        condense(cfg3, raw3, "Tower_Bot")
    monkeypatch.setattr(condense_module, "_save_checkpoint", real)
    saved = json.loads((out_dir(crashed) / "checkpoint.json").read_text())
    assert len(saved["files"]) == 2  # first batch committed, second did not
    result = condense(cfg3, raw3, "Tower_Bot")
    assert result.summary["processed"] == 3 and result.summary["already_done"] == 2
    assert_same_output(all_output(out_dir(crashed)), expected)


def test_crash_before_the_checkpoint_write_then_rerun_equals_a_clean_run(tmp_path, monkeypatch):
    clean = tmp_path / "clean"
    clean.mkdir()
    raw, cfg = banded_config(clean)
    banded_files(raw, n_files=2)
    condense(cfg, raw, "Tower_Bot")
    expected = all_output(out_dir(clean))

    work = tmp_path / "work"
    work.mkdir()
    raw2, cfg2 = banded_config(work)
    banded_files(raw2, n_files=2)
    held = work / "held_back.csv"
    (raw2 / "tow_bot_2026_03_10_0811.csv").rename(held)
    condense(cfg2, raw2, "Tower_Bot")  # first file only
    held.rename(raw2 / "tow_bot_2026_03_10_0811.csv")

    real = condense_module._save_checkpoint

    def crash(*args, **kwargs):
        raise OSError("crash")

    monkeypatch.setattr(condense_module, "_save_checkpoint", crash)
    with pytest.raises(OSError):
        condense(cfg2, raw2, "Tower_Bot")  # Summary and band state written, checkpoint not
    monkeypatch.setattr(condense_module, "_save_checkpoint", real)
    condense(cfg2, raw2, "Tower_Bot")

    assert_same_output(all_output(out_dir(work)), expected)


def test_a_redelivered_second_keeps_the_other_channels_rows(tmp_path):
    raw, cfg = make_config(tmp_path)
    t = pd.Timestamp("2026-03-10 08:15:00")
    write_raw(raw, "tow_bot_2026_03_10_0815.csv", t, [[1.0, 2.0]] * 150)  # seconds 0..2
    condense(cfg, raw, "Tower_Bot")
    # backfilled file: second 1 again, with Bend_90 all missing
    write_raw(raw, "tow_bot_2026_03_10_0816.csv", t + pd.Timedelta(seconds=1), [[5.0, MISSING]] * 50)
    condense(cfg, raw, "Tower_Bot")

    df = pd.read_parquet(out_dir(tmp_path) / "Tower_Bot_2026-03-10.parquet")
    sec1 = df[df.timestamp_utc == pd.Timestamp("2026-03-10 12:15:01")].set_index("channel")
    assert sec1.loc["Bend_00", "mean"] == 5.0  # replaced by the later file
    assert sec1.loc["Bend_90", "mean"] == 2.0  # not dropped
    assert len(df) == 6


def test_missing_share_is_per_channel_across_channel_mappings(tmp_path):
    raw, cfg = make_config(tmp_path)
    logger = cfg["loggers"]["Tower_Bot"]
    chans = logger.pop("channels")
    logger["channel_mappings"] = [
        {"to": "2026-03-10", "channels": chans},
        {"from": "2026-03-11", "channels": [{"name": "New_0", "unit": "x"}, {"name": "New_1", "unit": "x"}]},
    ]
    write_raw(raw, "tow_bot_2026_03_10_0800.csv", pd.Timestamp("2026-03-10 08:00:00"), [[1.0, 1.0]] * 100)
    write_raw(raw, "tow_bot_2026_03_11_0800.csv", pd.Timestamp("2026-03-11 08:00:00"), [[1.0, 1.0]] * 100)
    result = condense(cfg, raw, "Tower_Bot")
    assert len(result.report["channels"]) == 4
    for name, c in result.report["channels"].items():
        assert c["missing_share"] == 0.0 and c["counts_consistent"] is True, name


def test_missing_share_stays_in_range_and_is_flagged_when_the_sample_counts_are_lost(tmp_path):
    raw, cfg = make_config(tmp_path)
    write_raw(raw, "tow_bot_a.csv", pd.Timestamp("2026-03-10 08:00:00"), [[1.0, 1.0]] * 100)
    condense(cfg, raw, "Tower_Bot")
    (out_dir(tmp_path) / "report_samples.json").unlink()
    write_raw(raw, "tow_bot_b.csv", pd.Timestamp("2026-03-11 08:00:00"), [[1.0, 1.0]] * 50)
    result = condense(cfg, raw, "Tower_Bot")
    for c in result.report["channels"].values():
        assert 0.0 <= c["missing_share"] <= 1.0
        assert c["counts_consistent"] is False


def test_compare_uses_the_90_day_window_and_reports_progress(tmp_path):
    cfg = make_compare_config(tmp_path)
    write_logger_raw(tmp_path / "Hub" / "hub_2025_12_01_0800.csv", pd.Timestamp("2025-12-01 08:00:00"), 2,
                     value=9.0)
    seen = []
    data = compare(cfg, loggers=["Hub"], today=datetime.date(2026, 4, 1), progress=seen.append).data
    assert data["loggers"]["Hub"]["first_day"] == "2026-03-10"  # December is more than 90 days back
    assert seen
    data = compare(cfg, loggers=["Hub"], start_date="2025-11-01").data
    assert data["loggers"]["Hub"]["first_day"] == "2025-12-01"
