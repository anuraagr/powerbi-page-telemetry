"""v0.3.0 tests: page_catalog, report_views, user_views silver tables
plus unused-page detection (LEFT JOIN page_catalog vs page_views).

The headline story for Jon at Incyte is "which pages in our 60-page
Phase III Clinical Trial report has nobody opened in 90 days?" — that
question is unanswerable without the page_catalog feed, because
`'Report page views'` is a fact table and zero-view pages literally
don't appear in it. These tests guard the end-to-end correctness of
that LEFT JOIN math and the three new silver feeds.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import collector
import pytest
from collector import (
    SILVER_SCHEMA_VERSION,
    LiveAdapter,
    _hash_upn,
    main,
)

# ---------------------------------------------------------------------------
# v0.3.0 schema + summary pinning
# ---------------------------------------------------------------------------

def test_schema_version_is_minor_bump_to_1_1_0() -> None:
    """v0.3.0 is additive (new tables, no breaking column changes) so the
    silver schema bumps the MINOR component only — old readers of
    page_views.csv keep working."""
    assert SILVER_SCHEMA_VERSION == "1.1.0"


def _read_silver(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        first = f.readline()
        if not first.startswith("#"):
            f.seek(0)
        return list(csv.DictReader(f))


def _read_summary(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "_run_summary.json").read_text(encoding="utf-8"))


def test_run_emits_all_four_silver_feeds(tmp_path: Path) -> None:
    main(["--mock", "--out", str(tmp_path)])
    silver = tmp_path / "silver"
    for name in ("page_views.csv", "page_catalog.csv", "report_views.csv", "user_views.csv"):
        assert (silver / name).exists(), f"missing silver feed: {name}"


def test_run_summary_has_v030_keys(tmp_path: Path) -> None:
    main(["--mock", "--out", str(tmp_path)])
    s = _read_summary(tmp_path)
    for key in (
        "page_view_rows",
        "page_catalog_rows",
        "report_view_rows",
        "user_view_rows",
        "unused_pages",
        "reports_with_unused_pages",
        "silver_paths",
    ):
        assert key in s, f"missing v0.3.0 summary key: {key}"
    # v0.2.x back-compat fields still present
    assert s["rows"] == s["page_view_rows"]
    assert s["silver_path"] == s["silver_paths"]["page_views"]


def test_run_summary_pins_mock_v030_numbers(tmp_path: Path) -> None:
    """If the generator or the unused_pages.json overlay changes, the
    headline numbers in README and the Power BI dashboard would silently
    move. Pin them. To update: confirm the change is intentional, then
    edit these constants."""
    main(["--mock", "--out", str(tmp_path)])
    s = _read_summary(tmp_path)
    assert s["page_view_rows"] == 15_480
    assert s["page_catalog_rows"] == 232   # 222 viewed + 10 unused overlay
    assert s["report_view_rows"] == 1_350
    assert s["user_view_rows"] == 6_289
    assert s["unused_pages"] == 10
    assert s["reports_with_unused_pages"] == 3


# ---------------------------------------------------------------------------
# Unused-page detection — the core Jon use case
# ---------------------------------------------------------------------------

def test_unused_pages_is_left_join_of_catalog_minus_page_views(tmp_path: Path) -> None:
    """The math we ship to Jon must be exactly:
        unused = page_catalog LEFT JOIN page_views WHERE page_views IS NULL
    Verify directly from the silver CSVs (don't trust the summary)."""
    main(["--mock", "--out", str(tmp_path)])
    pv_rows = _read_silver(tmp_path / "silver" / "page_views.csv")
    pc_rows = _read_silver(tmp_path / "silver" / "page_catalog.csv")

    viewed = {(r["workspace_id"], r["report_id"], r["page_id"]) for r in pv_rows}
    catalog = {(r["workspace_id"], r["report_id"], r["page_id"]) for r in pc_rows}
    unused = catalog - viewed

    summary = _read_summary(tmp_path)
    assert len(unused) == summary["unused_pages"]


def test_unused_pages_appear_only_in_clinical_trial_reports(tmp_path: Path) -> None:
    """The overlay deliberately seeds unused pages in the 3 big
    clinical-trial reports (rep-clin-study101/202/303) because that's the
    story we're selling: 'your 60-page Phase III report has 5 dead pages.'
    """
    main(["--mock", "--out", str(tmp_path)])
    pv_rows = _read_silver(tmp_path / "silver" / "page_views.csv")
    pc_rows = _read_silver(tmp_path / "silver" / "page_catalog.csv")

    viewed = {(r["workspace_id"], r["report_id"], r["page_id"]) for r in pv_rows}
    unused = [
        (r["report_id"], r["page_name"])
        for r in pc_rows
        if (r["workspace_id"], r["report_id"], r["page_id"]) not in viewed
    ]
    reports_with_unused = {rep for rep, _ in unused}
    assert reports_with_unused == {
        "rep-clin-study101",
        "rep-clin-study202",
        "rep-clin-study303",
    }
    # at least one unused page name carries a "deprecated / legacy / debug"
    # signal — that's what makes the demo click for a clinical SE
    names = " ".join(name.lower() for _, name in unused)
    assert any(tag in names for tag in ("deprecated", "legacy", "debug", "obsolete"))


# ---------------------------------------------------------------------------
# page_catalog feed
# ---------------------------------------------------------------------------

def test_page_catalog_has_required_columns(tmp_path: Path) -> None:
    main(["--mock", "--out", str(tmp_path)])
    rows = _read_silver(tmp_path / "silver" / "page_catalog.csv")
    assert rows, "page_catalog.csv should not be empty"
    required = {
        "workspace_id", "workspace_name", "report_id", "report_name",
        "page_id", "page_name", "page_ordinal", "catalog_pulled_at",
    }
    assert required.issubset(rows[0].keys())


def test_page_catalog_pulled_at_is_iso_utc(tmp_path: Path) -> None:
    main(["--mock", "--out", str(tmp_path)])
    rows = _read_silver(tmp_path / "silver" / "page_catalog.csv")
    ts = rows[0]["catalog_pulled_at"]
    # ISO 8601 with timezone offset (the collector emits "+00:00")
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?[+-]\d{2}:\d{2}$", ts)


def test_live_adapter_list_report_pages_calls_documented_endpoint() -> None:
    """Guards that we hit the supported, GA endpoint exactly as documented.
    `GET /v1.0/myorg/groups/{wsId}/reports/{reportId}/pages` — workspace-
    scoped, requires SP membership in the workspace (same posture the rest
    of the collector already needs)."""
    a = LiveAdapter.__new__(LiveAdapter)
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "value": [
            {"name": "ReportSection1", "displayName": "Overview", "order": 1},
            {"name": "ReportSection2", "displayName": "Detail", "order": 2},
        ]
    }
    a._request = MagicMock(return_value=fake_response)
    report = collector.Report(
        id="rep-1", name="My Report",
        workspace_id="ws-1", workspace_name="WS-1",
    )
    rows = list(a.list_report_pages(report))
    assert len(rows) == 2
    method, url = a._request.call_args.args[0], a._request.call_args.args[1]
    assert method == "GET"
    assert url.endswith("/v1.0/myorg/groups/ws-1/reports/rep-1/pages")
    # We tolerate 403/404 (paginated reports) rather than raising
    assert a._request.call_args.kwargs.get("allow_status") == (403, 404)
    # Field mapping: REST `name`=page_id, `displayName`=page_name, `order`=ordinal
    assert rows[0].page_id == "ReportSection1"
    assert rows[0].page_name == "Overview"
    assert rows[0].page_ordinal == 1


