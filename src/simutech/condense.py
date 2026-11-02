"""The condensing run: config + Raw folder in, Summary files out.

Summary table (one Parquet file per Logger per UTC day, long format, one row
per Channel per UTC second that has at least one valid sample):

    timestamp_utc  datetime64[ns]  start of the UTC second (naive, UTC)
    channel        str             Channel name from the config
    mean, min, max float64         over valid samples, in logged units
    ac_rms         float64         RMS with the mean removed (population)
    std            float64         sample standard deviation (n - 1; NaN if n == 1)
    n_valid        int64           valid samples in the second (-7999 excluded);
                                   the true count, so a short second shows < 50
    n_clipped      int64           valid samples with |value| >= clipping threshold
    stuck          bool            second is part of a run of at least
                                   `stuck_window_seconds` consecutive seconds
                                   with zero standard deviation

Seconds with no valid sample for a Channel have no row (count 0 is implicit).
Duplicate seconds across Raw files keep the rows of the later file (file name
order); a second is never counted twice.

Incremental runs (see `condense`): the Summary files are updated in place from new or
changed Raw files; settings `start_date` (top level) and `settle_seconds` (per Logger,
default 0) are described there.

Config settings (per Logger, else top level, else default):
    stuck_window_seconds  default 5     consecutive zero-std seconds to flag stuck
    clipping_threshold    default 7900  absolute value counted as clipped
                                        (just inside the -7999 missing marker)
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

from .report import build_report_data, write_report

MISSING = -7999
DEFAULT_STUCK_WINDOW_SECONDS = 5
DEFAULT_CLIPPING_THRESHOLD = 7900.0
N_META = 5  # table id, year, day-of-year, HHMM, seconds


@dataclass
class CondenseResult:
    summary_files: list[Path] = field(default_factory=list)  # Summary files written this run
    skipped: list[Path] = field(default_factory=list)  # Raw files that matched no mapping
    report: dict = field(default_factory=dict)  # quality report data, see simutech.report
    report_file: Path | None = None  # self-contained HTML rendering of `report`
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
        frames.append(df.assign(_file=len(frames)))
        result.processed.append(path)
        new_entries[path.name] = entry

    kept_rows = {}
    if frames:
        raw = pd.concat(frames, ignore_index=True)
        stamps = _second_starts(raw) - offset  # logger clock -> UTC
        # duplicate seconds across files: keep only the later file's rows
        latest = raw["_file"].groupby(stamps).transform("max")
        keep = (raw["_file"] == latest).to_numpy()
        if start is not None:
            keep &= (stamps >= start).to_numpy()
        raw, stamps = raw[keep], stamps[keep]
        kept_rows = {result.processed[i].name: int(n) for i, n in raw.groupby("_file").size().items()}
        stuck_window = cfg.get("stuck_window_seconds",
                               config.get("stuck_window_seconds", DEFAULT_STUCK_WINDOW_SECONDS))
        clip = cfg.get("clipping_threshold",
                       config.get("clipping_threshold", DEFAULT_CLIPPING_THRESHOLD))
        if len(raw):
            summary = _summarise(raw.iloc[:, N_META:N_META + len(names)].set_axis(names, axis=1),
                                 stamps, stuck_window, clip)
            result.summary_files = _merge_into_days(out_dir, logger, summary, stuck_window)
        checkpoint.update(new_entries)
        _save_checkpoint(checkpoint_path, checkpoint, start)
    result.report = build_report_data(logger, out_dir, kept_rows, checkpoint, result.skipped)
    result.report_file = write_report(result.report, out_dir / f"{logger}_report.html")
    return result


def _merge_into_days(out_dir: Path, logger: str, summary: pd.DataFrame,
                     stuck_window: int) -> list[Path]:
    """Replace the new seconds in the existing daily Summary files (and add new days).

    `stuck` depends on neighbouring seconds, so it is recomputed over the merged rows of
    the affected days and the day on either side, which keeps incremental output equal to
    a single full run.
    """
    days = set(summary.timestamp_utc.dt.strftime("%Y-%m-%d"))
    one_day = pd.Timedelta(days=1)
    touched = set(days)
    for d in days:
        touched |= {str((pd.Timestamp(d) + k * one_day).date()) for k in (-1, 1)}
    old = [pd.read_parquet(out_dir / f"{logger}_{d}.parquet") for d in sorted(touched)
           if (out_dir / f"{logger}_{d}.parquet").exists()]
    if old:
        old = pd.concat(old, ignore_index=True)
        old = old[~old.timestamp_utc.isin(summary.timestamp_utc)]
        summary = pd.concat([old, summary], ignore_index=True)
    summary = summary.sort_values(["channel", "timestamp_utc"], kind="stable")
    for _, idx in summary.groupby("channel", sort=False).groups.items():
        g = summary.loc[idx]
        summary.loc[idx, "stuck"] = _stuck(g["timestamp_utc"], g["std"], stuck_window)
    summary["stuck"] = summary["stuck"].astype(bool)
    summary = summary.sort_values(["timestamp_utc", "channel"], kind="stable")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for day, part in summary.groupby(summary.timestamp_utc.dt.strftime("%Y-%m-%d")):
        if day in touched:
            path = out_dir / f"{logger}_{day}.parquet"
            part.reset_index(drop=True).to_parquet(path, index=False)
            written.append(path)
    return written


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


def _summarise(values: pd.DataFrame, stamps: pd.Series, stuck_window: int,
               clip: float) -> pd.DataFrame:
    parts = []
    for name in values.columns:
        x = values[name].where(values[name] != MISSING).astype(float)
        d = pd.DataFrame({"timestamp_utc": stamps, "x": x}).dropna(subset=["x"])
        g = d.groupby("timestamp_utc")["x"]
        d["dev2"] = (d["x"] - g.transform("mean")) ** 2
        s = g.agg(mean="mean", min="min", max="max", std="std", n_valid="count")
        s["ac_rms"] = np.sqrt(d.groupby("timestamp_utc")["dev2"].mean())
        s["n_clipped"] = (d["x"].abs() >= clip).groupby(d["timestamp_utc"]).sum()
        s = s.reset_index().sort_values("timestamp_utc")
        s["stuck"] = _stuck(s["timestamp_utc"], s["std"], stuck_window)
        s.insert(1, "channel", name)
        parts.append(s)
    out = pd.concat(parts, ignore_index=True)
    out["n_valid"] = out["n_valid"].astype("int64")
    out["n_clipped"] = out["n_clipped"].astype("int64")
    out = out[["timestamp_utc", "channel", "mean", "ac_rms", "min", "max", "std", "n_valid",
               "n_clipped", "stuck"]]
    return out.sort_values(["timestamp_utc", "channel"], kind="stable").reset_index(drop=True)


def _stuck(stamps: pd.Series, std: pd.Series, window: int) -> pd.Series:
    """True for seconds inside a run of >= `window` consecutive zero-std seconds."""
    flat = (std == 0).to_numpy()
    new_run = ~flat | (stamps.diff() != pd.Timedelta(seconds=1)).to_numpy() | np.r_[True, ~flat[:-1]]
    run_id = np.cumsum(new_run)
    length = pd.Series(flat, index=stamps.index).groupby(run_id).transform("sum")
    return pd.Series(flat & (length.to_numpy() >= window), index=stamps.index)
