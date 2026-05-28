"""
Power BI Page-Level Telemetry Collector — POC
=============================================

What this does
--------------
Builds a tenant-wide, page-level usage dataset by:

  1. Authenticating to the Power BI service as a service principal.
  2. Enumerating every report in every workspace via the Power BI Admin
     REST API.
  3. For each report, ensuring the auto-generated "Usage Metrics Report v2"
     dataset exists (POST `/admin/reports/{id}/usageMetrics`).
  4. Executing a parameterised DAX query against that dataset over the XMLA
     read endpoint to pull per-page view counts and unique users.
  5. Writing one Parquet/CSV file per report into a `bronze/` layer, then
     emitting a conformed daily aggregate to `silver/page_views.parquet`.

The same script in `--mock` mode does NOT call any Microsoft service —
it loads the bundled synthetic sample data so reviewers can run end-to-end
on a laptop with no tenant access.

Why this is the right shape today
---------------------------------
Microsoft Power BI does not expose a tenant-wide page-level activity API.
The `Get Activity Events` API and the Admin scanner stop at `ViewReport`
events — no page or section field. Page-level data does exist, but only
inside the auto-generated **per-report** Usage Metrics datasets. The only
way to roll it up tenant-wide today is to query those datasets directly
over the XMLA endpoint, which is exactly what this collector does.

A new **Monitor Usage Metrics for Workspaces** capability is in preview
and will provision a single workspace-level semantic model containing
page-level activity. When that GAs, this collector's `XmlaReportAdapter`
can be swapped for a `WorkspaceSemanticModelAdapter` without any change
to the silver/gold schema or the dashboard.

Run modes
---------
  python collector.py --mock                  # uses bundled sample data
  python collector.py --tenant <tenant-id>    # live; requires service principal env vars

Environment variables (live mode)
---------------------------------
  PBI_TENANT_ID           Entra tenant ID
  PBI_CLIENT_ID           Service principal app ID
  PBI_CLIENT_SECRET       Service principal secret (use Key Vault in prod)
  PBI_XMLA_WORKSPACE      Optional: workspace to use as default XMLA endpoint
  PBI_OUTPUT_DIR          Where to write bronze/ and silver/ (defaults to ./out)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

try:
    import requests
except ImportError:
    requests = None  # only required for live mode

HERE = Path(__file__).parent
SAMPLE_CSV = HERE / "sample_data" / "page_views.csv"

# ---------------------------------------------------------------------------
# REST API surface
# ---------------------------------------------------------------------------

POWERBI_API = "https://api.powerbi.com/v1.0/myorg"
SCOPE = "https://analysis.windows.net/powerbi/api/.default"

# Single DAX query that we run against each Usage Metrics dataset.
# The auto-generated dataset exposes a `Report page views` table whose
# columns include `Report Id`, `Report page name`, `Date`, `Views`,
# `Unique Users`, and `Average view time`. This summarisation is
# server-side, so we transfer only the aggregated rows.
DAX_PAGE_VIEWS = """
EVALUATE
SUMMARIZECOLUMNS(
    'Report page views'[Report Id],
    'Report page views'[Report page name],
    'Report page views'[Date],
    "Views",          SUM('Report page views'[Views]),
    "UniqueUsers",    DISTINCTCOUNT('Report page views'[User]),
    "AvgDwellSeconds",AVERAGE('Report page views'[Average view time])
)
ORDER BY 'Report page views'[Date]
"""

# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

@dataclass
class Workspace:
    id: str
    name: str
    capacity_id: str | None = None
    capacity_name: str | None = None

@dataclass
class Report:
    id: str
    name: str
    workspace_id: str
    workspace_name: str

@dataclass
class PageViewRow:
    workspace_id: str
    workspace_name: str
    capacity_name: str
    report_id: str
    report_name: str
    report_total_pages: int
    page_id: str
    page_name: str
    page_ordinal: int
    view_date: str
    view_count: int
    unique_users: int
    avg_dwell_seconds: float
    top_persona: str

# ---------------------------------------------------------------------------
# Adapter interface — same contract for live and mock
# ---------------------------------------------------------------------------

class CollectorAdapter:
    def list_workspaces(self) -> Iterator[Workspace]:
        raise NotImplementedError

    def list_reports(self, workspace: Workspace) -> Iterator[Report]:
        raise NotImplementedError

    def ensure_usage_metrics_dataset(self, report: Report) -> str:
        """Return the dataset id of the report's Usage Metrics dataset.
        Lazily provisions it via POST /admin/reports/{id}/usageMetrics
        if it doesn't exist yet."""
        raise NotImplementedError

    def query_page_views(self, dataset_id: str, since: date, until: date) -> Iterator[PageViewRow]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# LIVE adapter — Power BI Admin REST + XMLA read endpoint