def test_live_adapter_list_report_pages_tolerates_403_paginated_report() -> None:
    """Paginated reports (and some dataflow-backed reports) return
    403/404 on the /pages endpoint. We must NOT crash the whole run —
    just emit zero rows for that report and continue."""
    a = LiveAdapter.__new__(LiveAdapter)
    fake_response = MagicMock()
    fake_response.status_code = 403
    a._request = MagicMock(return_value=fake_response)
    report = collector.Report(
        id="rep-pag", name="Paginated",
        workspace_id="ws-1", workspace_name="WS-1",
    )
    rows = list(a.list_report_pages(report))
    assert rows == []
    # json() must NOT have been called on a 403 — guards against accidental
    # JSON-parse-then-crash on an error body
    fake_response.json.assert_not_called()


# ---------------------------------------------------------------------------
# report_views feed
# ---------------------------------------------------------------------------

def test_report_views_has_required_columns(tmp_path: Path) -> None:
    main(["--mock", "--out", str(tmp_path)])
    rows = _read_silver(tmp_path / "silver" / "report_views.csv")
    assert rows
    required = {
        "workspace_id", "workspace_name", "capacity_name",
        "report_id", "report_name", "view_date",
        "view_count", "unique_users", "avg_session_seconds",
    }
    assert required.issubset(rows[0].keys())


