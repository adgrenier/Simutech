"""Band-limited RMS per second and dominant frequency per 10-minute window.

Config (per Channel, in `channels` or in a `channel_mappings` entry):

    "bands": [{"name": "A", "low_hz": 0.10, "high_hz": 0.30}, ...]

Settings (per Logger, else top level, else default):
    band_window_seconds       default 10   rolling RMS window
    band_reset_gap_seconds    default 1.0  a time jump larger than this (missing data,
                                           -7999 stretch, missing file) restarts the
                                           filter and the window; shorter holes are bridged
    band_filter_order         default 2    Butterworth order per band edge (second-order
                                           sections, band-pass)

Band RMS: each band is a continuous Butterworth band-pass filter over the 50 Hz samples.
Its state is carried across seconds, across Raw files and across incremental runs
(`bandstate.npz` next to the Summary files). It starts from the steady state of the first
sample, so a constant offset gives no output. The Summary column `band_<name>_rms` of
second t is the RMS of the filtered samples of seconds t-window+1 .. t of the same
continuous stretch, so a stretch's first seconds use a shorter window and the filter is
still settling; the value is meaningful once the stretch is a few window lengths old.
Re-processing data older than the stored state restarts the filter there.

Dominant frequency: `dominant_<Logger>.parquet`, one row per UTC-aligned 10-minute window
(00, 10, 20 ... minutes) per Channel that has bands, once data reach the end of the window:

    window_start_utc  datetime64[ns]
    channel           str
    dominant_hz       float64  strongest spectral line between the lowest and highest band
                               edge of the Channel (parabolic interpolation of a Hann-
                               windowed FFT of the mean-removed window)
    amplitude         float64  peak amplitude of that line, in logged units
    n_valid           int64    valid samples in the window (of 30000)

Windows with fewer than `dominant_min_valid_fraction` (default 0.5) valid samples are not
written. A window still open at the end of a run is kept in `bandstate.npz`.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy import signal

FS = 50
STEP_MS = 1000 // FS
WINDOW_MS = 600_000
WINDOW_N = WINDOW_MS // STEP_MS
DEFAULT_WINDOW_SECONDS = 10
DEFAULT_RESET_GAP_SECONDS = 1.0
DEFAULT_ORDER = 2
DEFAULT_MIN_VALID_FRACTION = 0.5
STATE_FILE = "bandstate.npz"


def band_names(bands_by_channel: dict) -> list[str]:
    seen = []
    for bands in bands_by_channel.values():
        seen += [b["name"] for b in bands if b["name"] not in seen]
    return seen


class BandSettings:
    def __init__(self, cfg, config):
        get = lambda key, default: cfg.get(key, config.get(key, default))  # noqa: E731
        self.window = int(get("band_window_seconds", DEFAULT_WINDOW_SECONDS))
        self.gap_ms = float(get("band_reset_gap_seconds", DEFAULT_RESET_GAP_SECONDS)) * 1000
        self.order = int(get("band_filter_order", DEFAULT_ORDER))
        self.min_valid = float(get("dominant_min_valid_fraction", DEFAULT_MIN_VALID_FRACTION))


def load_state(path: Path) -> dict:
    """{channel: {"spec", "last_ms", "zi", "roll", "open"}} from the state file."""
    state: dict = {}
    if not path.exists():
        return state
    with np.load(path) as f:
        for key in f.files:
            ch, kind, *rest = key.split("|")
            s = state.setdefault(ch, {"zi": {}, "ssq": {}, "open": {}})
            v = f[key]
            if kind == "spec":
                s["spec"] = str(v)
            elif kind == "last_ms":
                s["last_ms"] = int(v)
            elif kind == "zi":
                s["zi"][rest[0]] = v
            elif kind == "roll_start":
                s["roll_start"] = int(v)
            elif kind == "roll_cnt":
                s["roll_cnt"] = v
            elif kind == "roll_ssq":
                s["ssq"][rest[0]] = v
            elif kind == "open":
                s["open"][int(rest[0])] = v
    return state


def save_state(path: Path, state: dict) -> None:
    flat = {}
    for ch, s in state.items():
        flat[f"{ch}|spec"] = np.array(s["spec"])
        if "last_ms" in s:
            flat[f"{ch}|last_ms"] = np.array(s["last_ms"])
            flat[f"{ch}|roll_start"] = np.array(s["roll_start"])
            flat[f"{ch}|roll_cnt"] = s["roll_cnt"]
            for name, v in s["zi"].items():
                flat[f"{ch}|zi|{name}"] = v
            for name, v in s["ssq"].items():
                flat[f"{ch}|roll_ssq|{name}"] = v
        for wid, grid in s["open"].items():
            flat[f"{ch}|open|{wid}"] = grid
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("bandstate.tmp.npz")
    np.savez(tmp, **flat)
    tmp.replace(path)


def process_channel(name: str, bands: list[dict], t_ms: np.ndarray, x: np.ndarray,
                    data_end_ms: int, st: BandSettings, state: dict):
    """Band RMS rows and closed dominant windows for one Channel; updates `state`.

    `t_ms` (UTC epoch milliseconds, ascending) and `x` are the valid samples of this run;
    `data_end_ms` is the time of the last row of the run, valid or not.
    Returns (DataFrame timestamp_utc + band_<name>_rms columns, list of dominant rows).
    """
    spec = repr((sorted((b["name"], b["low_hz"], b["high_hz"]) for b in bands),
                 st.window, st.order, st.gap_ms))
    s = state.get(name)
    if s is None or s.get("spec") != spec:
        s = state[name] = {"spec": spec, "zi": {}, "ssq": {}, "open": {}}
    sos = {b["name"]: signal.butter(st.order, [b["low_hz"], b["high_hz"]], "bandpass",
                                    fs=FS, output="sos") for b in bands}
    frames = []
    if len(t_ms):
        cut = np.flatnonzero(np.diff(t_ms) > st.gap_ms) + 1
        edges = [0, *cut.tolist(), len(t_ms)]
        for i, (a, b) in enumerate(zip(edges[:-1], edges[1:])):
            cont = (i == 0 and "last_ms" in s and 0 < t_ms[0] - s["last_ms"] <= st.gap_ms)
            frames.append(_segment(s, sos, t_ms[a:b], x[a:b], st, cont))
        s["last_ms"] = int(t_ms[-1])
    rows = _dominant(name, bands, t_ms, x, data_end_ms, st, s)
    if frames:
        out = pd.concat(frames, ignore_index=True)
    else:
        out = pd.DataFrame({"timestamp_utc": pd.Series([], dtype="datetime64[ns]")})
    return out, rows


def _segment(s, sos, t, x, st, cont):
    secs = t // 1000
    s0 = int(secs[0])
    span = int(secs[-1]) - s0 + 1
    cnt_new = np.bincount(secs - s0, minlength=span).astype(float)
    base = int(s["roll_start"]) if cont else s0
    length = int(secs[-1]) - base + 1
    cnt = np.zeros(length)
    if cont:
        cnt[: len(s["roll_cnt"])] += s["roll_cnt"]
    cnt[s0 - base:] += cnt_new
    c = np.r_[0.0, np.cumsum(cnt)]
    lo = np.maximum(np.arange(length) + 1 - st.window, 0)
    win_cnt = c[1:] - c[lo]
    shown = np.flatnonzero(cnt_new > 0) + (s0 - base)
    out = {"timestamp_utc": pd.to_datetime((base + shown) * 1000, unit="ms")}
    zi_new, ssq_keep = {}, {}
    for name, sec in sos.items():
        zi = s["zi"][name] if cont else signal.sosfilt_zi(sec) * x[0]
        y, zf = signal.sosfilt(sec, x, zi=zi)
        zi_new[name] = zf
        ssq = np.zeros(length)
        if cont:
            ssq[: len(s["roll_cnt"])] += s["ssq"][name]
        ssq[s0 - base:] += np.bincount(secs - s0, weights=y * y, minlength=span)
        q = np.r_[0.0, np.cumsum(ssq)]
        win = q[1:] - q[lo]
        out[f"band_{name}_rms"] = np.sqrt(win[shown] / win_cnt[shown])
        ssq_keep[name] = ssq[-st.window:]
    s["zi"], s["ssq"] = zi_new, ssq_keep
    s["roll_cnt"] = cnt[-st.window:]
    s["roll_start"] = base + length - len(s["roll_cnt"])
    return pd.DataFrame(out)


def _dominant(name, bands, t_ms, x, data_end_ms, st, s) -> list[dict]:
    opened = s["open"]
    if len(t_ms):
        wid = t_ms // WINDOW_MS
        for w in np.unique(wid):
            pick = wid == w
            grid = opened.get(int(w))
            if grid is None:
                grid = opened[int(w)] = np.full(WINDOW_N, np.nan)
            grid[(t_ms[pick] - w * WINDOW_MS) // STEP_MS] = x[pick]
    lo = min(b["low_hz"] for b in bands)
    hi = max(b["high_hz"] for b in bands)
    rows = []
    for w in sorted(opened):
        if (w + 1) * WINDOW_MS - STEP_MS > data_end_ms:
            continue  # data have not reached the end of this window yet
        grid = opened.pop(w)
        n_valid = int(np.count_nonzero(~np.isnan(grid)))
        if n_valid < st.min_valid * WINDOW_N:
            continue
        freq, amp = _peak(grid, lo, hi)
        rows.append({"window_start_utc": pd.Timestamp(w * WINDOW_MS, unit="ms"), "channel": name,
                     "dominant_hz": freq, "amplitude": amp, "n_valid": n_valid})
    return rows


def _peak(grid: np.ndarray, lo: float, hi: float) -> tuple[float, float]:
    x = grid - np.nanmean(grid)
    x = np.nan_to_num(x)
    w = signal.windows.hann(WINDOW_N, sym=False)
    mag = np.abs(np.fft.rfft(x * w))
    freqs = np.fft.rfftfreq(WINDOW_N, 1 / FS)
    k = int(np.argmax(np.where((freqs >= lo) & (freqs <= hi), mag, -1.0)))
    delta, peak = 0.0, mag[k]
    if 0 < k < len(mag) - 1 and min(mag[k - 1], mag[k], mag[k + 1]) > 0:
        a, b, c = np.log(mag[k - 1:k + 2])
        denom = a - 2 * b + c
        if denom != 0:
            delta = 0.5 * (a - c) / denom
            peak = np.exp(b - 0.25 * (a - c) * delta)
    return float((k + delta) * FS / WINDOW_N), float(2 * peak / w.sum())