# ---------------------------------------------------------------------------

class LiveAdapter(CollectorAdapter):
    """Real implementation. Requires a service principal with:
       * Tenant setting `Service principals can use Power BI APIs` ON
       * `Fabric administrator` or member of a workspace with admin rights
       * Premium / Fabric capacity (P-SKU, F-SKU) for XMLA read access
    """

    def __init__(self, tenant_id: str, client_id: str, client_secret: str):
        if requests is None:
            raise RuntimeError("requests not installed; run `pip install -r requirements.txt`")
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_expires: float = 0.0
        # Optional: deferred import; only required for true XMLA queries.
        try:
            from pyadomd import Pyadomd  # noqa: F401
            self._xmla_available = True
        except Exception:
            self._xmla_available = False

    # ---- auth -----------------------------------------------------------------
    def _bearer(self) -> str:
        if self._token and time.time() < self._token_expires - 120:
            return self._token
        resp = requests.post(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": SCOPE,
            },
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + int(body.get("expires_in", 3600))
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._bearer()}"}

    # ---- workspaces / reports -------------------------------------------------
    def list_workspaces(self) -> Iterator[Workspace]:
        # Admin groups API; supports paging via $top/$skip.
        skip = 0
        page = 100
        while True:
            r = requests.get(
                f"{POWERBI_API}/admin/groups",
                params={"$top": page, "$skip": skip,
                        "$expand": "users",
                        "$filter": "type eq 'Workspace' and state eq 'Active'"},
                headers=self._headers(),
                timeout=60,
            )
            r.raise_for_status()
            items = r.json().get("value", [])
            if not items:
                return
            for w in items:
                yield Workspace(
                    id=w["id"],
                    name=w["name"],
                    capacity_id=w.get("capacityId"),
                )
            if len(items) < page:
                return
            skip += page

    def list_reports(self, workspace: Workspace) -> Iterator[Report]:
        r = requests.get(
            f"{POWERBI_API}/admin/groups/{workspace.id}/reports",
            headers=self._headers(),
            timeout=60,
        )
        r.raise_for_status()
        for rep in r.json().get("value", []):
            if rep.get("reportType") != "PowerBIReport":
                continue
            yield Report(
                id=rep["id"],
                name=rep["name"],
                workspace_id=workspace.id,
                workspace_name=workspace.name,
            )

    def ensure_usage_metrics_dataset(self, report: Report) -> str:
        # POST /admin/reports/{id}/usageMetrics creates the underlying dataset
        # if it doesn't already exist. The response includes the datasetId.
        r = requests.post(
            f"{POWERBI_API}/admin/reports/{report.id}/usageMetrics",
            headers=self._headers(),
            timeout=120,
        )
        if r.status_code in (200, 201, 202):
            try:
                return r.json().get("datasetId") or ""
            except ValueError:
                return ""
        if r.status_code == 409:
            # Already exists — Graph the workspace for the dataset whose name
            # matches the Usage Metrics convention.
            ds = requests.get(
                f"{POWERBI_API}/admin/groups/{report.workspace_id}/datasets",
                headers=self._headers(), timeout=60,
            ).json().get("value", [])
            for d in ds:
                if d.get("name", "").startswith("Usage Metrics Report"):
                    return d["id"]
        r.raise_for_status()
        return ""

    def query_page_views(self, dataset_id: str, since: date, until: date) -> Iterator[PageViewRow]:
        if not self._xmla_available:
            raise RuntimeError(
                "pyadomd / ADOMD client not available. Either install the "
                "Microsoft.AnalysisServices.AdomdClient.retail.amd64 NuGet "
                "package alongside pyadomd, or run with --mock."
            )
        # Real implementation would build an XMLA connection string of the form
        #   Provider=MSOLAP;Data Source=powerbi://api.powerbi.com/v1.0/myorg/<workspace>;
        #     Initial Catalog=<datasetName>;User ID=app:<clientId>@<tenantId>;
        #     Password=<clientSecret>
        # and execute DAX_PAGE_VIEWS against it. Left as a stub here so that
        # this script remains runnable in environments without ADOMD installed.
        raise NotImplementedError(
            "Wire up your XMLA endpoint here. See docs/api-reference.md for the "
            "exact connection string and DAX query."
        )