def test_report_views_view_count_reconciles_with_page_views(tmp_path: Path) -> None:
    """Sanity check on the mock aggregator: total report-level views for
    a given (report, date) should be >= the max single-page views on that
    date (a user can view multiple pages in one session, so reports often
    have more 'views' than any single page)."""
    main(["--mock", "--out", str(tmp_path)])
    pv = _read_silver(tmp_path / "silver" / "page_views.csv")
    rv = _read_silver(tmp_path / "silver" / "report_views.csv")
    # For each (report, date), max page-level view_count should be <=
    # report-level view_count (because report = sum/agg of page sessions).
    rv_by_key = {(r["report_id"], r["view_date"]): int(r["view_count"]) for r in rv}
    pv_max = {}
    for r in pv:
        k = (r["report_id"], r["view_date"])
        v = int(r["view_count"])
        if v > pv_max.get(k, 0):
            pv_max[k] = v
    for k, max_page in pv_max.items():
        assert rv_by_key.get(k, 0) >= max_page, (
            f"report-level view_count for {k} should be >= max page view_count "
            f"({rv_by_key.get(k)} vs {max_page})"
        )


# ---------------------------------------------------------------------------
# user_views feed + UPN hashing
# ---------------------------------------------------------------------------

def test_user_views_has_hashed_upns_only(tmp_path: Path) -> None:
    """Silver must NEVER carry raw UPNs — only the 16-hex-char SHA-256
    prefix. Guards a regression that would leak PII into a customer's
    OneLake."""
    main(["--mock", "--out", str(tmp_path)])
    rows = _read_silver(tmp_path / "silver" / "user_views.csv")
    assert rows
    for r in rows:
        h = r["user_id_hash"]
        assert re.fullmatch(r"[0-9a-f]{16}", h), f"non-hex hash leaked: {h!r}"
        assert "@" not in h
        assert "user" not in h.lower()  # no plaintext "userNNN" leakage


def test_user_views_has_required_columns(tmp_path: Path) -> None:
    main(["--mock", "--out", str(tmp_path)])
    rows = _read_silver(tmp_path / "silver" / "user_views.csv")
    required = {
        "workspace_id", "workspace_name", "report_id", "report_name",
        "user_id_hash", "view_date", "view_count", "distinct_pages_viewed",
    }
    assert required.issubset(rows[0].keys())


def test_hash_upn_is_deterministic_short_and_case_insensitive() -> None:
    """The hash is the join key between page_views and user_views.
    Determinism is non-negotiable; length is fixed at 16 hex chars
    (64 bits — collision-resistant for any real org). UPNs in Azure AD
    are case-insensitive, so we normalize before hashing — alice@x.com
    and ALICE@X.com are the same user and must collapse to the same hash."""
    h1 = _hash_upn("alice@incyte.com")
    h2 = _hash_upn("alice@incyte.com")
    h3 = _hash_upn("Alice@INCYTE.com")
    h4 = _hash_upn("  alice@incyte.com  ")  # whitespace-tolerant
    assert h1 == h2 == h3 == h4
    assert re.fullmatch(r"[0-9a-f]{16}", h1)
    # Different UPN must produce a different hash
    assert _hash_upn("bob@incyte.com") != h1


def test_hash_upn_handles_blank() -> None:
    """Some DAX rows can come back with no [User] value (system jobs,
    refresh-on-publish). Don't crash — return an empty sentinel so
    downstream SQL can GROUP BY user_id_hash without NULL-handling
    boilerplate."""
    assert _hash_upn("") == ""
    assert _hash_upn(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Determinism — mock run twice must produce byte-identical silver
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("feed", ["page_views", "page_catalog", "report_views", "user_views"])
def test_silver_feeds_are_byte_identical_across_runs(tmp_path: Path, feed: str) -> None:
    """The whole sample-data contract depends on mock runs being
    reproducible across machines and Python versions. If this regresses,
    docs/README headline numbers drift silently."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    main(["--mock", "--out", str(a)])
    main(["--mock", "--out", str(b)])
    assert (a / "silver" / f"{feed}.csv").read_bytes() == (b / "silver" / f"{feed}.csv").read_bytes()
