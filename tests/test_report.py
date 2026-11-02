"""Quality report tests: drive a condense run on synthetic Raw files and assert
on the report DATA (`result.report`), plus that a self-contained HTML file is written."""
import math

import numpy as np
import pandas as pd

from simutech import condense
from test_condense import MISSING, make_config, write_raw


def run(tmp_path, files):
    raw, cfg = make_config(tmp_path)
    for name, start, values in files:
        write_raw(raw, name, pd.Timestamp(start), values)
    return condense(cfg, raw, "Tower_Bot")


def test_daily_trends_per_statistic(tmp_path):
    rows = lambda v: [[v, 0.0]] * 50
    result = run(tmp_path, [
        ("tow_bot_a.csv", "2026-03-10 08:00:00", rows(1.0)),
        ("tow_bot_b.csv", "2026-03-11 08:00:00", rows(3.0)),
    ])
    daily = result.report["channels"]["Bend_00"]["daily"]
    assert sorted(daily) == ["2026-03-10", "2026-03-11"]
    assert daily["2026-03-10"]["mean"] == 1.0
    assert daily["2026-03-11"]["mean"] == 3.0
    assert daily["2026-03-11"]["min"] == 3.0 and daily["2026-03-11"]["max"] == 3.0
    assert daily["2026-03-11"]["ac_rms"] == 0.0 and daily["2026-03-11"]["std"] == 0.0
    assert result.report["logger"] == "Tower_Bot"


def test_share_of_full_seconds_and_missing_share_per_channel(tmp_path):
    # 4 seconds: 0 full; 1 has 25 of 50 samples of A missing; 2 entirely missing
    # for A (no Summary); 3 full.
    vals = [[1.0, 2.0] for _ in range(200)]
    for i in range(75, 150):
        vals[i][0] = MISSING
    result = run(tmp_path, [("tow_bot_a.csv", "2026-03-10 08:00:00", vals)])
    a = result.report["channels"]["Bend_00"]
    b = result.report["channels"]["Bend_90"]
    assert math.isclose(a["missing_share"], 75 / 200)
    assert b["missing_share"] == 0.0
    # 3 Summary seconds for A, of which 2 are full
    assert math.isclose(a["full_second_share"], 2 / 3)
    assert b["full_second_share"] == 1.0


def test_stuck_and_clipping_counts_per_channel(tmp_path):
    # Bend_00 flat for 6 s (stuck, default window 5); Bend_90 has 2 clipped samples
    vals = [[2.0, float(np.sin(i))] for i in range(300)]
    vals[0][1] = 7950.0
    vals[1][1] = -7950.0
    result = run(tmp_path, [("tow_bot_a.csv", "2026-03-10 08:00:00", vals)])
    a = result.report["channels"]["Bend_00"]
    b = result.report["channels"]["Bend_90"]
    assert a["stuck_seconds"] == 6 and b["stuck_seconds"] == 0
    assert a["clipped_samples"] == 0 and b["clipped_samples"] == 2
    assert b["clipped_seconds"] == 1


def test_skipped_files_listed_in_report(tmp_path):
    raw, cfg = make_config(tmp_path)
    write_raw(raw, "tow_bot_a.csv", pd.Timestamp("2026-03-10 08:00:00"), [[1.0, 1.0]] * 50)
    write_raw(raw, "tow_bot_bad.csv", pd.Timestamp("2026-03-10 08:10:00"), [[1.0, 1.0, 1.0]] * 50)
    result = condense(cfg, raw, "Tower_Bot")
    assert result.report["skipped_files"] == ["tow_bot_bad.csv"]


def test_html_report_written_self_contained(tmp_path):
    result = run(tmp_path, [
        ("tow_bot_a.csv", "2026-03-10 08:00:00", [[1.0, 2.0]] * 50),
        ("tow_bot_b.csv", "2026-03-11 08:00:00", [[3.0, 2.0]] * 50),
    ])
    path = tmp_path / "out" / "Tower_Bot" / "Tower_Bot_report.html"
    assert result.report_file == path
    html = path.read_text(encoding="utf-8")
    assert "<svg" in html and "Bend_00" in html and "Tower_Bot" in html
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in html and "<script src" not in html


def test_no_data_still_reports_skipped(tmp_path):
    raw, cfg = make_config(tmp_path)
    write_raw(raw, "tow_bot_bad.csv", pd.Timestamp("2026-03-10 08:10:00"), [[1.0, 1.0, 1.0]] * 50)
    result = condense(cfg, raw, "Tower_Bot")
    assert result.report["skipped_files"] == ["tow_bot_bad.csv"]
    assert result.report["channels"] == {}
