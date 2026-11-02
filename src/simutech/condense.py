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

Bands: a Channel with a `bands` list in the config adds `band_<name>_rms` float64 columns
(NaN where a Channel has no such band) and a `dominant_<Logger>.parquet` file; see
`simutech.bands`.

TOA5 Raw files (2024-09 onward) are read next to headerless ones, see `_read_raw`;
`clock_offset_hours` (default -4) is for headerless files, `toa5_clock_offset_hours` (default 0,
UTC) for TOA5 files.

Config settings (per Logger, else top level, else default):
    stuck_window_seconds  default 5     consecutive zero-std seconds to flag stuck
    clipping_threshold    default 7900  absolute value counted as clipped
                                        (just inside the -7999 missing marker)
"""
from __future__ import annotations

import csv
import datetime
import fnmatch
import io
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from . import bands as _bands
from .report import build_report_data, record_samples, write_report

MISSING = -7999
CARD_FOLDER = "card"  # copy of the Logger's memory card, never read
DATE_IN_NAME = re.compile(r"_(\d{4})_(\d{2})_(\d{2})(?:_|\.)")
DEFAULT_STUCK_WINDOW_SECONDS = 5
DEFAULT_CLIPPING_THRESHOLD = 7900.0
TOA5_MARK = b'"TOA5"'
TOA5_HEADER_ROWS = 4  # metadata, field names, units, processing
DEFAULT_CLOCK_OFFSET_HOURS = -4  # headerless Raw files, ADR 0002
DEFAULT_TOA5_OFFSET_HOURS = 0  # TOA5 files are UTC, ADR 0003
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


def window_start(config: dict, start_date=None, today: datetime.date | None = None) -> str:
    """First UTC day of a run window: `start_date`, else the config's `start_date`, else
    `DEFAULT_WINDOW_DAYS` days before `today`. Shared by the command line and `compare`."""
    return str(start_date or config.get("start_date")
               or (today or datetime.date.today()) - datetime.timedelta(days=DEFAULT_WINDOW_DAYS))


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

    Raw files are condensed in batches of `batch_files` (per Logger, else top level, default
    144) so a long backlog never sits in memory at once; band state is carried from batch to
    batch and the result equals an unbatched run for files that arrive in time order. Each
    batch ends with a commit whose last step is the checkpoint write (it also names the band
    state file `bandstate-<id>.npz` that goes with it): a crash earlier in a batch leaves the
    previous checkpoint and band state in place and a rerun gives the same output as a clean run.
    """
    if not isinstance(config, dict):
        config = json.loads(Path(config).read_text(encoding="utf-8"))
    cfg = config["loggers"][logger]
    mappings = _mappings(cfg)
    offset = pd.Timedelta(hours=cfg.get("clock_offset_hours", DEFAULT_CLOCK_OFFSET_HOURS))
    toa5_offset = pd.Timedelta(hours=cfg.get("toa5_clock_offset_hours", DEFAULT_TOA5_OFFSET_HOURS))
    patterns = cfg["file_pattern"]
    pattern = [p.lower() for p in ([patterns] if isinstance(patterns, str) else patterns)]
    settle = float(cfg.get("settle_seconds", 0))
    start = start_date if start_date is not None else config.get("start_date")
    start = pd.Timestamp(start) if start is not None else None

    out_dir = Path(config["output_folder"]) / logger
    checkpoint_path = out_dir / CHECKPOINT
    checkpoint, saved_start, cut, band_file = _load_checkpoint(checkpoint_path)
    if saved_start is not None and (start is None or start < saved_start):
        for key in cut:  # start date moved earlier: redo only the files the old one cut into
            checkpoint.pop(key, None)
        cut = set()

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
    batch_size = max(1, int(cfg.get("batch_files", config.get("batch_files", DEFAULT_BATCH_FILES))))
    now = time.time()

    def load(item):  # runs in a worker thread: the slow part is reading the share
        _, path, entry = item
        try:
            if now - entry[1] / 1e9 < settle:
                return "pending", None
            data = path.read_bytes()  # the only read of the file
            if _incomplete(data):
                return "pending", None
            return "ok", _read_raw(data, offset - toa5_offset)
        except Exception as exc:  # one bad file must not stop the run
            return "failed", f"{type(exc).__name__}: {exc}"

    stuck_window = cfg.get("stuck_window_seconds",
                           config.get("stuck_window_seconds", DEFAULT_STUCK_WINDOW_SECONDS))
    clip = cfg.get("clipping_threshold", config.get("clipping_threshold", DEFAULT_CLIPPING_THRESHOLD))
    bands_by_channel = _bands_by_channel(cfg)
    band_settings = _bands.BandSettings(cfg, config)
    band_state = {}
    if bands_by_channel:
        band_state = _bands.load_state(out_dir / (band_file or _bands.STATE_FILE))
    new_cut = set(cut)
    summary_files: set[Path] = set()
    n_read = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for lo in range(0, len(todo), batch_size):  # one batch in memory at a time
            chunk = todo[lo:lo + batch_size]
            frames: list[list[pd.DataFrame]] = [[] for _ in mappings]  # per mapping
            keys, new_entries = [], {}  # keys[i]: checkpoint key of file number i of this batch
            for (key, path, entry), (status, df) in zip(chunk, pool.map(load, chunk)):
                n_read += 1
                if progress is not None:
                    progress(f"{logger}: file {n_read}/{len(todo)} {key}")
                if status == "pending":
                    result.pending.append(path)
                    continue
                if status == "failed":
                    result.failed.append((path, df))
                    continue
                df, header = df
                match = None if df is None else _match(mappings, _file_date(path), df.shape[1] - N_META, header)
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
                frames[match].append(df.assign(_file=len(keys)))
                keys.append(key)
                result.processed.append(path)
                new_entries[key] = entry
            if not new_entries:
                continue
            summaries, dominant_rows, kept = _condense_batch(
                frames, keys, mappings, offset, start, stuck_window, clip, bands_by_channel,
                band_settings, band_state)
            # Commit. Day files and the dominant file are replaced by key, so redoing them
            # after a crash gives the same rows; the band state goes to a new file; the
            # checkpoint, which names that state file, is written last and is the commit point.
            if summaries:
                summary_files.update(_merge_into_days(out_dir, logger, pd.concat(summaries, ignore_index=True),
                                                      stuck_window))
            if dominant_rows:
                _merge_dominant(out_dir / f"dominant_{logger}.parquet", dominant_rows)
            new_band_file = None
            if bands_by_channel:
                new_band_file = f"bandstate-{uuid.uuid4().hex[:12]}.npz"
                _bands.save_state(out_dir / new_band_file, band_state)
            checkpoint = {**checkpoint, **new_entries}
            record_samples(out_dir, kept, checkpoint)
            _save_checkpoint(checkpoint_path, checkpoint, start, new_cut, new_band_file)
            if bands_by_channel:
                for old in out_dir.glob("bandstate*.npz"):
                    if old.name != new_band_file:
                        old.unlink(missing_ok=True)
    result.summary_files = sorted(summary_files)
    result.report = build_report_data(logger, out_dir, checkpoint, result.skipped)
    result.report_file = write_report(result.report, out_dir / f"{logger}_report.html")
    return result


