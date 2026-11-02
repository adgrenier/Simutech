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
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .report import build_report_data, write_report

MISSING = -7999
CARD_FOLDER = "card"  # copy of the Logger's memory card, never read
DATE_IN_NAME = re.compile(r"_(\d{4})_(\d{2})_(\d{2})(?:_|\.)")
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


def condense(config, raw_folder, logger: str, start_date=None, refresh_listing: bool = False,
             progress=None) -> CondenseResult:
    """Condense one Logger's Raw files into Summary files.

    Raw files are found in `raw_folder` (default: the Logger's `folder` setting) and
    all its subfolders (year and weekly folders), except folders named `Card`. Each is
    mapped to Channel names by the date in its name and its column count; a file that
    fits no mapping is skipped and listed in `result.skipped`.

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

    Backlog-scale settings: `workers` (per Logger, else top level, default 8) Raw files
    are read in parallel. `listing_cache_hours` (default 0 = list the folder every run)
    keeps the Raw file listing in `<output_folder>/<Logger>/listing.json` and reuses it
    while younger than that, because listing the storage server takes minutes; files
    added since are only seen after it expires or with `refresh_listing=True`. Moving
    `start_date` earlier only adds the files (and seconds) the old start date cut.
    `progress`, if given, is called with one line of text per Raw file read.
    """
    if not isinstance(config, dict):
        config = json.loads(Path(config).read_text(encoding="utf-8"))
    cfg = config["loggers"][logger]
    mappings = _mappings(cfg)
    offset = pd.Timedelta(hours=cfg["clock_offset_hours"])
    pattern = cfg["file_pattern"].lower()
    settle = float(cfg.get("settle_seconds", 0))
    start = start_date if start_date is not None else config.get("start_date")
    start = pd.Timestamp(start) if start is not None else None

    out_dir = Path(config["output_folder"]) / logger
    checkpoint_path = out_dir / CHECKPOINT
    checkpoint, saved_start, cut = _load_checkpoint(checkpoint_path)
    if saved_start is not None and (start is None or start < saved_start):
        for key in cut:  # start date moved earlier: redo only the files the old one cut into
            checkpoint.pop(key, None)
        cut = set()
    now = time.time()

    result = CondenseResult()
    root = Path(raw_folder if raw_folder is not None else cfg["folder"])
    cache_hours = float(cfg.get("listing_cache_hours", config.get("listing_cache_hours", 0)))
    listing = _listing(root, pattern, out_dir / LISTING, cache_hours, refresh_listing)
    todo = []  # (key, path, entry): files to read
    for key, size, mtime_ns in listing:
        name = key.rsplit("/", 1)[-1]
        if start is not None and _name_date_before(name, start - pd.Timedelta(days=1)):
            continue  # cheap pre-filter; the exact cut is on the UTC second below
        path = root / key  # key: path relative to root; weekly folders repeat file names
        entry = [size, mtime_ns]
        if checkpoint.get(key) == entry:
            result.already_done.append(path)
        else:
            todo.append((key, path, entry))

    workers = max(1, int(cfg.get("workers", config.get("workers", DEFAULT_WORKERS))))
    now = time.time()

    def load(item):  # runs in a worker thread: the slow part is reading the share
        _, path, entry = item
        try:
            if now - entry[1] / 1e9 < settle or _incomplete(path):
                return "pending", None
            return "ok", _read_raw(path)
        except Exception as exc:  # one bad file must not stop the run
            return "failed", f"{type(exc).__name__}: {exc}"

    frames: list[list[pd.DataFrame]] = [[] for _ in mappings]  # per mapping
    n_files, new_entries, keys = 0, {}, []  # keys[i]: checkpoint key of file number i
    new_cut = set(cut)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n_read, ((key, path, entry), (status, df)) in enumerate(zip(todo, pool.map(load, todo)), 1):
            if progress is not None:
                progress(f"{logger}: file {n_read}/{len(todo)} {key}")
            if status == "pending":
                result.pending.append(path)
                continue
            if status == "failed":
                result.failed.append((path, df))
                continue
            match = None if df is None else _match(mappings, _file_date(path), df.shape[1] - N_META)
            if match is None:
                result.skipped.append(path)
                continue
            new_cut.discard(key)
            if start is not None:
                utc = _second_starts(df) - offset
                if utc.max() < start:
                    continue  # wholly before the start date: left out, not checkpointed
                if utc.min() < start:
                    new_cut.add(key)  # partly cut: redone if the start date moves earlier
            frames[match].append(df.assign(_file=n_files))
            keys.append(key)
            n_files += 1
            result.processed.append(path)
            new_entries[key] = entry

    stuck_window = cfg.get("stuck_window_seconds",
                           config.get("stuck_window_seconds", DEFAULT_STUCK_WINDOW_SECONDS))
    clip = cfg.get("clipping_threshold", config.get("clipping_threshold", DEFAULT_CLIPPING_THRESHOLD))
    summaries = []
    kept_rows: dict[str, int] = {}  # checkpoint key -> kept 50 Hz rows, this run
    for (_, _, names), group in zip(mappings, frames):
        if not group:
            continue
        raw = pd.concat(group, ignore_index=True)
        stamps = _second_starts(raw) - offset  # logger clock -> UTC
        # duplicate seconds across files: keep only the later file's rows
        latest = raw["_file"].groupby(stamps).transform("max")
        keep = (raw["_file"] == latest).to_numpy()
        if start is not None:
            keep &= (stamps >= start).to_numpy()
        raw, stamps = raw[keep], stamps[keep]
        for i, n in raw.groupby("_file").size().items():
            kept_rows[keys[i]] = int(n)
        if len(raw):
            summaries.append(_summarise(raw.iloc[:, N_META:N_META + len(names)].set_axis(names, axis=1),
                                        stamps, stuck_window, clip))
    if summaries:
        result.summary_files = _merge_into_days(out_dir, logger, pd.concat(summaries, ignore_index=True),
                                                stuck_window)
    if new_entries:
        checkpoint.update(new_entries)
        _save_checkpoint(checkpoint_path, checkpoint, start, new_cut)
    result.report = build_report_data(logger, out_dir, kept_rows, checkpoint, result.skipped)
    result.report_file = write_report(result.report, out_dir / f"{logger}_report.html")
    return result


