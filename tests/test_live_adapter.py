"""End-to-end behavior tests for LiveAdapter against a mocked Power BI REST.

These pin the contract we expect from the real Power BI service so that
schema drift in the Modern Usage Metrics dataset, or an accidental
regression in our code, will surface in CI before a customer sees it.

Covered:
  - Per-workspace UM dataset lookup is cached (one /datasets call per
    workspace even when there are many reports).
  - Workspaces without a bootstrapped UM dataset are skipped with an
    empty string return and recorded in workspaces_not_bootstrapped.
  - executeQueries body is well-formed JSON with a DAX query that
    filters to the requested report id + date window.
  - executeQueries row-shape coercion lands the expected
    workspace/report/page/views/users fields on PageViewRow.
  - End-to-end run() against a mocked adapter writes the expected
    bronze partition + silver CSV and surfaces a non-empty
    workspaces_not_bootstrapped list in _run_summary.json.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

ETL = Path(__file__).resolve().parent.parent / "etl"
if str(ETL) not in sys.path:
    sys.path.insert(0, str(ETL))

import collector  # noqa: E402


def _resp(status: int, body: dict | None = None, headers: dict | None = None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = body or {}
    r.text = ""
    r.raise_for_status = MagicMock()
    return r


def _live() -> collector.LiveAdapter:
    """Build a LiveAdapter without actually doing OAuth."""
    a = collector.LiveAdapter.__new__(collector.LiveAdapter)
    a.tenant_id = "tenant-id"
    a.client_id = "client-id"
    a.client_secret = "client-secret"
    a._token = "fake-bearer"
    a._token_expires = 9_999_999_999.0
    a._usage_metrics_dataset_per_workspace = {}
    a._workspace_for_dataset = {}
    a._workspaces_by_id = {}
    a._dataset_name_by_id = {}
    a.workspaces_not_bootstrapped = []
    a._usage_dataset_name = "Usage Metrics Report"
    a._xmla_available = False
    return a


# ---------------------------------------------------------------------------
# ensure_usage_metrics_dataset
# ---------------------------------------------------------------------------

@patch("collector.time.sleep", lambda *_a, **_k: None)
@patch("collector.requests.request")
def test_ensure_usage_metrics_dataset_caches_per_workspace(mock_req):
    """Multiple reports in the same workspace should trigger ONE /datasets
    call, not one per report."""
    mock_req.return_value = _resp(200, body={"value": [
        {"id": "ds-aaa", "name": "Usage Metrics Report"},
        {"id": "ds-bbb", "name": "Sales Q4"},
    ]})
    a = _live()
    rep1 = collector.Report(id="r1", name="A", workspace_id="ws1", workspace_name="WS1")
    rep2 = collector.Report(id="r2", name="B", workspace_id="ws1", workspace_name="WS1")
    rep3 = collector.Report(id="r3", name="C", workspace_id="ws2", workspace_name="WS2")
    mock_req.side_effect = [
        _resp(200, body={"value": [{"id": "ds-aaa", "name": "Usage Metrics Report"}]}),
        # workspace 2 returns no UM dataset
        _resp(200, body={"value": [{"id": "ds-ccc", "name": "Other dataset"}]}),
    ]
    assert a.ensure_usage_metrics_dataset(rep1) == "ds-aaa"
    assert a.ensure_usage_metrics_dataset(rep2) == "ds-aaa"  # cached, no HTTP call
    assert a.ensure_usage_metrics_dataset(rep3) == ""        # workspace 2 not bootstrapped
    assert mock_req.call_count == 2                          # one per *workspace*, not per report
    assert "WS2" in a.workspaces_not_bootstrapped


@patch("collector.time.sleep", lambda *_a, **_k: None)
@patch("collector.requests.request")
def test_legacy_um_dataset_v2_prefix_still_matches(mock_req):
    """Some tenants still have legacy 'Usage Metrics Report v2 - <name>'
    naming. Our prefix match must cover both."""
    mock_req.return_value = _resp(200, body={"value": [
        {"id": "ds-legacy", "name": "Usage Metrics Report v2 - Sales"},
    ]})
    a = _live()
    rep = collector.Report(id="r", name="X", workspace_id="ws", workspace_name="WS")
    assert a.ensure_usage_metrics_dataset(rep) == "ds-legacy"


# ---------------------------------------------------------------------------
# query_page_views via executeQueries (REST path)
# ---------------------------------------------------------------------------

@patch("collector.time.sleep", lambda *_a, **_k: None)
@patch("collector.requests.request")
def test_query_page_views_posts_well_formed_executequeries(mock_req):
    """The POST body to /executeQueries must be a JSON object with a
    `queries` array whose first entry has a DAX query that:
       - filters to the report id
       - filters to the date window (closed interval)
       - aggregates Views, UniqueUsers, AvgDwellSeconds
    """
    mock_req.return_value = _resp(200, body={
        "results": [{
            "tables": [{
                "rows": [
                    {
                        "'Report page views'[Report Id]": "report-1",
                        "'Report page views'[Report page name]": "Overview",
                        "'Report page views'[Date]": "2026-05-27T00:00:00",
                        "[Views]": 42,
                        "[UniqueUsers]": 7,
                        "[AvgDwellSeconds]": 18.5,
                    },
                ]
            }]
        }]
    })
    a = _live()
    a._workspace_for_dataset["ds-aaa"] = "ws-id-1"
    rows = list(
        a.query_page_views("ds-aaa", date(2026, 5, 20), date(2026, 5, 27), report_id="report-1")
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.report_id == "report-1"
    assert row.page_name == "Overview"
    assert row.view_date == "2026-05-27"
    assert row.view_count == 42
    assert row.unique_users == 7
    assert abs(row.avg_dwell_seconds - 18.5) < 1e-6
    # Inspect the request body we sent
    call_kwargs = mock_req.call_args.kwargs
    body = call_kwargs["json"]
    assert "queries" in body
    dax = body["queries"][0]["query"]
    assert 'Report Id] = "report-1"' in dax
    assert "DATE(2026,5,20)" in dax
    assert "DATE(2026,5,27)" in dax
    assert "SUMMARIZECOLUMNS" in dax
    # URL must hit the workspace-scoped executeQueries endpoint
    url = mock_req.call_args.args[1]
    assert url.endswith("/groups/ws-id-1/datasets/ds-aaa/executeQueries")


def test_query_page_views_empty_dataset_id_is_a_noop():
    """A workspace that wasn't bootstrapped surfaces as an empty
    dataset_id; query_page_views must yield nothing and NOT call HTTP."""
    a = _live()
    rows = list(
        a.query_page_views("", date(2026, 5, 1), date(2026, 5, 27), report_id="r1")
    )
    assert rows == []


def test_query_page_views_requires_report_id():
    """The per-workspace UM dataset covers many reports; calling without
    a report_id would scan the whole workspace and is rejected to avoid
    a surprise bill / quota event."""
    a = _live()
    a._workspace_for_dataset["ds-aaa"] = "ws-id-1"
    import pytest
    with pytest.raises(ValueError, match="report_id"):
        list(a.query_page_views("ds-aaa", date(2026, 5, 1), date(2026, 5, 27)))


# ---------------------------------------------------------------------------
# End-to-end via run() with a fake adapter
# ---------------------------------------------------------------------------

class _FakeAdapter(collector.CollectorAdapter):
    """Drives run() with a mix of bootstrapped and un-bootstrapped
    workspaces so we can verify the summary's accounting."""

    def __init__(self):
        self.workspaces_not_bootstrapped: list[str] = []

    def list_workspaces(self):
        yield collector.Workspace(id="ws-a", name="WS-A", capacity_name="cap-1")
        yield collector.Workspace(id="ws-b", name="WS-B", capacity_name="cap-1")

    def list_reports(self, workspace):
        if workspace.id == "ws-a":
            yield collector.Report(id="r1", name="R1", workspace_id="ws-a", workspace_name="WS-A")
            yield collector.Report(id="r2", name="R2", workspace_id="ws-a", workspace_name="WS-A")
        else:
            yield collector.Report(id="r3", name="R3", workspace_id="ws-b", workspace_name="WS-B")

    def ensure_usage_metrics_dataset(self, report):
        if report.workspace_id == "ws-b":
            self.workspaces_not_bootstrapped.append(report.workspace_name)
            return ""
        return "ds-shared"  # ws-a's two reports share one dataset

    def query_page_views(self, dataset_id, since, until, report_id=None):
        if not dataset_id:
            return
        yield collector.PageViewRow(
            workspace_id="ws-a", workspace_name="WS-A", capacity_name="cap-1",
            report_id=report_id or "r?", report_name="R?",
            report_total_pages=1, page_id="p1", page_name="P1",
            page_ordinal=1, view_date="2026-05-27",
            view_count=1, unique_users=1, avg_dwell_seconds=1.0,
            top_persona="",
        )


def test_run_summary_reflects_bootstrap_and_dedup(tmp_path):
    summary = collector.run(_FakeAdapter(), date(2026, 5, 1), date(2026, 5, 27), tmp_path)
    assert summary["workspaces"] == 2
    assert summary["reports"] == 3
    assert summary["datasets"] == 1  # ws-a's two reports share one UM dataset
    assert summary["reports_skipped_no_bootstrap"] == 1
    assert summary["workspaces_not_bootstrapped"] == ["WS-B"]
    assert summary["rows"] == 2  # one per ws-a report

    silver = tmp_path / "silver" / "page_views.csv"
    assert silver.exists()
    bronze_partition = Path(summary["bronze_partition"])
    bronze_files = sorted(p.name for p in bronze_partition.glob("*.csv"))
    assert bronze_files == ["ws-a__r1.csv", "ws-a__r2.csv"]
    # _run_summary.json on disk must match the returned summary
    on_disk = json.loads((tmp_path / "_run_summary.json").read_text(encoding="utf-8"))
    assert on_disk["workspaces_not_bootstrapped"] == ["WS-B"]
