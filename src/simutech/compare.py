"""Cross-Logger comparison page: one self-contained HTML page with every Logger's
data health and trend summary side by side, linking to each Logger's own report.

Comparison data (plain dict, the thing tests assert on):

    loggers   {Logger: {
        n_samples         50 Hz samples kept
        skipped_files     number of Raw files that matched no mapping
        full_second_share, missing_share   means over the Logger's Channels
        stuck_seconds, clipped_samples    sums over the Logger's Channels
        first_day, last_day   first and last UTC date with Summary rows (None if none)
        channels   {Channel: {full_second_share, missing_share, stuck_seconds,
                              clipped_samples, trend: {first, last, change}}}
            trend: daily mean on the first and last day, and last - first
        report_link       the Logger's report path relative to the comparison page
    }}

Run: `python -m simutech.compare config.json [Logger ...]`
"""
from __future__ import annotations

import html
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .condense import condense

PAGE_NAME = "comparison.html"


@dataclass
class ComparisonResult:
    data: dict
    page_file: Path


def compare(config, loggers: list[str] | None = None, raw_folders: dict | None = None) -> ComparisonResult:
    """Run the condense run for each Logger (default: all in the config) and write
    `<output_folder>/comparison.html`. `raw_folders` optionally maps Logger -> raw folder."""
    if not isinstance(config, dict):
        config = json.loads(Path(config).read_text(encoding="utf-8"))
    out = Path(config["output_folder"])
    names = loggers or list(config["loggers"])
    results = {n: condense(config, (raw_folders or {}).get(n), n) for n in names}
    data = {"loggers": {n: _logger(r, out) for n, r in results.items()}}
    out.mkdir(parents=True, exist_ok=True)
    page = out / PAGE_NAME
    page.write_text(render_html(data), encoding="utf-8")
    return ComparisonResult(data, page)


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def _logger(result, out_dir: Path) -> dict:
    rep = result.report
    chans, days = {}, set()
    for name, c in rep["channels"].items():
        d = c["daily"]
        days |= set(d)
        first, last = d[min(d)]["mean"], d[max(d)]["mean"]
        chans[name] = {k: c[k] for k in ("full_second_share", "missing_share", "stuck_seconds",
                                         "clipped_samples")}
        chans[name]["trend"] = {"first": first, "last": last, "change": last - first}
    link = Path(os.path.relpath(result.report_file, out_dir)).as_posix() if result.report_file else None
    return {
        "n_samples": rep["n_samples"], "skipped_files": len(rep["skipped_files"]),
        "full_second_share": _mean(c["full_second_share"] for c in chans.values()),
        "missing_share": _mean(c["missing_share"] for c in chans.values()),
        "stuck_seconds": sum(c["stuck_seconds"] for c in chans.values()),
        "clipped_samples": sum(c["clipped_samples"] for c in chans.values()),
        "first_day": min(days) if days else None, "last_day": max(days) if days else None,
        "channels": chans, "report_link": link,
    }


def _pct(v, nd=1):
    return "n/a" if v is None else f"{v:.{nd}%}"


def render_html(data: dict) -> str:
    esc = html.escape
    rows, trends = "", ""
    for name, d in data["loggers"].items():
        link = f'<a href="{esc(d["report_link"])}">{esc(name)}</a>' if d["report_link"] else esc(name)
        rows += (f"<tr><td>{link}</td><td>{_pct(d['full_second_share'])}</td>"
                 f"<td>{_pct(d['missing_share'], 2)}</td><td>{d['stuck_seconds']}</td>"
                 f"<td>{d['clipped_samples']}</td><td>{d['skipped_files']}</td>"
                 f"<td>{esc(d['first_day'] or 'n/a')} to {esc(d['last_day'] or 'n/a')}</td></tr>")
        for ch, c in d["channels"].items():
            t = c["trend"]
            trends += (f"<tr><td>{esc(name)}</td><td>{esc(ch)}</td><td>{t['first']:.4g}</td>"
                       f"<td>{t['last']:.4g}</td><td>{t['change']:+.4g}</td></tr>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Logger comparison</title>
<style>body{{font-family:sans-serif;margin:1.5rem;max-width:70rem}}
table{{border-collapse:collapse;margin-bottom:1.5rem}}td,th{{border:1px solid #bbb;padding:.25rem .6rem;text-align:right}}
td:first-child,th:first-child{{text-align:left}}</style></head><body>
<h1>Logger comparison</h1>
<h2>Data health</h2>
<table><tr><th>Logger</th><th>Full 50-sample seconds</th><th>-7999 share</th><th>Stuck seconds</th>
<th>Clipped samples</th><th>Skipped files</th><th>Summary days</th></tr>{rows}</table>
<h2>Daily mean trend (first day to last day)</h2>
<table><tr><th>Logger</th><th>Channel</th><th>First</th><th>Last</th><th>Change</th></tr>{trends}</table>
</body></html>
"""


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        raise SystemExit("usage: python -m simutech.compare config.json [Logger ...]")
    print(compare(argv[0], argv[1:] or None).page_file)


if __name__ == "__main__":
    main()
