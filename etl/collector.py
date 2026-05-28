"""
Power BI Page-Level Telemetry Collector
=======================================

What this does
--------------
Builds a tenant-wide, page-level usage dataset by:

  1. Authenticating to the Power BI service as a service principal.
  2. Enumerating every report in every workspace via the Power BI Admin
     REST API.
  3. For each report, ensuring the auto-generated "Usage Metrics Report v2"
     dataset exists (POST `/admin/reports/{id}/usageMetrics`).
  4. Executing a parameterised DAX query against that dataset over the
     Power BI REST `executeQueries` endpoint (or XMLA via pyadomd as an
     optional advanced path) to pull per-page view counts and unique
     users.
  5. Writing one CSV per report into a date-partitioned `bronze/` layer,
     then emitting a conformed `silver/page_views.csv` and a
     `_run_summary.json` with the silver schema version.

All REST calls are wrapped in exponential-backoff retry that honors
HTTP 429 `Retry-After` and transient 5xx, so a multi-workspace run
on a tenant with hundreds of reports tolerates throttling without
crashing.

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

# Force UTF-8 stdout/stderr so em-dashes and other Unicode characters in
# log lines don't get mangled by Windows' default cp1252 console codepage.
# No-op on already-UTF-8 platforms.
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

try:
    import requests
except ImportError:
    requests = None  # only required for live mode

HERE = Path(__file__).parent
SAMPLE_CSV = HERE / "sample_data" / "page_views.csv"

# Schema version for the silver layer. Bump on breaking changes (column
# rename / drop / type change). Downstream MERGEs should assert on this
# in their landing notebook to avoid silent data corruption.
SILVER_SCHEMA_VERSION = "1.0.0"

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

    DAX execution path
    ------------------
    By default this adapter executes DAX via the Power BI REST endpoint:

        POST /v1.0/myorg/groups/{wsId}/datasets/{datasetId}/executeQueries

    It returns JSON. No native ADOMD DLLs are required, so the same
    collector runs identically from Linux Azure Functions, Fabric Spark,
    a macOS laptop, or a Windows scheduled task.

    For tenants that want true XMLA (e.g. for queries that exceed the
    REST endpoint's row limits, or for on-prem AS hybrid scenarios)
    install `pyadomd` + the ADOMD.NET retail client. The adapter
    auto-detects pyadomd and routes through it when available.
    """

    # Retry tuning ---------------------------------------------------------
    _RETRY_MAX_ATTEMPTS = 5
    _RETRY_BASE_SECONDS = 1.0
    _RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, tenant_id: str, client_id: str, client_secret: str):
        if requests is None:
            raise RuntimeError("requests not installed; run `pip install -r requirements.txt`")
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._workspace_for_dataset: dict[str, str] = {}
        # Optional: deferred import; only required for true XMLA queries via ADOMD.
        try:
            from pyadomd import Pyadomd  # noqa: F401
            self._xmla_available = True
        except Exception:
            self._xmla_available = False

    # ---- auth -----------------------------------------------------------------
    def _bearer(self) -> str:
        if self._token and time.time() < self._token_expires - 120:
            return self._token
        try:
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
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(
                f"Could not reach Entra login endpoint for tenant '{self.tenant_id}': "
                f"{type(exc).__name__}. Check the tenant ID and network connectivity."
            ) from exc
        if resp.status_code != 200:
            try:
                err = resp.json()
                desc = err.get("error_description", "").splitlines()[0]
                code = err.get("error", "auth_error")
            except Exception:
                desc, code = resp.text[:300], "auth_error"
            raise RuntimeError(
                f"Service principal sign-in failed ({code}, HTTP {resp.status_code}): {desc}\n"
                "       Verify --tenant, --client-id, --client-secret and that the SP has Power BI access."
            )
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + int(body.get("expires_in", 3600))
        return self._token

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._bearer()}"}
        if extra:
            h.update(extra)
        return h

    # ---- retry-wrapped HTTP --------------------------------------------------
    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        headers: dict | None = None,
        timeout: int = 60,
        allow_status: tuple[int, ...] = (),
    ) -> "requests.Response":
        """HTTP with exponential backoff on 429/5xx that honors `Retry-After`.

        `allow_status` lists status codes that should be returned to the
        caller without raising — useful for 409 / 202 LRO / 404-as-info.
        """
        last_exc: Exception | None = None
        for attempt in range(self._RETRY_MAX_ATTEMPTS):
            try:
                r = requests.request(
                    method, url,
                    params=params,
                    json=json_body,
                    headers=self._headers(headers),
                    timeout=timeout,
                )
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                sleep = self._RETRY_BASE_SECONDS * (2 ** attempt)
                time.sleep(sleep)
                continue

            if r.status_code in allow_status or r.status_code < 400:
                return r

            if r.status_code in self._RETRY_STATUS and attempt < self._RETRY_MAX_ATTEMPTS - 1:
                # Honor Retry-After (seconds or HTTP date). Fall back to exp backoff.
                retry_after = r.headers.get("Retry-After")
                sleep: float
                if retry_after and retry_after.isdigit():
                    sleep = float(retry_after)
                else:
                    sleep = self._RETRY_BASE_SECONDS * (2 ** attempt)
                print(
                    f"  [retry] {method} {url} -> HTTP {r.status_code}; "
                    f"sleeping {sleep:.1f}s (attempt {attempt + 1}/{self._RETRY_MAX_ATTEMPTS})",
                    file=sys.stderr,
                )
                time.sleep(sleep)
                continue
            # Non-retryable error code
            r.raise_for_status()
            return r  # pragma: no cover  (raise_for_status raised)

        if last_exc:
            raise RuntimeError(
                f"{method} {url} failed after {self._RETRY_MAX_ATTEMPTS} attempts: "
                f"{type(last_exc).__name__}: {last_exc}"
            ) from last_exc
        raise RuntimeError(
            f"{method} {url} kept returning a retryable error after "
            f"{self._RETRY_MAX_ATTEMPTS} attempts."
        )

    # ---- workspaces / reports -------------------------------------------------
    def list_workspaces(self) -> Iterator[Workspace]:
        # Admin groups API; supports paging via $top/$skip.
        skip = 0
        page = 100
        while True:
            r = self._request(
                "GET",
                f"{POWERBI_API}/admin/groups",
                params={"$top": page, "$skip": skip,
                        "$expand": "users",
                        "$filter": "type eq 'Workspace' and state eq 'Active'"},
            )
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
        r = self._request(
            "GET",
            f"{POWERBI_API}/admin/groups/{workspace.id}/reports",
        )
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
        """Ensure the report's Usage Metrics v2 dataset exists and return its id.

        `POST /admin/reports/{id}/usageMetrics` is a long-running operation
        on first call: HTTP 202 + `Location` header to poll until terminal
        status. Subsequent calls return 200 with the datasetId directly,
        or 409 (already exists) — in which case we look up the dataset by
        the well-known "Usage Metrics Report" naming convention.
        """
        r = self._request(
            "POST",
            f"{POWERBI_API}/admin/reports/{report.id}/usageMetrics",
            timeout=120,
            allow_status=(202, 409),
        )

        ds_id = ""
        if r.status_code in (200, 201):
            ds_id = self._parse_dataset_id(r)
        elif r.status_code == 202:
            ds_id = self._poll_lro_for_dataset_id(r, report)
        elif r.status_code == 409:
            ds_id = self._lookup_usage_metrics_dataset(report)

        if ds_id:
            # Cache the dataset -> workspace mapping for the executeQueries call.
            self._workspace_for_dataset[ds_id] = report.workspace_id
        return ds_id

    def _parse_dataset_id(self, response) -> str:
        try:
            return response.json().get("datasetId") or response.json().get("id") or ""
        except ValueError:
            return ""

    def _poll_lro_for_dataset_id(self, initial_response, report: Report) -> str:
        location = initial_response.headers.get("Location") or initial_response.headers.get("location")
        if not location:
            return self._lookup_usage_metrics_dataset(report)
        for attempt in range(20):
            time.sleep(min(2 ** attempt, 30))
            r = self._request("GET", location, allow_status=(202,))
            if r.status_code == 200:
                ds_id = self._parse_dataset_id(r)
                if ds_id:
                    return ds_id
                return self._lookup_usage_metrics_dataset(report)
            # 202 = still working
        # LRO didn't resolve in time; fall back to a lookup.
        return self._lookup_usage_metrics_dataset(report)

    def _lookup_usage_metrics_dataset(self, report: Report) -> str:
        r = self._request(
            "GET",
            f"{POWERBI_API}/admin/groups/{report.workspace_id}/datasets",
        )
        for d in r.json().get("value", []):
            # Auto-generated names look like:
            #   "Usage Metrics Report" (legacy) or
            #   "Usage Metrics Report v2 - <report-name>" (current)
            name = d.get("name") or ""
            if name.startswith("Usage Metrics Report"):
                return d["id"]
        return ""

    def query_page_views(self, dataset_id: str, since: date, until: date) -> Iterator[PageViewRow]:
        """Execute the page-views DAX query and yield typed rows.

        Default path: POST /datasets/{id}/executeQueries (REST, JSON in/out).
        Advanced path: if pyadomd + ADOMD.NET are available AND
        `PBI_USE_PYADOMD=1` is set, use a true XMLA connection instead.

        Both paths produce the same `PageViewRow` shape downstream.
        """
        if os.environ.get("PBI_USE_PYADOMD") and self._xmla_available:
            yield from self._query_via_pyadomd(dataset_id, since, until)
            return

        ws_id = self._workspace_for_dataset.get(dataset_id)
        if not ws_id:
            # Fallback: scan the tenant for the dataset. Expensive — should rarely fire.
            for ws in self.list_workspaces():
                for d in self._request(
                    "GET", f"{POWERBI_API}/admin/groups/{ws.id}/datasets",
                ).json().get("value", []):
                    if d.get("id") == dataset_id:
                        ws_id = ws.id
                        self._workspace_for_dataset[dataset_id] = ws_id
                        break
                if ws_id:
                    break
        if not ws_id:
            raise RuntimeError(
                f"Could not resolve workspace for dataset {dataset_id} — "
                "ensure_usage_metrics_dataset must be called first."
            )

        dax = self._dax_with_date_filter(since, until)
        r = self._request(
            "POST",
            f"{POWERBI_API}/groups/{ws_id}/datasets/{dataset_id}/executeQueries",
            json_body={
                "queries": [{"query": dax}],
                "serializerSettings": {"includeNulls": False},
            },
            headers={"Content-Type": "application/json"},
            timeout=120,
        )
        body = r.json()
        tables = body.get("results", [{}])[0].get("tables", [{}])
        for row in tables[0].get("rows", []):
            yield from self._coerce_executequeries_row(row, dataset_id)

    def _dax_with_date_filter(self, since: date, until: date) -> str:
        """Inject a date window into the base DAX so we don't haul 90 days
        every run when the caller asked for 1."""
        return (
            "EVALUATE\n"
            "FILTER(\n"
            "  SUMMARIZECOLUMNS(\n"
            "    'Report page views'[Report Id],\n"
            "    'Report page views'[Report page name],\n"
            "    'Report page views'[Date],\n"
            "    \"Views\",          SUM('Report page views'[Views]),\n"
            "    \"UniqueUsers\",    DISTINCTCOUNT('Report page views'[User]),\n"
            "    \"AvgDwellSeconds\",AVERAGE('Report page views'[Average view time])\n"
            "  ),\n"
            f"  'Report page views'[Date] >= DATE({since.year},{since.month},{since.day}) &&\n"
            f"  'Report page views'[Date] <= DATE({until.year},{until.month},{until.day})\n"
            ")\n"
            "ORDER BY 'Report page views'[Date]\n"
        )

    def _coerce_executequeries_row(self, raw: dict, dataset_id: str) -> Iterator[PageViewRow]:
        """`executeQueries` returns rows keyed by the DAX column expressions —
        e.g. `'Report page views'[Report Id]`, `[Views]`. Map to PageViewRow.
        Caller is expected to enrich workspace/report/capacity metadata
        from the earlier enumeration step."""
        def g(*keys: str, default=None):
            for k in keys:
                if k in raw and raw[k] is not None:
                    return raw[k]
            return default

        view_date = g("'Report page views'[Date]", "[Date]") or ""
        # API returns ISO-with-time; normalize to YYYY-MM-DD.
        view_date = str(view_date)[:10]

        yield PageViewRow(
            workspace_id="",
            workspace_name="",
            capacity_name="",
            report_id=g("'Report page views'[Report Id]", default="") or "",
            report_name="",
            report_total_pages=0,
            page_id=g("'Report page views'[Report page name]", default="") or "",
            page_name=g("'Report page views'[Report page name]", default="") or "",
            page_ordinal=0,
            view_date=view_date,
            view_count=int(g("[Views]", default=0) or 0),
            unique_users=int(g("[UniqueUsers]", default=0) or 0),
            avg_dwell_seconds=float(g("[AvgDwellSeconds]", default=0.0) or 0.0),
            top_persona="",
        )

    def _query_via_pyadomd(self, dataset_id: str, since: date, until: date) -> Iterator[PageViewRow]:
        from pyadomd import Pyadomd  # type: ignore
        ws_id = self._workspace_for_dataset.get(dataset_id, "")
        # Lookup the workspace's *name* — XMLA wants the friendly Data Source.
        ws_name = ""
        for ws in self.list_workspaces():
            if ws.id == ws_id:
                ws_name = ws.name
                break
        if not ws_name:
            raise RuntimeError(f"Could not find workspace name for id {ws_id}")
        # Find the dataset name (Initial Catalog).
        ds_name = ""
        for d in self._request(
            "GET", f"{POWERBI_API}/admin/groups/{ws_id}/datasets",
        ).json().get("value", []):
            if d.get("id") == dataset_id:
                ds_name = d.get("name") or ""
                break
        conn = (
            f"Provider=MSOLAP;"
            f"Data Source=powerbi://api.powerbi.com/v1.0/myorg/{ws_name};"
            f"Initial Catalog={ds_name};"
            f"User ID=app:{self.client_id}@{self.tenant_id};"
            f"Password={self.client_secret};"
        )
        dax = self._dax_with_date_filter(since, until)
        with Pyadomd(conn).cursor() as cur:
            cur.execute(dax)
            cols = [c.name for c in cur.description]
            for row in cur.fetchall():
                rec = dict(zip(cols, row))
                yield from self._coerce_executequeries_row(rec, dataset_id)


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

    @classmethod
    def max_csv_date(cls, csv_path: Path = SAMPLE_CSV) -> date:
        """Latest view_date in the bundled sample CSV. Used by `main()` so
        --mock always exercises a populated date window even years from now."""
        if not csv_path.exists():
            return date.today()
        latest = date(1970, 1, 1)
        with csv_path.open("r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    d = date.fromisoformat(r["view_date"])
                    if d > latest:
                        latest = d
                except (KeyError, ValueError):
                    continue
        return latest

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

def _rmtree_resilient(path: Path, attempts: int = 5) -> None:
    """shutil.rmtree fails with WinError 5 inside OneDrive-synced folders
    when the sync client briefly holds a handle. Retry with backoff, and as
    a last resort empty the directory file-by-file."""
    for i in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            time.sleep(0.2 * (i + 1))
    # Fallback: delete contents but leave the directory itself.
    for child in path.rglob("*"):
        if child.is_file():
            try:
                child.unlink()
            except PermissionError:
                pass


def run(adapter: CollectorAdapter, since: date, until: date, out_dir: Path) -> dict:
    """Drive the adapter: enumerate, collect, partition bronze by date,
    emit a conformed silver CSV and a `_run_summary.json` with the
    silver schema version.

    Bronze layout:
        bronze/dt=YYYY-MM-DD/{wsId}__{reportId}.csv

    Silver layout:
        silver/page_views.csv   (a `# silver_schema_version=...` comment line precedes the header)
    """
    bronze = out_dir / "bronze"
    silver = out_dir / "silver"
    bronze.mkdir(parents=True, exist_ok=True)
    silver.mkdir(parents=True, exist_ok=True)

    run_dt = until.isoformat()
    bronze_run = bronze / f"dt={run_dt}"
    # Inside a daily partition we *do* want a clean slate for that day's
    # files, but never delete other days' partitions.
    if bronze_run.exists():
        _rmtree_resilient(bronze_run)
    bronze_run.mkdir(parents=True, exist_ok=True)

    summary = {
        "schema_version": SILVER_SCHEMA_VERSION,
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
                raw_rows = list(adapter.query_page_views(ds_id, since, until))
            except Exception as e:
                msg = f"{rep.workspace_name}/{rep.name}: {type(e).__name__}: {e}"
                summary["errors"].append(msg)
                print(f"  ! {msg}")
                continue
            # Enrich any rows missing workspace/report metadata (LiveAdapter
            # returns bare DAX rows; MockAdapter pre-fills them).
            rep_rows = [_enrich_row(r, ws, rep) for r in raw_rows]
            out = bronze_run / f"{rep.workspace_id}__{rep.id}.csv"
            _write_rows(out, rep_rows)
            all_rows.extend(rep_rows)
            summary["rows"] += len(rep_rows)
            print(f"  [report] {rep.name}: {len(rep_rows):,} rows -> {out.parent.name}/{out.name}")

    # Silver: one conformed CSV across the tenant, prefaced with the schema-version
    # comment so downstream MERGEs can assert compatibility.
    silver_path = silver / "page_views.csv"
    _write_rows(silver_path, all_rows, schema_version_comment=True)
    summary["silver_path"] = str(silver_path)
    summary["bronze_partition"] = str(bronze_run)
    summary["ended_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    (out_dir / "_run_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8",
    )
    return summary


def _enrich_row(row: PageViewRow, ws: Workspace, rep: Report) -> PageViewRow:
    """Patch in workspace/report context for rows that came from the live
    DAX path with bare identifiers. MockAdapter rows are already complete."""
    if row.workspace_id and row.workspace_name and row.report_name:
        return row
    return PageViewRow(
        workspace_id=row.workspace_id or ws.id,
        workspace_name=row.workspace_name or ws.name,
        capacity_name=row.capacity_name or (ws.capacity_name or ""),
        report_id=row.report_id or rep.id,
        report_name=row.report_name or rep.name,
        report_total_pages=row.report_total_pages,
        page_id=row.page_id,
        page_name=row.page_name,
        page_ordinal=row.page_ordinal,
        view_date=row.view_date,
        view_count=row.view_count,
        unique_users=row.unique_users,
        avg_dwell_seconds=row.avg_dwell_seconds,
        top_persona=row.top_persona,
    )


def _write_rows(path: Path, rows: Iterable[PageViewRow], *, schema_version_comment: bool = False) -> None:
    rows = list(rows)
    if not rows:
        return
    fields = list(asdict(rows[0]).keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        if schema_version_comment:
            # A leading `#` comment line is ignored by pandas/Spark when
            # `comment='#'` is set, and is a no-op for the bundled
            # dashboard aggregator (it uses csv.DictReader which skips
            # rows where the first field is empty / unknown header).
            # The presence of this line lets downstream MERGEs verify
            # they are joining compatible schemas.
            f.write(f"# silver_schema_version={SILVER_SCHEMA_VERSION}\n")
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


CLI_EPILOG = """\
Examples
--------
  python collector.py --mock
      Run end-to-end against the bundled synthetic sample data. No tenant
      access required. Produces out/bronze/*.csv and out/silver/page_views.csv.

  python collector.py --tenant <tenant-id> \\
                      --client-id <sp-client-id> \\
                      --client-secret <sp-secret>
      Run against a real Power BI tenant. The service principal must have
      Fabric Administrator (or be a member of the security group enabled
      for Read-only admin APIs) AND have access to the XMLA endpoint.

  python collector.py --mock --days 30 --out C:\\tmp\\pbi
      Pull the last 30 days into a custom output directory.

Environment variables (live mode, alternative to CLI flags)
-----------------------------------------------------------
  PBI_TENANT_ID           Entra tenant ID
  PBI_CLIENT_ID           Service principal app ID
  PBI_CLIENT_SECRET       Service principal secret (use Key Vault in prod)
  PBI_XMLA_WORKSPACE      Optional: workspace to use as default XMLA endpoint
  PBI_OUTPUT_DIR          Where to write bronze/ and silver/ (defaults to ./out)

See docs/api-reference.md for the REST endpoints and DAX query, and
docs/deployment-guide.md for end-to-end deployment in a Fabric tenant.
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="collector.py",
        description="Power BI page-level telemetry collector — pulls per-page view "
                    "counts from every report in every workspace via the Admin REST API "
                    "+ XMLA endpoint, and writes a conformed bronze/silver layer.",
        epilog=CLI_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--mock", action="store_true",
                   help="Use bundled sample data - no Power BI access required.")
    p.add_argument("--tenant", help="Entra tenant ID (live mode).")
    p.add_argument("--client-id", help="Service principal client ID (live mode).")
    p.add_argument("--client-secret", help="Service principal secret (live mode).")
    p.add_argument("--days", type=int, default=90,
                   help="How many days of history to pull (default: 90).")
    p.add_argument("--out", type=Path, default=None,
                   help="Output directory (default: ./out, overridable via PBI_OUTPUT_DIR).")
    args = p.parse_args(argv)

    out_dir = args.out or Path(os.environ.get("PBI_OUTPUT_DIR") or (HERE / "out"))

    until = date.today()
    # In mock mode shift the window to the synthetic data's actual date
    # range so the script always produces output, even years after the
    # sample CSV was generated.
    if args.mock:
        until = MockAdapter.max_csv_date()
    since = until - timedelta(days=args.days - 1)

    if args.mock:
        adapter: CollectorAdapter = MockAdapter()
    else:
        tenant = args.tenant or os.environ.get("PBI_TENANT_ID")
        client_id = args.client_id or os.environ.get("PBI_CLIENT_ID")
        client_secret = args.client_secret or os.environ.get("PBI_CLIENT_SECRET")
        if not all([tenant, client_id, client_secret]):
            print("ERROR: Live mode requires --tenant/--client-id/--client-secret "
                  "(or PBI_TENANT_ID / PBI_CLIENT_ID / PBI_CLIENT_SECRET env vars).\n"
                  "       Run `python collector.py --mock` for an offline demo.",
                  file=sys.stderr)
            return 2
        if requests is None:
            print("ERROR: The 'requests' package is required for live mode.\n"
                  "       Install it with: pip install -r requirements.txt",
                  file=sys.stderr)
            return 2
        adapter = LiveAdapter(tenant, client_id, client_secret)

    try:
        summary = run(adapter, since, until, out_dir)
    except Exception as exc:  # surface friendly error, full trace on --debug
        print(f"\nERROR: collector failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if os.environ.get("PBI_DEBUG"):
            raise
        print("       Set PBI_DEBUG=1 to see the full traceback.", file=sys.stderr)
        return 1

    print()
    print(json.dumps({k: v for k, v in summary.items() if k != "errors"}, indent=2))
    if summary["errors"]:
        print(f"\nERRORS ({len(summary['errors'])}):")
        for e in summary["errors"][:10]:
            print(" -", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