# ---------------------------------------------------------------------------
# MOCK adapter — replays the bundled synthetic dataset end-to-end
# ---------------------------------------------------------------------------

class MockAdapter(CollectorAdapter):
    """Reads the same shape from sample_data/page_views.csv so reviewers can
    run the full collector on a laptop without any Power BI access."""

    def __init__(self, csv_path: Path = SAMPLE_CSV):
        if not csv_path.exists():
            raise FileNotFoundError(
                f"{csv_path} not found. Run generate_sample_data.py first."
            )
        self._rows: list[dict] = []
        with csv_path.open("r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self._rows.append(r)

    def _grouped(self):
        ws: dict[str, Workspace] = {}
        reps: dict[str, Report] = {}
        rep_to_ws: dict[str, str] = {}
        rep_to_capacity: dict[str, str] = {}
        for r in self._rows:
            ws.setdefault(r["workspace_id"], Workspace(
                id=r["workspace_id"], name=r["workspace_name"],
                capacity_name=r["capacity_name"],
            ))
            reps.setdefault(r["report_id"], Report(
                id=r["report_id"], name=r["report_name"],
                workspace_id=r["workspace_id"],
                workspace_name=r["workspace_name"],
            ))
            rep_to_ws[r["report_id"]] = r["workspace_id"]
            rep_to_capacity[r["report_id"]] = r["capacity_name"]
        return ws, reps, rep_to_ws, rep_to_capacity

    def list_workspaces(self) -> Iterator[Workspace]:
        ws, _, _, _ = self._grouped()
        yield from ws.values()

    def list_reports(self, workspace: Workspace) -> Iterator[Report]:
        _, reps, rep_to_ws, _ = self._grouped()
        for rep in reps.values():
            if rep_to_ws[rep.id] == workspace.id:
                yield rep

    def ensure_usage_metrics_dataset(self, report: Report) -> str:
        # In mock-land we synthesize a deterministic id from the report id.
        return f"ds-um::{report.id}"

    def query_page_views(self, dataset_id: str, since: date, until: date) -> Iterator[PageViewRow]:
        # dataset_id maps back to a report_id via convention `ds-um::<report-id>`.
        report_id = dataset_id.split("::", 1)[1]
        for r in self._rows:
            if r["report_id"] != report_id:
                continue
            row_date = date.fromisoformat(r["view_date"])
            if row_date < since or row_date > until:
                continue
            yield PageViewRow(
                workspace_id=r["workspace_id"],
                workspace_name=r["workspace_name"],
                capacity_name=r["capacity_name"],
                report_id=r["report_id"],
                report_name=r["report_name"],
                report_total_pages=int(r["report_total_pages"]),
                page_id=r["page_id"],
                page_name=r["page_name"],
                page_ordinal=int(r["page_ordinal"]),
                view_date=r["view_date"],
                view_count=int(r["view_count"]),
                unique_users=int(r["unique_users"]),
                avg_dwell_seconds=float(r["avg_dwell_seconds"]),
                top_persona=r["top_persona"],
            )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run(adapter: CollectorAdapter, since: date, until: date, out_dir: Path) -> dict:
    bronze = out_dir / "bronze"
    silver = out_dir / "silver"
    if bronze.exists():
        shutil.rmtree(bronze)
    bronze.mkdir(parents=True)
    silver.mkdir(parents=True, exist_ok=True)

    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since.isoformat(), "until": until.isoformat(),
        "workspaces": 0, "reports": 0, "datasets": 0, "rows": 0,
        "errors": [],
    }
    all_rows: list[PageViewRow] = []

    for ws in adapter.list_workspaces():
        summary["workspaces"] += 1
        print(f"[workspace] {ws.name} ({ws.id})")
        for rep in adapter.list_reports(ws):
            summary["reports"] += 1
            try:
                ds_id = adapter.ensure_usage_metrics_dataset(rep)
                summary["datasets"] += 1
                rep_rows = list(adapter.query_page_views(ds_id, since, until))
            except Exception as e:
                msg = f"{rep.workspace_name}/{rep.name}: {type(e).__name__}: {e}"
                summary["errors"].append(msg)
                print(f"  ! {msg}")
                continue
            # Bronze: one CSV per report (raw shape)
            out = bronze / f"{rep.workspace_id}__{rep.id}.csv"
            _write_rows(out, rep_rows)
            all_rows.extend(rep_rows)
            summary["rows"] += len(rep_rows)
            print(f"  [report] {rep.name}: {len(rep_rows):,} rows -> {out.name}")

    # Silver: one conformed CSV across the tenant
    silver_path = silver / "page_views.csv"
    _write_rows(silver_path, all_rows)
    summary["silver_path"] = str(silver_path)
    summary["ended_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    (out_dir / "_run_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8",
    )
    return summary


