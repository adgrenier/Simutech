"""Per-Logger quality report: report data from Summary rows, rendered as one
self-contained HTML file (inline SVG charts, no external fetches).

Report data (plain dict, the thing tests assert on):

    logger          Logger name
    n_samples       50 Hz samples per Channel kept after duplicate resolution
    skipped_files   names of Raw files that matched no mapping
    channels        {channel: {
        missing_share      share of the Channel's samples that were -7999, over the Raw files
                           that carry the Channel; always within 0..1
        counts_consistent  False if the Summary holds more valid samples than the sample
                           counts cover (counts lost); missing_share is then unreliable
        full_second_share  share of Summary seconds with a full 50 samples
        stuck_seconds      Summary seconds flagged stuck
        clipped_samples    sum of n_clipped
        clipped_seconds    Summary seconds with n_clipped > 0
        daily              {UTC date: {mean, ac_rms, min, max, std}}  daily trend values
    }}

Daily trend values: mean, ac_rms and std are the average of that day's per-second
values; min and max are the day's extremes.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

import pandas as pd

FULL_SECOND = 50
SAMPLES_FILE = "report_samples.json"
STATS = ("mean", "ac_rms", "min", "max", "std")
_DAILY_AGG = {"mean": "mean", "ac_rms": "mean", "std": "mean", "min": "min", "max": "max"}


def build_report_data(logger: str, out_dir: Path, checkpoint: dict, skipped: list[Path]) -> dict:
    """Report data over every Summary file of the Logger in `out_dir`.

    The sample counts come from the sidecar written by `record_samples`, restricted to the
    Raw files in `checkpoint`; they are cumulative across incremental runs. Duplicate seconds
    spanning two separate runs make the -7999 share slightly pessimistic.
    """
    files = sorted(Path(out_dir).glob(f"{logger}_????-??-??.parquet"))
    summary = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True) if files else None
    return _report_data(logger, summary, _load_samples(Path(out_dir), checkpoint), skipped)


def record_samples(out_dir: Path, kept: dict[str, dict], checkpoint: dict) -> None:
    """Persist the kept 50 Hz row counts of Raw files condensed this batch.

    `kept` maps a checkpoint key to {"rows": kept rows, "valid": {channel: valid samples}}.
    Entries of files no longer in `checkpoint` are dropped.
    """
    counts = _read_samples(out_dir)
    counts.update(kept)
    counts = {k: v for k, v in counts.items() if k in checkpoint}
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / SAMPLES_FILE
    tmp = path.with_name(SAMPLES_FILE + ".tmp")
    tmp.write_text(json.dumps(counts, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _read_samples(out_dir: Path) -> dict:
    try:
        counts = json.loads((out_dir / SAMPLES_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in counts.items() if isinstance(v, dict)}  # older int-only format dropped


def _load_samples(out_dir: Path, checkpoint: dict) -> dict:
    return {k: v for k, v in _read_samples(out_dir).items() if k in checkpoint}


def _report_data(logger: str, summary: pd.DataFrame | None, samples: dict,
                 skipped: list[Path]) -> dict:
    data = {"logger": logger, "n_samples": int(sum(v["rows"] for v in samples.values())),
            "skipped_files": [p.name for p in skipped], "channels": {}}
    if summary is None or summary.empty:
        return data
    day = summary["timestamp_utc"].dt.strftime("%Y-%m-%d")
    for name, rows in summary.groupby("channel", sort=False):
        daily = rows.groupby(day[rows.index]).agg(_DAILY_AGG)
        # same data on both sides: the Raw files that carry this Channel
        n_rows = sum(v["rows"] for v in samples.values() if name in v["valid"])
        n_ok = sum(v["valid"][name] for v in samples.values() if name in v["valid"])
        in_summary = int(rows["n_valid"].sum())
        data["channels"][name] = {
            "missing_share": min(1.0, max(0.0, 1 - n_ok / n_rows)) if n_rows else 0.0,
            # False when the Summary holds more valid samples than the counts cover
            "counts_consistent": bool(n_rows) and in_summary <= n_ok,
            "full_second_share": float((rows["n_valid"] == FULL_SECOND).mean()),
            "stuck_seconds": int(rows["stuck"].sum()),
            "clipped_samples": int(rows["n_clipped"].sum()),
            "clipped_seconds": int((rows["n_clipped"] > 0).sum()),
            "daily": {d: {s: float(v[s]) for s in STATS} for d, v in daily.iterrows()},
        }
    return data


def write_report(data: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(data), encoding="utf-8")
    return path


def render_html(data: dict) -> str:
    esc = html.escape
    rows = "".join(
        f"<tr><td>{esc(n)}</td><td>{c['full_second_share']:.1%}</td>"
        f"<td>{c['missing_share']:.2%}{'' if c['counts_consistent'] else ' (counts incomplete)'}</td><td>{c['stuck_seconds']}</td>"
        f"<td>{c['clipped_samples']} ({c['clipped_seconds']} s)</td></tr>"
        for n, c in data["channels"].items())
    charts = "".join(
        f"<h2>{esc(n)}</h2><div class='grid'>"
        + "".join(_chart(s, c["daily"]) for s in STATS) + "</div>"
        for n, c in data["channels"].items())
    skipped = "".join(f"<li>{esc(f)}</li>" for f in data["skipped_files"]) or "<li>none</li>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{esc(data['logger'])} quality report</title>
<style>body{{font-family:sans-serif;margin:1.5rem;max-width:70rem}}
table{{border-collapse:collapse}}td,th{{border:1px solid #bbb;padding:.25rem .6rem;text-align:right}}
td:first-child,th:first-child{{text-align:left}}.grid{{display:flex;flex-wrap:wrap;gap:1rem}}
svg{{background:#fafafa;border:1px solid #ddd}}</style></head><body>
<h1>{esc(data['logger'])} quality report</h1>
<h2>Data health</h2>
<table><tr><th>Channel</th><th>Full 50-sample seconds</th><th>-7999 share</th>
<th>Stuck seconds</th><th>Clipped samples</th></tr>{rows}</table>
<h2>Daily trends</h2>{charts}
<h2>Skipped files</h2><ul>{skipped}</ul>
</body></html>
"""


def _chart(stat: str, daily: dict, w: int = 320, h: int = 140, pad: int = 28) -> str:
    days = sorted(daily)
    ys = [daily[d][stat] for d in days]
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1.0

    def x(i):
        return pad + (w - 2 * pad) * (i / (len(ys) - 1) if len(ys) > 1 else 0.5)

    def y(v):
        return h - pad - (h - 2 * pad) * (v - lo) / span

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(ys))
    dots = "".join(f"<circle cx='{x(i):.1f}' cy='{y(v):.1f}' r='2.5' fill='#1f6feb'/>"
                   for i, v in enumerate(ys))
    return (f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}' "
            f"viewBox='0 0 {w} {h}' role='img' aria-label='{stat} daily trend'>"
            f"<text x='{pad}' y='14' font-size='12'>{stat}</text>"
            f"<polyline points='{pts}' fill='none' stroke='#1f6feb' stroke-width='1.5'/>{dots}"
            f"<text x='2' y='{h - pad + 4}' font-size='9'>{lo:.4g}</text>"
            f"<text x='2' y='{pad + 4}' font-size='9'>{hi:.4g}</text>"
            f"<text x='{pad}' y='{h - 6}' font-size='9'>{days[0]}</text>"
            f"<text x='{w - pad}' y='{h - 6}' font-size='9' text-anchor='end'>{days[-1]}</text>"
            f"</svg>")
