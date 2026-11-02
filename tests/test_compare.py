"""Cross-Logger comparison page tests: run `compare` over several synthetic Loggers
and assert on the comparison DATA, plus the self-contained HTML page written."""
import re

import pandas as pd

from simutech.compare import compare
from test_condense import MISSING
from test_loggers import channels, config, write_raw


def make(tmp_path):
    cfg = config(tmp_path, {
        name: {"folder": str(tmp_path / name), "file_pattern": f"{name.lower()}_*.csv",
               "clock_offset_hours": -4, "channels": channels("A", "B")}
        for name in ("Hub", "Nacelle")})
    # Hub: two days, value 1 then 3. Nacelle: an all-missing file and a file of the wrong width.
    write_raw(tmp_path / "Hub" / "hub_2026_03_10_0800.csv", pd.Timestamp("2026-03-10 08:00:00"), 2, value=1.0)
    write_raw(tmp_path / "Hub" / "hub_2026_03_11_0800.csv", pd.Timestamp("2026-03-11 08:00:00"), 2, value=3.0)
    write_raw(tmp_path / "Nacelle" / "nacelle_2026_03_10_0800.csv", pd.Timestamp("2026-03-10 08:00:00"), 2,
              value=MISSING)
    write_raw(tmp_path / "Nacelle" / "nacelle_2026_03_10_0900.csv", pd.Timestamp("2026-03-10 09:00:00"), 5)
    return cfg


def test_comparison_data_puts_loggers_side_by_side(tmp_path):
    data = compare(make(tmp_path)).data
    assert list(data["loggers"]) == ["Hub", "Nacelle"]
    hub, nac = data["loggers"]["Hub"], data["loggers"]["Nacelle"]
    assert hub["full_second_share"] == 1.0 and hub["missing_share"] == 0.0
    assert hub["skipped_files"] == 0 and nac["skipped_files"] == 1
    assert hub["n_samples"] == 100
    assert hub["first_day"] == "2026-03-10" and hub["last_day"] == "2026-03-11"
    assert hub["channels"]["A"]["trend"] == {"first": 1.0, "last": 3.0, "change": 2.0}
    assert hub["report_link"] == "Hub/Hub_report.html"
    # all-missing Nacelle file: no Summary rows, so no channels and no days
    assert nac["channels"] == {} and nac["first_day"] is None


def test_page_is_self_contained_and_links_each_logger_report(tmp_path):
    result = compare(make(tmp_path))
    page = result.page_file.read_text(encoding="utf-8")
    assert result.page_file == tmp_path / "out" / "comparison.html"
    assert 'href="Hub/Hub_report.html"' in page and 'href="Nacelle/Nacelle_report.html"' in page
    assert (tmp_path / "out" / "Hub" / "Hub_report.html").exists()
    assert not re.search(r"(src|href)=['\"]https?:", page) and "<script" not in page


def test_logger_subset(tmp_path):
    data = compare(make(tmp_path), loggers=["Hub"]).data
    assert list(data["loggers"]) == ["Hub"]
