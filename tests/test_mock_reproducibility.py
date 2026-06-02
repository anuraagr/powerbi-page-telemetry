"""End-to-end reproducibility guard rail.

If `generate_sample_data.py` or the collector ever drifts, the headline
numbers shown in the README and bundled dashboard would silently change.
This test pins them. To update, run the generator and the collector,
confirm the new numbers are intentional, then edit the constants below.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from collector import SILVER_SCHEMA_VERSION, MockAdapter, main

# Headline numbers also documented in README.md.
EXPECTED_ROWS = 15_480
EXPECTED_VIEWS = 154_815
EXPECTED_REPORTS = 15
EXPECTED_WORKSPACES = 5
EXPECTED_PAGES_VIEWED = 222  # distinct (report, page) with at least one view
EXPECTED_PAGES_DEFINED = 231  # sum of report_total_pages


def _read_silver(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        first = f.readline()
        if not first.startswith("#"):
            f.seek(0)
        return list(csv.DictReader(f))


def test_mock_run_produces_pinned_signature(tmp_path: Path) -> None:
    exit_code = main(["--mock", "--out", str(tmp_path)])
    assert exit_code == 0

    summary = json.loads((tmp_path / "_run_summary.json").read_text(encoding="utf-8"))
    assert summary["schema_version"] == SILVER_SCHEMA_VERSION
    assert summary["workspaces"] == EXPECTED_WORKSPACES
    assert summary["reports"] == EXPECTED_REPORTS
    assert summary["rows"] == EXPECTED_ROWS
    assert summary["errors"] == []
    assert "bronze_partition" in summary
    assert "dt=" in summary["bronze_partition"]

    silver = _read_silver(tmp_path / "silver" / "page_views.csv")
    assert len(silver) == EXPECTED_ROWS

    total_views = sum(int(r["view_count"]) for r in silver)
    assert total_views == EXPECTED_VIEWS, (
        f"silver view-count drifted: expected {EXPECTED_VIEWS:,}, got {total_views:,}"
    )

    distinct_pages = {(r["report_id"], r["page_id"]) for r in silver}
    assert len(distinct_pages) == EXPECTED_PAGES_VIEWED

    # Sum of report_total_pages is the "universe" — pages that exist in
    # report design, including the 9 never-viewed pages the generator
    # deliberately creates. Both numbers appear in the README.
    pages_per_report = {r["report_id"]: int(r["report_total_pages"]) for r in silver}
    assert sum(pages_per_report.values()) == EXPECTED_PAGES_DEFINED


def test_silver_csv_has_schema_version_comment(tmp_path: Path) -> None:
    main(["--mock", "--out", str(tmp_path)])
    first = (tmp_path / "silver" / "page_views.csv").read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("# silver_schema_version=")
    assert first.split("=", 1)[1] == SILVER_SCHEMA_VERSION


def test_bronze_is_partitioned_by_date(tmp_path: Path) -> None:
    main(["--mock", "--out", str(tmp_path)])
    bronze = tmp_path / "bronze"
    partitions = list(bronze.iterdir())
    assert len(partitions) == 1
    assert partitions[0].name.startswith("dt=")
    # v0.3.0 layout: bronze/dt=*/page_views/{wsId}__{reportId}.csv
    page_view_files = list((partitions[0] / "page_views").glob("*.csv"))
    assert len(page_view_files) == EXPECTED_REPORTS
    # All 4 feed sub-folders should exist (catalog/report_views/user_views may
    # have fewer files since they only land when the adapter emits rows).
    for feed in ("page_views", "page_catalog", "report_views", "user_views"):
        assert (partitions[0] / feed).is_dir(), f"missing bronze sub-folder: {feed}"


def test_mock_max_csv_date_is_deterministic() -> None:
    # If the bundled sample data is regenerated, this changes — pin it.
    assert MockAdapter.max_csv_date().isoformat() == "2026-05-27"


def test_dax_query_window_includes_both_endpoints() -> None:
    """The DAX should use closed-interval date comparisons (>= and <=)
    so a 1-day window actually pulls that day, AND filter to a specific
    report id so we only get rows for the report being collected."""
    from datetime import date

    from collector import LiveAdapter

    # Construct without going through __init__ so we don't need real creds.
    adapter = LiveAdapter.__new__(LiveAdapter)
    dax = adapter._dax_with_filters(
        date(2026, 5, 27),
        date(2026, 5, 27),
        report_id="11111111-2222-3333-4444-555555555555",
    )
    assert "DATE(2026,5,27)" in dax
    assert "[Date] >=" in dax
    assert "[Date] <=" in dax
    assert '[Report Id] = "11111111-2222-3333-4444-555555555555"' in dax
    # CALCULATETABLE pushes filters down so SUMMARIZECOLUMNS only sees
    # the report's rows; cheaper on workspaces with many reports.
    assert "CALCULATETABLE" in dax


@pytest.mark.parametrize("status, retryable", [
    (200, False),
    (404, False),
    (401, False),
    (429, True),
    (500, True),
    (502, True),
    (503, True),
    (504, True),
])
def test_retry_status_set_is_correct(status: int, retryable: bool) -> None:
    from collector import LiveAdapter
    is_retryable = status in LiveAdapter._RETRY_STATUS
    assert is_retryable is retryable