def _condense_batch(frames, keys, mappings, offset, start, stuck_window, clip, bands_by_channel,
                    band_settings, band_state):
    """Summary frames, closed dominant rows and kept sample counts of one batch of Raw files.
    Updates `band_state`."""
    summaries, dominant_rows = [], []
    kept: dict[str, dict] = {}  # checkpoint key -> {"rows": kept 50 Hz rows, "valid": {channel: n}}
    for (_, _, names, _), group in zip(mappings, frames):
        if not group:
            continue
        raw = pd.concat(group, ignore_index=True)
        for i in raw["_file"].unique():
            kept[keys[i]] = {"rows": 0, "valid": {n: 0 for n in names}}
        stamps = _second_starts(raw) - offset  # logger clock -> UTC
        # duplicate seconds across files: keep only the later file's rows
        latest = raw["_file"].groupby(stamps).transform("max")
        keep = (raw["_file"] == latest).to_numpy()
        if start is not None:
            keep &= (stamps >= start).to_numpy()
        raw, stamps = raw[keep], stamps[keep]
        if not len(raw):
            continue
        values = raw.iloc[:, N_META:N_META + len(names)].set_axis(names, axis=1)
        usable = values.notna() & (values != MISSING)
        valid = usable.groupby(raw["_file"].to_numpy()).sum()
        for i, n in raw.groupby("_file").size().items():
            kept[keys[i]]["rows"] = int(n)
            kept[keys[i]]["valid"] = {c: int(valid.loc[i, c]) for c in names}
        summary = _summarise(values, stamps, stuck_window, clip)
        if bands_by_channel:
            times = _epoch_ms(_sample_times(raw) - offset)
            summary = _add_bands(summary, values, times, bands_by_channel, band_settings,
                                 band_state, dominant_rows)
        summaries.append(summary)
    return summaries, dominant_rows, kept


def _mappings(cfg) -> list[tuple]:
    """(first date, last date, channel names, units) per Channel mapping; dates are inclusive, None = open."""
    if "channel_mappings" not in cfg:
        return [(None, None, [c["name"] for c in cfg["channels"]], [c.get("unit") for c in cfg["channels"]])]
    return [(pd.Timestamp(m["from"]) if m.get("from") else None,
             pd.Timestamp(m["to"]) if m.get("to") else None,
             [c["name"] for c in m["channels"]], [c.get("unit") for c in m["channels"]])
            for m in cfg["channel_mappings"]]