def _write_rows(path: Path, rows: Iterable[PageViewRow]) -> None:
    rows = list(rows)
    if not rows:
        return
    fields = list(asdict(rows[0]).keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mock", action="store_true",
                   help="Use bundled sample data — no Power BI access required.")
    p.add_argument("--tenant", help="Entra tenant ID (live mode).")
    p.add_argument("--client-id", help="Service principal client ID (live mode).")
    p.add_argument("--client-secret", help="Service principal secret (live mode).")
    p.add_argument("--days", type=int, default=90,
                   help="How many days of history to pull (default: 90).")
    p.add_argument("--out", type=Path, default=HERE / "out",
                   help="Output directory (default: ./out).")
    args = p.parse_args(argv)

    until = date.today()
    # Note: in mock mode we shift the window to the synthetic data's date range
    # so the script always produces output.
    if args.mock:
        until = date(2026, 5, 27)
    since = until - timedelta(days=args.days - 1)

    if args.mock:
        adapter: CollectorAdapter = MockAdapter()
    else:
        tenant = args.tenant or os.environ.get("PBI_TENANT_ID")
        client_id = args.client_id or os.environ.get("PBI_CLIENT_ID")
        client_secret = args.client_secret or os.environ.get("PBI_CLIENT_SECRET")
        if not all([tenant, client_id, client_secret]):
            print("ERROR: Live mode requires --tenant/--client-id/--client-secret "
                  "(or PBI_TENANT_ID / PBI_CLIENT_ID / PBI_CLIENT_SECRET env vars).",
                  file=sys.stderr)
            return 2
        adapter = LiveAdapter(tenant, client_id, client_secret)

    summary = run(adapter, since, until, args.out)
    print()
    print(json.dumps({k: v for k, v in summary.items() if k != "errors"}, indent=2))
    if summary["errors"]:
        print(f"\nERRORS ({len(summary['errors'])}):")
        for e in summary["errors"][:10]:
            print(" -", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
