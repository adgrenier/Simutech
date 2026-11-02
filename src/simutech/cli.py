"""Command line: condense every Logger for the configured window.

    python -m simutech --config config.json [--start-date YYYY-MM-DD] [--logger NAME ...]
                       [--raw-folder PATH] [--refresh-listing]

The window starts at `start_date` (argument, else config, else 90 days before today) and
runs to the newest Raw file. Progress goes to stdout, one line per Raw file and one
summary line per Logger. `--raw-folder` (one Logger only) condenses a sample folder
instead of the Logger's folder.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

from .condense import condense, window_start


def main(argv=None, today: datetime.date | None = None) -> int:
    ap = argparse.ArgumentParser(prog="simutech", description="Condense every Logger for the configured window.")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--start-date", help="first UTC day (default: config start_date, else 90 days back)")
    ap.add_argument("--logger", action="append", help="Logger to run (repeatable; default: all)")
    ap.add_argument("--raw-folder", help="Raw folder to use instead of the Logger's (one Logger only)")
    ap.add_argument("--refresh-listing", action="store_true", help="re-list the Raw folders now")
    args = ap.parse_args(argv)

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    loggers = args.logger or list(config["loggers"])
    if args.raw_folder and len(loggers) != 1:
        ap.error("--raw-folder needs exactly one --logger")
    start = window_start(config, args.start_date, today)
    print(f"Window starts {start} (UTC); Loggers: {', '.join(loggers)}", flush=True)

    failures = 0
    for i, name in enumerate(loggers, 1):
        print(f"[{i}/{len(loggers)}] {name}: listing and condensing", flush=True)
        result = condense(config, args.raw_folder, name, start_date=start,
                          refresh_listing=args.refresh_listing,
                          progress=lambda m: print("  " + m, flush=True))
        print(f"[{i}/{len(loggers)}] {name} done: "
              + ", ".join(f"{n} {k}" for k, n in result.summary.items()), flush=True)
        failures += len(result.failed)
        for path, err in result.failed:
            print(f"  FAILED {path}: {err}", file=sys.stderr, flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