def _bands_by_channel(cfg) -> dict[str, list[dict]]:
    """Configured bands per Channel name (Channels without bands are left out)."""
    entries = list(cfg.get("channels", []))
    for m in cfg.get("channel_mappings", []):
        entries += m["channels"]
    return {c["name"]: c["bands"] for c in entries if c.get("bands")}


def _epoch_ms(times: pd.Series) -> np.ndarray:
    return times.to_numpy().astype("datetime64[ms]").astype(np.int64)


def _add_bands(summary, values, times, bands_by_channel, settings, state, dominant_rows):
    """Add `band_<name>_rms` columns (NaN for Channels without that band) to the Summary rows."""
    data_end = int(times.max())
    for name in _bands.band_names(bands_by_channel):
        summary[f"band_{name}_rms"] = np.nan
    summary = summary.set_index(["timestamp_utc", "channel"])
    for name, bands in bands_by_channel.items():
        if name not in values.columns:
            continue
        x = values[name].to_numpy(dtype=float)
        ok = x != MISSING
        order = np.argsort(times[ok], kind="stable")
        out, rows = _bands.process_channel(name, bands, times[ok][order], x[ok][order], data_end,
                                           settings, state)
        dominant_rows += rows
        if len(out):
            stamps = out["timestamp_utc"].astype(summary.index.levels[0].dtype)
            out.index = pd.MultiIndex.from_arrays([stamps, [name] * len(out)],
                                                  names=["timestamp_utc", "channel"])
            summary.update(out.drop(columns="timestamp_utc"))  # aligned on (second, Channel)
    return summary.reset_index()


def _merge_dominant(path: Path, rows: list[dict]) -> None:
    """Add closed windows to the dominant file. A window already written is replaced only by
    one with at least as many valid samples, so a late partial file never degrades it."""
    new = pd.DataFrame(rows)
    new["n_valid"] = new["n_valid"].astype("int64")
    if path.exists():
        old = pd.read_parquet(path)
        cols = ["window_start_utc", "channel"]
        have = old.set_index(cols)["n_valid"]
        prior = have.reindex(pd.MultiIndex.from_frame(new[cols])).to_numpy()
        new = new[~(new["n_valid"].to_numpy() < prior)]  # NaN prior (no old window): kept
        new_keys = pd.MultiIndex.from_frame(new[cols])
        old = old[~pd.MultiIndex.from_frame(old[cols]).isin(new_keys)]
        new = pd.concat([old, new], ignore_index=True)
    new = new.sort_values(["window_start_utc", "channel"], kind="stable").reset_index(drop=True)
    _write_parquet(new, path)


def _write_parquet(df: pd.DataFrame, path: Path) -> None:
    """Write via a temporary file so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def _file_date(path: Path) -> pd.Timestamp | None:
    """Logger-clock date in the file name (`..._YYYY_MM_DD_...`), if it has one."""
    m = DATE_IN_NAME.search(path.name)
    try:
        return pd.Timestamp(int(m[1]), int(m[2]), int(m[3])) if m else None
    except ValueError:
        return None


def _match(mappings, date, n_channels, header=None) -> int | None:
    """Index of the first mapping whose date range holds `date` and whose Channel count fits.
    A TOA5 file (`header` = (names, units) read from it) must also match the mapping's names
    and units exactly."""
    for i, (first, last, names, units) in enumerate(mappings):
        if date is None:
            in_range = first is None and last is None
        else:
            in_range = (first is None or date >= first) and (last is None or date <= last)
        if in_range and len(names) == n_channels and (
                header is None or (list(header[0]) == names and list(header[1]) == units)):
            return i
    return None


def _discover(root: Path, patterns: list[str]) -> list[tuple[str, int, int]]:
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
            if e.is_file() and any(fnmatch.fnmatch(e.name.lower(), p) for p in patterns):
                st = e.stat()
                found.append((Path(e.path).relative_to(root).as_posix(), st.st_size, st.st_mtime_ns))
        stack.extend(Path(p) for p in reversed(subs))
    return found


def _listing(root: Path, pattern: list[str], cache_path: Path, max_age_hours: float,
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
        replaced = pd.MultiIndex.from_frame(summary[["timestamp_utc", "channel"]])
        old = old[~pd.MultiIndex.from_frame(old[["timestamp_utc", "channel"]]).isin(replaced)]
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
            _write_parquet(part.reset_index(drop=True), path)
            written.append(path)
    return written


CHECKPOINT = "checkpoint.json"
LISTING = "listing.json"
DEFAULT_WORKERS = 8
DEFAULT_BATCH_FILES = 144  # Raw files held in memory at once (one day of 10-minute files)
DEFAULT_WINDOW_DAYS = 90
_NAME_DATE = re.compile(r"(\d{4})_(\d{2})_(\d{2})")


def _load_checkpoint(path: Path) -> tuple[dict, pd.Timestamp | None, set, str | None]:
    """(files, start date of the last run, files that run cut into at the start date, name of
    the band state file that belongs to this checkpoint)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        saved = data.get("start_date")
        return (data["files"], pd.Timestamp(saved) if saved else None, set(data.get("cut", [])),
                data.get("band_state"))
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return {}, None, set(), None


