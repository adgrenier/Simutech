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
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

MISSING = -7999
N_META = 5  # table id, year, day-of-year, HHMM, seconds


@dataclass
class CondenseResult:
    summary_files: list[Path] = field(default_factory=list)  # Summary files written this run
    skipped: list[Path] = field(default_factory=list)  # Raw files that matched no mapping
    pending: list[Path] = field(default_factory=list)  # Raw files still being written; retried next run
    failed: list[tuple[Path, str]] = field(default_factory=list)  # (Raw file, error); run went on
    processed: list[Path] = field(default_factory=list)  # Raw files condensed this run
    already_done: list[Path] = field(default_factory=list)  # Raw files in the checkpoint, unchanged

    @property
    def summary(self) -> dict[str, int]:
        """End-of-run counts of Raw files by outcome."""
        return {"processed": len(self.processed), "already_done": len(self.already_done),
                "skipped": len(self.skipped), "pending": len(self.pending),
                "failed": len(self.failed)}


def condense(config, raw_folder, logger: str, start_date=None) -> CondenseResult:
    """Condense one Logger's Raw files in `raw_folder` into Summary files.

    `config` is a dict or the path of a JSON config file (see config.example.json).

    Runs are incremental and idempotent. A checkpoint of processed Raw files (name,
    size, modification time) is kept in `<output_folder>/<Logger>/checkpoint.json`,
    never in the Raw folder; only new or changed files are condensed, and their
    Summaries replace the same seconds in the existing daily Summary files. Rerunning
    over unchanged files changes nothing.

    `start_date` (or `start_date` in the config), e.g. "2026-03-10", ignores Raw files
    and seconds before that UTC day. A Raw file modified less than the Logger's
    `settle_seconds` ago (default 0) or whose last row is incomplete is still being
    written: it is returned in `result.pending` and retried on the next run. A file that
    cannot be parsed goes in `result.failed` and the run continues.
    """
    if not isinstance(config, dict):
        config = json.loads(Path(config).read_text(encoding="utf-8"))
    cfg = config["loggers"][logger]
    names = [c["name"] for c in cfg["channels"]]
    offset = pd.Timedelta(hours=cfg["clock_offset_hours"])
    pattern = cfg["file_pattern"].lower()
    settle = float(cfg.get("settle_seconds", 0))
    start = start_date if start_date is not None else config.get("start_date")
    start = pd.Timestamp(start) if start is not None else None

    out_dir = Path(config["output_folder"]) / logger
    checkpoint_path = out_dir / CHECKPOINT
    checkpoint, saved_start = _load_checkpoint(checkpoint_path)
    if saved_start is not None and (start is None or start < saved_start):
        checkpoint = {}  # start date moved earlier: files cut by the old one are redone
    now = time.time()

    result = CondenseResult()
    frames, new_entries = [], {}
    for path in sorted(Path(raw_folder).iterdir()):
        if not path.is_file() or not fnmatch.fnmatch(path.name.lower(), pattern):
            continue
        if start is not None and _name_date_before(path.name, start - pd.Timedelta(days=1)):
            continue  # cheap pre-filter; the exact cut is on the UTC second below
        st = path.stat()
        entry = [st.st_size, st.st_mtime_ns]
        if checkpoint.get(path.name) == entry:
            result.already_done.append(path)
            continue
        try:
            if now - st.st_mtime < settle or _incomplete(path):
                result.pending.append(path)
                continue
            df = _read_raw(path, len(names))
        except Exception as exc:  # one bad file must not stop the run
            result.failed.append((path, f"{type(exc).__name__}: {exc}"))
            continue
        if df is None:
            result.skipped.append(path)
            continue
        if start is not None and (_second_starts(df) - offset).max() < start:
            continue  # wholly before the start date: left out, not checkpointed
        frames.append(df)
        result.processed.append(path)
        new_entries[path.name] = entry

    if frames:
        raw = pd.concat(frames, ignore_index=True)
        stamps = _second_starts(raw) - offset  # logger clock -> UTC
        values = raw.iloc[:, N_META:].set_axis(names, axis=1)
        if start is not None:
            keep = stamps >= start
            values, stamps = values[keep], stamps[keep]
        if len(values):
            summary = _summarise(values, stamps)
            out_dir.mkdir(parents=True, exist_ok=True)
            for day, part in summary.groupby(summary.timestamp_utc.dt.strftime("%Y-%m-%d")):
                path = out_dir / f"{logger}_{day}.parquet"
                if path.exists():  # replace the same seconds, keep the rest
                    old = pd.read_parquet(path)
                    old = old[~old.timestamp_utc.isin(part.timestamp_utc)]
                    part = pd.concat([old, part], ignore_index=True).sort_values(
                        ["timestamp_utc", "channel"], kind="stable")
                part.reset_index(drop=True).to_parquet(path, index=False)
                result.summary_files.append(path)
        checkpoint.update(new_entries)
        _save_checkpoint(checkpoint_path, checkpoint, start)
    return result


CHECKPOINT = "checkpoint.json"
_NAME_DATE = re.compile(r"(\d{4})_(\d{2})_(\d{2})")


def _load_checkpoint(path: Path) -> tuple[dict, pd.Timestamp | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        saved = data.get("start_date")
        return data["files"], pd.Timestamp(saved) if saved else None
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return {}, None


def _save_checkpoint(path: Path, files: dict, start) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(
        {"start_date": str(start.date()) if start is not None else None, "files": files},
        indent=0, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _name_date_before(name: str, day: pd.Timestamp) -> bool:
    """True if the file name carries a YYYY_MM_DD date earlier than `day`."""
    m = _NAME_DATE.search(name)
    return bool(m) and pd.Timestamp("-".join(m.groups())) < day


def _incomplete(path: Path) -> bool:
    """True if the last row looks cut off: no final newline, or a different field
    count from the first row."""
    data = path.read_bytes()
    if not data.strip():
        return False
    if not data.endswith(b"\n"):
        return True
    lines = data.splitlines()
    return lines[-1].count(b",") != lines[0].count(b",")


def _read_raw(path: Path, n_channels: int) -> pd.DataFrame | None:
    """Parse a headerless Raw file; None if its column count matches no mapping.
    Raises ValueError on non-numeric content."""
    try:
        df = pd.read_csv(path, header=None)
    except (pd.errors.EmptyDataError, pd.errors.ParserError):
        return None
    if df.shape[1] != N_META + n_channels:
        return None
    return df.apply(pd.to_numeric)  # ValueError names the bad value


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
