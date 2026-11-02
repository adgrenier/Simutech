"""The condensing run: config + Raw folder in, Summary files out.

Summary table (one Parquet file per Logger per UTC day, long format, one row
per Channel per UTC second that has at least one valid sample):

    timestamp_utc  datetime64[ns]  start of the UTC second (naive, UTC)
    channel        str             Channel name from the config
    mean, min, max float64         over valid samples, in logged units
    ac_rms         float64         RMS with the mean removed (population)
    std            float64         sample standard deviation (n - 1; NaN if n == 1)
    n_valid        int64           valid samples in the second (-7999 excluded)

Seconds with no valid sample for a Channel have no row.
"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

MISSING = -7999
N_META = 5  # table id, year, day-of-year, HHMM, seconds


@dataclass
class CondenseResult:
    summary_files: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)  # Raw files that matched no mapping


def condense(config, raw_folder, logger: str) -> CondenseResult:
    """Condense one Logger's Raw files in `raw_folder` into Summary files.

    `config` is a dict or the path of a JSON config file (see config.example.json).
    Re-running over the same files rewrites identical output.
    """
    if not isinstance(config, dict):
        config = json.loads(Path(config).read_text(encoding="utf-8"))
    cfg = config["loggers"][logger]
    names = [c["name"] for c in cfg["channels"]]
    offset = pd.Timedelta(hours=cfg["clock_offset_hours"])
    pattern = cfg["file_pattern"].lower()

    result = CondenseResult()
    frames = []
    for path in sorted(Path(raw_folder).iterdir()):
        if not path.is_file() or not fnmatch.fnmatch(path.name.lower(), pattern):
            continue
        df = _read_raw(path, len(names))
        if df is None:
            result.skipped.append(path)
            continue
        frames.append(df)

    out_dir = Path(config["output_folder"]) / logger
    if frames:
        raw = pd.concat(frames, ignore_index=True)
        stamps = _second_starts(raw) - offset  # logger clock -> UTC
        summary = _summarise(raw.iloc[:, N_META:].set_axis(names, axis=1), stamps)
        out_dir.mkdir(parents=True, exist_ok=True)
        for day, part in summary.groupby(summary.timestamp_utc.dt.strftime("%Y-%m-%d")):
            path = out_dir / f"{logger}_{day}.parquet"
            part.reset_index(drop=True).to_parquet(path, index=False)
            result.summary_files.append(path)
    return result


def _read_raw(path: Path, n_channels: int) -> pd.DataFrame | None:
    """Parse a headerless Raw file; None if its column count matches no mapping."""
    try:
        df = pd.read_csv(path, header=None)
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        return None
    if df.shape[1] != N_META + n_channels:
        return None
    return df


def _second_starts(raw: pd.DataFrame) -> pd.Series:
    """Start of each row's whole second, on the Logger's own clock."""
    year, doy, hhmm, sec = (raw.iloc[:, i] for i in (1, 2, 3, 4))
    whole = np.round(sec.to_numpy() * 100).astype(np.int64) // 100  # 20 ms steps, exact
    days = pd.to_timedelta(doy.to_numpy() - 1, unit="D")
    clock = pd.to_timedelta((hhmm // 100).to_numpy(), unit="h") + pd.to_timedelta(
        (hhmm % 100).to_numpy(), unit="min") + pd.to_timedelta(whole, unit="s")
    jan1 = pd.to_datetime(year.astype(str) + "-01-01")
    return pd.Series(jan1 + days + clock, index=raw.index)


def _summarise(values: pd.DataFrame, stamps: pd.Series) -> pd.DataFrame:
    parts = []
    for name in values.columns:
        x = values[name].where(values[name] != MISSING).astype(float)
        d = pd.DataFrame({"timestamp_utc": stamps, "x": x}).dropna(subset=["x"])
        g = d.groupby("timestamp_utc")["x"]
        d["dev2"] = (d["x"] - g.transform("mean")) ** 2
        s = g.agg(mean="mean", min="min", max="max", std="std", n_valid="count")
        s["ac_rms"] = np.sqrt(d.groupby("timestamp_utc")["dev2"].mean())
        s = s.reset_index()
        s.insert(1, "channel", name)
        parts.append(s)
    out = pd.concat(parts, ignore_index=True)
    out["n_valid"] = out["n_valid"].astype("int64")
    out = out[["timestamp_utc", "channel", "mean", "ac_rms", "min", "max", "std", "n_valid"]]
    return out.sort_values(["timestamp_utc", "channel"], kind="stable").reset_index(drop=True)