def _mappings(cfg) -> list[tuple]:
    """(first date, last date, channel names) per Channel mapping; dates are inclusive, None = open."""
    if "channel_mappings" not in cfg:
        return [(None, None, [c["name"] for c in cfg["channels"]])]
    return [(pd.Timestamp(m["from"]) if m.get("from") else None,
             pd.Timestamp(m["to"]) if m.get("to") else None,
             [c["name"] for c in m["channels"]]) for m in cfg["channel_mappings"]]


def _file_date(path: Path) -> pd.Timestamp | None:
    """Logger-clock date in the file name (`..._YYYY_MM_DD_...`), if it has one."""
    m = DATE_IN_NAME.search(path.name)
    try:
        return pd.Timestamp(int(m[1]), int(m[2]), int(m[3])) if m else None
    except ValueError:
        return None


def _match(mappings, date, n_channels) -> int | None:
    """Index of the first mapping whose date range holds `date` and whose Channel count fits."""
    for i, (first, last, names) in enumerate(mappings):
        if date is None:
            in_range = first is None and last is None
        else:
            in_range = (first is None or date >= first) and (last is None or date <= last)
        if in_range and len(names) == n_channels:
            return i
    return None


def _discover(root: Path, pattern: str) -> list[tuple[str, int, int]]:
    """(path relative to `root`, size, mtime_ns) of files under `root` matching the Logger's
    name pattern, in folder then name order; Card copies are skipped. One scandir pass:
    on Windows the size and time come with the listing, with no extra call per file."""
    found = []
    stack = [root]
    while stack:
        folder = stack.pop()
        with os.scandir(folder) as it:
            entries = sorted(it, key=lambda e: e.name)
        subs = [e.path for e in entries if e.is_dir() and e.name.lower() != CARD_FOLDER]
        for e in entries:
            if e.is_file() and fnmatch.fnmatch(e.name.lower(), pattern):
                st = e.stat()
                found.append((Path(e.path).relative_to(root).as_posix(), st.st_size, st.st_mtime_ns))
        stack.extend(Path(p) for p in reversed(subs))
    return found


def _listing(root: Path, pattern: str, cache_path: Path, max_age_hours: float,
             refresh: bool) -> list[tuple[str, int, int]]:
    """`_discover`, or the cached listing of the same folder and pattern if young enough."""
    if max_age_hours > 0 and not refresh:
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if (data["root"] == str(root) and data["pattern"] == pattern
                    and time.time() - data["listed_at"] < max_age_hours * 3600):
                return [tuple(f) for f in data["files"]]
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass
    files = _discover(root, pattern)
    if max_age_hours > 0:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"root": str(root), "pattern": pattern, "listed_at": time.time(),
                                   "files": files}), encoding="utf-8")
        tmp.replace(cache_path)
    return files


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
LISTING = "listing.json"
DEFAULT_WORKERS = 8
_NAME_DATE = re.compile(r"(\d{4})_(\d{2})_(\d{2})")


def _load_checkpoint(path: Path) -> tuple[dict, pd.Timestamp | None, set]:
    """(files, start date of the last run, files that run cut into at the start date)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        saved = data.get("start_date")
        return data["files"], pd.Timestamp(saved) if saved else None, set(data.get("cut", []))
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return {}, None, set()


def _save_checkpoint(path: Path, files: dict, start, cut: set) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(
        {"start_date": str(start.date()) if start is not None else None, "files": files,
         "cut": sorted(cut & files.keys())},
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


def _read_raw(path: Path) -> pd.DataFrame | None:
    """Parse a headerless Raw file; None if it cannot be read as one.
    Raises ValueError on non-numeric content."""
    try:
        df = pd.read_csv(path, header=None)
    except (pd.errors.EmptyDataError, pd.errors.ParserError, UnicodeDecodeError):
        return None
    if df.shape[1] <= N_META:
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
