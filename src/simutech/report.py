"""Per-Logger quality report: report data from Summary rows, rendered as one
self-contained HTML file (inline SVG charts, no external fetches).

Report data (plain dict, the thing tests assert on):

    logger          Logger name
    n_samples       50 Hz samples per Channel kept after duplicate resolution
    skipped_files   names of Raw files that matched no mapping
    channels        {channel: {
        missing_share      share of the Channel's samples that were -7999
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


def build_report_data(logger: str, out_dir: Path, kept_rows: dict[str, int],
                      checkpoint: dict, skipped: list[Path]) -> dict:
    """Report data over every Summary file of the Logger in `out_dir`.

    `kept_rows` maps the Raw files condensed this run to their kept 50 Hz rows. Row
    counts are persisted in a sidecar so the -7999 share stays cumulative across
    incremental runs (entries for files no longer in `checkpoint` are dropped).
    Duplicate seconds spanning two separate runs make that share slightly pessimistic.
    """
    files = sorted(Path(out_dir).glob(f"{logger}_*.parquet"))
    summary = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True) if files else None
    return _report_data(logger, summary, _sample_total(Path(out_dir), kept_rows, checkpoint),
                        skipped)


def _sample_total(out_dir: Path, kept_rows: dict[str, int], checkpoint: dict) -> int:
    path = out_dir / SAMPLES_FILE
    try:
        counts = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        counts = {}
    counts.update(kept_rows)
    counts = {k: v for k, v in counts.items() if k in checkpoint}
    if kept_rows:
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(counts, sort_keys=True), encoding="utf-8")
    return sum(counts.values())


def _report_data(logger: str, summary: pd.DataFrame | None, n_samples: int,
                 skipped: list[Path]) -> dict:
    data = {"logger": logger, "n_samples": int(n_samples),
            "skipped_files": [p.name for p in skipped], "channels": {}}
    if summary is None or summary.empty:
        return data
    day = summary["timestamp_utc"].dt.strftime("%Y-%m-%d")
    for name, rows in summary.groupby("channel", sort=False):
        daily = rows.groupby(day[rows.index]).agg(_DAILY_AGG)
        data["channels"][name] = {
            "missing_share": 1 - float(rows["n_valid"].sum()) / n_samples if n_samples else 0.0,
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
        f"<td>{c['missing_share']:.2%}</td><td>{c['stuck_seconds']}</td>"
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
