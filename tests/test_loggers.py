"""Discovery, channel mapping and clock offsets across several Loggers.

Only the public entry point (`condense`) and its output Parquet files are used.
"""
import json
from pathlib import Path

import pandas as pd

from simutech import condense


def write_raw(path, start_local, n_channels, n_rows=50, value=1.0):
    """Write a headerless Raw file of `n_rows` 20 ms rows on the Logger clock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(n_rows):
        t = start_local + pd.Timedelta(milliseconds=20 * i)
        sec = t.second + t.microsecond / 1e6
        cols = [101, t.year, t.dayofyear, t.hour * 100 + t.minute, f"{sec:.2f}", *([value] * n_channels)]
        lines.append(",".join(str(c) for c in cols))
    path.write_text("\n".join(lines) + "\n")


def channels(*names):
    return [{"name": n, "unit": "mV"} for n in names]


def config(tmp_path, loggers):
    return {"output_folder": str(tmp_path / "out"), "loggers": loggers}


def read_day(tmp_path, logger, day):
    return pd.read_parquet(tmp_path / "out" / logger / f"{logger}_{day}.parquet")


def test_discovers_root_and_weekly_subfolders_but_not_card_or_stray_files(tmp_path):
    root = tmp_path / "Hub"
    cfg = config(tmp_path, {"Hub": {
        "folder": str(root), "file_pattern": "hub_data_*.csv", "clock_offset_hours": -4,
        "channels": channels("A", "B")}})
    write_raw(root / "hub_data_2020_09_04_1823.csv", pd.Timestamp("2020-09-04 18:23:00"), 2)
    write_raw(root / "2026" / "Week_33" / "hub_data_2025_08_17_0009.csv", pd.Timestamp("2025-08-17 00:09:00"), 2)
    write_raw(root / "Card" / "hub_data_2020_09_04_1900.csv", pd.Timestamp("2020-09-04 19:00:00"), 2)
    write_raw(root / "2026" / "Week_33" / "Card" / "hub_data_2025_08_17_0019.csv", pd.Timestamp("2025-08-17 00:19:00"), 2)
    (root / "hub_data.csv").write_text('"TOA5","header"\n')  # stray: not matching the naming pattern
    (root / "notes.txt").write_text("stray")

    result = condense(cfg, None, "Hub")  # folder comes from the config

    assert list(read_day(tmp_path, "Hub", "2020-09-04").timestamp_utc.unique()) == [
        pd.Timestamp("2020-09-04 22:23:00")]
    assert list(read_day(tmp_path, "Hub", "2025-08-17").timestamp_utc.unique()) == [
        pd.Timestamp("2025-08-17 04:09:00")]
    assert result.skipped == []


def nacelle_config(tmp_path):
    root = tmp_path / "Nacelle"
    return root, config(tmp_path, {"Nacelle": {
        "folder": str(root), "file_pattern": "nacelle_data_*.csv", "clock_offset_hours": -4,
        "channel_mappings": [
            {"from": "2020-09-04", "to": "2021-08-23", "channels": channels("Old_A", "Old_B", "Old_C")},
            {"from": "2021-08-24", "channels": channels("New_A", "New_B")},
        ]}})


def test_channel_mapping_is_chosen_per_date_range_and_verified_against_column_count(tmp_path):
    root, cfg = nacelle_config(tmp_path)
    write_raw(root / "nacelle_data_2020_09_04_1823.csv", pd.Timestamp("2020-09-04 18:23:00"), 3)
    write_raw(root / "nacelle_data_2021_08_24_2035.csv", pd.Timestamp("2021-08-24 20:35:00"), 2)
    # right date, wrong number of columns for that date range: unmatched
    write_raw(root / "nacelle_data_2021_08_25_0100.csv", pd.Timestamp("2021-08-25 01:00:00"), 3)
    write_raw(root / "nacelle_data_2020_09_05_0100.csv", pd.Timestamp("2020-09-05 01:00:00"), 2)
    # before any date range: unmatched
    write_raw(root / "nacelle_data_2019_01_01_0100.csv", pd.Timestamp("2019-01-01 01:00:00"), 3)

    result = condense(cfg, None, "Nacelle")

    assert set(read_day(tmp_path, "Nacelle", "2020-09-04").channel) == {"Old_A", "Old_B", "Old_C"}
    assert set(read_day(tmp_path, "Nacelle", "2021-08-25").channel) == {"New_A", "New_B"}
    assert sorted(p.name for p in result.skipped) == [
        "nacelle_data_2019_01_01_0100.csv", "nacelle_data_2020_09_05_0100.csv",
        "nacelle_data_2021_08_25_0100.csv"]


def test_clock_offset_is_per_logger(tmp_path):
    cfg = config(tmp_path, {
        name: {"folder": str(tmp_path / name), "file_pattern": f"{name.lower()}_*.csv",
               "clock_offset_hours": off, "channels": channels("A")}
        for name, off in (("Alpha", -4), ("Beta", -5))})
    for name in ("Alpha", "Beta"):
        write_raw(tmp_path / name / f"{name.lower()}_2026_03_10_0800.csv", pd.Timestamp("2026-03-10 08:00:00"), 1)

    condense(cfg, None, "Alpha")
    condense(cfg, None, "Beta")

    assert read_day(tmp_path, "Alpha", "2026-03-10").timestamp_utc.iloc[0] == pd.Timestamp("2026-03-10 12:00:00")
    assert read_day(tmp_path, "Beta", "2026-03-10").timestamp_utc.iloc[0] == pd.Timestamp("2026-03-10 13:00:00")


# (Logger, a Raw file name, its date, its column count) as seen on the real share.
REAL_SHAPES = [
    ("Hub", "Hub_Data_2020_09_04_1823.csv", "2020-09-04 18:23:00", 12),
    ("Hub", "hub_data_2025_08_17_0009.csv", "2025-08-17 00:09:00", 15),
    ("Nacelle", "nacelle_data_2020_09_04_1823.csv", "2020-09-04 18:23:00", 15),
    ("Nacelle", "nacelle_data_2021_08_24_2035.csv", "2021-08-24 20:35:00", 12),
    ("Tower_Top", "tow_top_data_2020_09_04_1823_1.csv", "2020-09-04 18:23:00", 3),
    ("Tower_Bot", "tow_bot_2021_06_28_1809.csv", "2021-06-28 18:09:00", 2),
]


def test_shipped_config_maps_all_four_loggers_with_utc_minus_4(tmp_path):
    cfg = json.loads((Path(__file__).parent.parent / "config.json").read_text(encoding="utf-8"))
    cfg["output_folder"] = str(tmp_path / "out")
    cfg["listing_cache_hours"] = 0  # the test adds files between runs
    assert {n: c["clock_offset_hours"] for n, c in cfg["loggers"].items()} == {
        "Hub": -4, "Nacelle": -4, "Tower_Top": -4, "Tower_Bot": -4}

    for logger, name, start, n_cols in REAL_SHAPES:
        raw = tmp_path / "raw" / logger
        write_raw(raw / "2026" / "Week_33" / name, pd.Timestamp(start), n_cols)
        result = condense(cfg, raw, logger)
        assert result.skipped == [], (logger, name)
        day = pd.Timestamp(start).tz_localize("Etc/GMT+4").tz_convert("UTC").strftime("%Y-%m-%d")
        assert read_day(tmp_path, logger, day).channel.nunique() == n_cols