def _save_checkpoint(path: Path, files: dict, start, cut: set, band_state: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(
        {"start_date": str(start.date()) if start is not None else None, "files": files,
         "cut": sorted(cut & files.keys()), "band_state": band_state},
        indent=0, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _name_date_before(name: str, day: pd.Timestamp) -> bool:
    """True if the file name carries a YYYY_MM_DD date earlier than `day`."""
    m = _NAME_DATE.search(name)
    return bool(m) and pd.Timestamp("-".join(m.groups())) < day


def _incomplete(data: bytes) -> bool:
    """True if the last row looks cut off: no final newline, or a different field
    count from the first row."""
    if not data or data.isspace():
        return False
    if not data.endswith(b"\n"):
        return True
    head = data.split(b"\n", 2)
    first = head[1] if data.startswith(TOA5_MARK) and len(head) > 2 else head[0]  # TOA5: field names
    end = len(data) - 1
    last = data[data.rfind(b"\n", 0, end) + 1:end]
    return last.rstrip(b"\r").count(b",") != first.rstrip(b"\r").count(b",")


def _read_raw(source: Path | bytes, shift: pd.Timedelta = pd.Timedelta(0)):
    """Parse a Raw file (its path or its bytes) into (frame, header); (None, None) if it
    cannot be read.

    Headerless file: frame is the file as is, header None. TOA5 file (first line starts with
    "TOA5"): the TIMESTAMP column is split into the headerless layout (table id 0, year,
    day-of-year, HHMM, seconds) and moved by `shift` (headerless offset minus TOA5 offset) so
    that, once the headerless offset is subtracted, it is UTC; `NAN` becomes -7999; header is
    (Channel names, units) from the file. Raises ValueError on non-numeric content."""
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    try:
        if not data.startswith(TOA5_MARK):
            df = pd.read_csv(io.BytesIO(data), header=None)
            return (None, None) if df.shape[1] <= N_META else (df.apply(pd.to_numeric), None)
        lines = data.split(b"\n", TOA5_HEADER_ROWS)[:TOA5_HEADER_ROWS]
        if len(lines) < 3:
            return None, None
        names, units = (next(csv.reader([ln.decode("utf-8").rstrip("\r")]))[2:] for ln in lines[1:3])
        df = pd.read_csv(io.BytesIO(data), header=None, skiprows=TOA5_HEADER_ROWS, dtype=str,
                         na_values=[], keep_default_na=False)
    except (pd.errors.EmptyDataError, pd.errors.ParserError, UnicodeDecodeError):
        return None, None
    if df.shape[1] <= 2:
        return None, None
    stamps = pd.to_datetime(df.iloc[:, 0], format="ISO8601") + shift  # ValueError on bad text
    values = df.iloc[:, 2:].replace("NAN", str(MISSING)).apply(pd.to_numeric)
    ms = (stamps.dt.second * 1000 + stamps.dt.microsecond // 1000).to_numpy()
    out = pd.DataFrame({"t": 0, "year": stamps.dt.year, "doy": stamps.dt.dayofyear,
                        "hhmm": stamps.dt.hour * 100 + stamps.dt.minute, "sec": ms / 1000})
    out = pd.concat([out, values.set_axis(range(5, 5 + values.shape[1]), axis=1)], axis=1)
    out.columns = range(out.shape[1])
    return out, (names, units)


def _sample_times(raw: pd.DataFrame) -> pd.Series:
    """Time of each row (20 ms steps) on the Logger's own clock."""
    year, doy, hhmm, sec = (raw.iloc[:, i] for i in (1, 2, 3, 4))
    ms = np.round(sec.to_numpy() * 1000).astype(np.int64)
    days = pd.to_timedelta(doy.to_numpy() - 1, unit="D")
    clock = pd.to_timedelta((hhmm // 100).to_numpy(), unit="h") + pd.to_timedelta(
        (hhmm % 100).to_numpy(), unit="min") + pd.to_timedelta(ms, unit="ms")
    jan1 = pd.to_datetime(year.astype(str) + "-01-01")
    return pd.Series(jan1 + days + clock, index=raw.index)


def _second_starts(raw: pd.DataFrame) -> pd.Series:
    """Start of each row's whole second, on the Logger's own clock."""
    return _sample_times(raw).dt.floor("s")


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
