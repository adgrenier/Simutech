"""The all-Loggers command line run, on synthetic Raw files."""
import datetime
import json

import pandas as pd

from simutech.cli import main
from test_condense import write_raw


def make_two_logger_config(tmp_path):
    cfg = {"output_folder": str(tmp_path / "out"), "loggers": {}}
    for name, pattern in (("Tower_Bot", "tow_bot_*.csv"), ("Tower_Top", "tow_top_*.csv")):
        folder = tmp_path / name
        folder.mkdir()
        cfg["loggers"][name] = {"folder": str(folder), "file_pattern": pattern,
                                "clock_offset_hours": -4,
                                "channels": [{"name": "A", "unit": "mV"}, {"name": "B", "unit": "mV"}]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    return path, cfg


def test_runs_all_loggers_for_the_default_90_day_window_with_progress(tmp_path, capsys):
    path, cfg = make_two_logger_config(tmp_path)
    today = datetime.date(2026, 6, 1)
    for logger, prefix in (("Tower_Bot", "tow_bot"), ("Tower_Top", "tow_top")):
        folder = tmp_path / logger
        write_raw(folder, f"{prefix}_2026_05_20_0815.csv", pd.Timestamp("2026-05-20 08:15:00"), [[1.0, 1.0]] * 50)
        write_raw(folder, f"{prefix}_2026_01_20_0815.csv", pd.Timestamp("2026-01-20 08:15:00"), [[1.0, 1.0]] * 50)

    code = main(["--config", str(path)], today=today)

    assert code == 0
    for logger in ("Tower_Bot", "Tower_Top"):
        names = sorted(p.name for p in (tmp_path / "out" / logger).glob("*.parquet"))
        assert names == [f"{logger}_2026-05-20.parquet"]  # Jan 20 is more than 90 days back
    out = capsys.readouterr().out
    assert "Tower_Bot" in out and "Tower_Top" in out and "processed" in out
    assert "2026-03-03" in out  # 90 days before 2026-06-01


def test_start_date_and_logger_selection_and_raw_folder_override(tmp_path):
    path, cfg = make_two_logger_config(tmp_path)
    sample = tmp_path / "sample"
    sample.mkdir()
    write_raw(sample, "tow_bot_2026_05_20_0815.csv", pd.Timestamp("2026-05-20 08:15:00"), [[1.0, 1.0]] * 50)
    main(["--config", str(path), "--logger", "Tower_Bot", "--raw-folder", str(sample),
          "--start-date", "2026-05-01"], today=datetime.date(2026, 6, 1))
    assert (tmp_path / "out" / "Tower_Bot" / "Tower_Bot_2026-05-20.parquet").exists()
    assert not (tmp_path / "out" / "Tower_Top").exists()
