"""
Power BI Page-Level Telemetry Collector
=======================================

What this does
--------------
Builds a tenant-wide, page-level usage dataset by:

  1. Authenticating to the Power BI service as a service principal.
  2. Enumerating every workspace via the Power BI Admin REST API.
  3. For each workspace, looking up the auto-generated, per-workspace
     "Usage Metrics Report" semantic model that the **Modern Usage
     Metrics (preview)** feature creates the first time any admin /
     contributor clicks "View usage metrics report" on any report in
     that workspace. See:
     https://learn.microsoft.com/en-us/power-bi/collaborate-share/service-modern-usage-metrics
  4. For each report in that workspace, executing a parameterised DAX
     query against that semantic model over the Power BI REST
     `executeQueries` endpoint (or XMLA via pyadomd as an optional
     advanced path) to pull per-page view counts and unique users.
  5. Writing one CSV per report into a date-partitioned `bronze/` layer,
     then emitting a conformed `silver/page_views.csv` and a
     `_run_summary.json` with the silver schema version and a list of
     any workspaces that need to be bootstrapped.

All REST calls are wrapped in exponential-backoff retry that honors
HTTP 429 `Retry-After` and transient 5xx, so a multi-workspace run
on a tenant with hundreds of reports tolerates throttling without
crashing.

The same script in `--mock` mode does NOT call any Microsoft service —
it loads the bundled synthetic sample data so reviewers can run end-to-end
on a laptop with no tenant access.

Why this shape and not "/admin/reports/{id}/usageMetrics"
---------------------------------------------------------
There is **no public REST API to provision** a Usage Metrics dataset.
The "View usage metrics report" button in app.powerbi.com is a portal-
internal action — confirmed by Power BI Product Management (David Browne,
in the CY2026 HLS Fabric Roundtable): *"We don't have an API, but it
builds a semantic model you can access."*

What we do have, post-Modern Usage Metrics preview, is **one semantic
model per workspace** (named "Usage Metrics Report") that captures
page-level activity for every report in the workspace, refreshed
daily. Once any admin / contributor bootstraps a workspace by clicking
"View usage metrics report" once on any report in it, the dataset
exists forever and accumulates data for all reports — including new
reports added later. This collector treats that one-time-per-workspace
click as a deployment prerequisite (logged + summarised) and reads the
resulting dataset via REST.

The Admin Scanner / Activity Log path was also considered and rejected:
its only report event is `ViewReport` (no `ViewReportPage` / page name
field), which is exactly the report-level-only gap the customer asked
us to close.

Run modes
---------
  python collector.py --mock                  # uses bundled sample data
  python collector.py --tenant <tenant-id>    # live; requires service principal env vars

Environment variables (live mode)
---------------------------------
  PBI_TENANT_ID           Entra tenant ID
  PBI_CLIENT_ID           Service principal app ID
  PBI_CLIENT_SECRET       Service principal secret (use Key Vault in prod)
  PBI_USAGE_DATASET_NAME  Override the dataset name to look up
                          (default: "Usage Metrics Report")
  PBI_USE_PYADOMD         Set to "1" to route DAX via XMLA + ADOMD.NET
                          instead of REST (Windows-only, advanced).
  PBI_OUTPUT_DIR          Where to write bronze/ and silver/ (defaults to ./out)
  PBI_MOCK                Set to "1" to use bundled sample data instead of
                          a live Power BI tenant (env-var equivalent of --mock).
  PBI_SAMPLE_CSV          Override the location of the bundled sample CSV
                          (useful when collector.py is vendored away from
                          its sample_data/ folder, e.g. in containers).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

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


def _resolve_sample_csv(here: Path | None = None) -> Path:
    """Find the bundled sample CSV. The collector may be deployed to an
    Azure Function (where collector.py gets copied alongside function_app.py
    and sample_data/ is left behind), or installed from source, or vendored
    by a Fabric notebook to /tmp. Search in this priority order:

      1. PBI_SAMPLE_CSV env var (full path; explicit override)
      2. Adjacent: <collector.py dir>/sample_data/page_views.csv
      3. Repo root: <collector.py dir>/../etl/sample_data/page_views.csv
      4. Parent: <collector.py dir>/../sample_data/page_views.csv
      5. Two-up: <collector.py dir>/../../etl/sample_data/page_views.csv
         (for deploy/<option>/collector.py layouts)

    `here` defaults to the directory containing this module and may be
    overridden by tests to simulate a vendored layout. Returns the first
    match. If none exist, returns the canonical adjacent path so
    MockAdapter's existence check still produces a helpful error
    message that points to a sensible location.
    """
    base = here if here is not None else HERE
    candidates = [
        Path(os.environ["PBI_SAMPLE_CSV"]) if os.environ.get("PBI_SAMPLE_CSV") else None,
        base / "sample_data" / "page_views.csv",
        base.parent / "etl" / "sample_data" / "page_views.csv",
        base.parent / "sample_data" / "page_views.csv",
        # e.g. deploy/azure-function/collector.py -> ../../etl/sample_data/
        base.parent.parent / "etl" / "sample_data" / "page_views.csv",
    ]
    for c in candidates:
        if c and c.exists():
            return c
    return base / "sample_data" / "page_views.csv"


SAMPLE_CSV = _resolve_sample_csv()

# Schema version for the silver layer. Bump on breaking changes (column
# rename / drop / type change). Downstream MERGEs should assert on this
# in their landing notebook to avoid silent data corruption.
SILVER_SCHEMA_VERSION = "1.2.0"

# ---------------------------------------------------------------------------
# REST API surface
# ---------------------------------------------------------------------------

POWERBI_API = "https://api.powerbi.com/v1.0/myorg"
SCOPE = "https://analysis.windows.net/powerbi/api/.default"

# Default name of the per-workspace Modern Usage Metrics semantic model.
# Overridable via the PBI_USAGE_DATASET_NAME env var if a tenant has
# renamed it or is still on a legacy variant like "Usage Metrics Report v2".
USAGE_METRICS_DATASET_NAME = "Usage Metrics Report"

# Single DAX query that we run against each per-workspace Usage Metrics
# semantic model. The Modern Usage Metrics model exposes a
# `Report page views` table whose columns include `Report Id`,
# `Report page name`, `Date`, `Views`, `User`, and `Average view time`.
# We filter to a single report id at the source and aggregate
# server-side so we transfer only the aggregated rows.
DAX_PAGE_VIEWS_TEMPLATE = """
EVALUATE
CALCULATETABLE(
    SUMMARIZECOLUMNS(
        'Report page views'[Report Id],
        'Report page views'[Report page name],
        'Report page views'[Date],
        "Views",          SUM('Report page views'[Views]),
        "UniqueUsers",    DISTINCTCOUNT('Report page views'[User]),
        "AvgDwellSeconds",AVERAGE('Report page views'[Average view time])
    ),
    'Report page views'[Date] >= DATE({since_y},{since_m},{since_d}),
    'Report page views'[Date] <= DATE({until_y},{until_m},{until_d}),
    'Report page views'[Report Id] = "{report_id}"
)
ORDER BY 'Report page views'[Date]
"""

# Report-level (no page dimension) DAX. The Modern Usage Metrics model
# exposes a `Report views` table that's distinct from `Report page views`
# — same dataset, coarser grain. Used to populate silver/report_views.csv
# and to power session-level metrics like avg_session_seconds that page-
# level data can't compute (a 5-page session looks like 5 page rows in
# 'Report page views' but is one row in 'Report views').
DAX_REPORT_VIEWS_TEMPLATE = """
EVALUATE
CALCULATETABLE(
    SUMMARIZECOLUMNS(
        'Report views'[Report Id],
        'Report views'[Date],
        "Views",            SUM('Report views'[Views]),
        "UniqueUsers",      DISTINCTCOUNT('Report views'[User]),
        "AvgSessionSeconds",AVERAGE('Report views'[Average view time])
    ),
    'Report views'[Date] >= DATE({since_y},{since_m},{since_d}),
    'Report views'[Date] <= DATE({until_y},{until_m},{until_d}),
    'Report views'[Report Id] = "{report_id}"
)
ORDER BY 'Report views'[Date]
"""

# Per-user grain. UPN comes back raw from DAX; we SHA-256-hash it before
# it lands in silver so PII never persists. We also surface the count of
# distinct pages that user touched per day — useful for "who is doing
# deep exploration vs. who's just landing on the cover page" analyses.
DAX_USER_VIEWS_TEMPLATE = """
EVALUATE
CALCULATETABLE(
    SUMMARIZECOLUMNS(
        'Report page views'[Report Id],
        'Report page views'[User],
        'Report page views'[Date],
        "Views",               SUM('Report page views'[Views]),
        "DistinctPagesViewed", DISTINCTCOUNT('Report page views'[Report page name])
    ),
    'Report page views'[Date] >= DATE({since_y},{since_m},{since_d}),
    'Report page views'[Date] <= DATE({until_y},{until_m},{until_d}),
    'Report page views'[Report Id] = "{report_id}"
)
ORDER BY 'Report page views'[Date]
"""

# UPN hash truncation. SHA-256 first 16 hex chars = 64 bits of collision
# resistance, which is overkill for tenant-scale user counts (a tenant
# with 1M users has p_collision ~= 2.7e-8). Brute-forcing a 16-char hash
# back to an arbitrary UPN is infeasible. Downstream consumers who NEED
# to map a hash back to a known person can re-hash that known UPN and
# look up — but cannot enumerate UPNs from the silver layer.
def _hash_upn(upn: str | None) -> str:
    if not upn:
        return ""
    return hashlib.sha256(upn.strip().lower().encode("utf-8")).hexdigest()[:16]

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


@dataclass
class PageCatalogRow:
    """One row per (workspace_id, report_id, page_id). The full catalog of
    pages that EXIST in a report at collection time, sourced from
    `GET /admin/groups/{ws}/reports/{rep}/pages` (a supported GA endpoint).
    LEFT JOIN this with PageViewRow on (workspace_id, report_id, page_id)
    to find pages with zero views in a window = unused pages.
    """
    workspace_id: str
    workspace_name: str
    report_id: str
    report_name: str
    page_id: str
    page_name: str
    page_ordinal: int
    catalog_pulled_at: str  # ISO timestamp, run-level


@dataclass
class ReportViewRow:
    """Report-level aggregate (no page dimension). One row per
    (workspace_id, report_id, view_date). Sourced from the same Modern
    Usage Metrics semantic model as PageViewRow, but querying the
    report-grain table directly so session-level metrics like
    `avg_session_seconds` aren't biased by page-hop counts."""
    workspace_id: str
    workspace_name: str
    capacity_name: str
    report_id: str
    report_name: str
    view_date: str
    view_count: int
    unique_users: int
    avg_session_seconds: float


@dataclass
class UserViewRow:
    """Per-user activity, one row per (workspace_id, report_id, user_id_hash, view_date).
    `user_id_hash` is a deterministic SHA-256 of the user's UPN truncated to 16
    hex chars — keeps the data joinable across runs and across tables, but PII
    never lands in silver. Original UPN can be recovered downstream only by
    re-hashing a known UPN; brute-forcing 16-char SHA-256 is not feasible."""
    workspace_id: str
    workspace_name: str
    report_id: str
    report_name: str
    user_id_hash: str
    view_date: str
    view_count: int
    distinct_pages_viewed: int


@dataclass
class UnusedPageRow:
    """v0.3.1 — one row per page that EXISTS in `page_catalog` but has ZERO
    matching rows in `page_views` for the collection window. Computed by the
    collector via in-memory LEFT JOIN after both feeds finish. Materialized
    so consumers can list/sort/filter by report_name without writing DAX or
    a join in Power Query.

    Shape mirrors PageCatalogRow exactly so a `UNION ALL` with the catalog
    is trivial if you ever want a single 'is_unused' flag on the catalog
    instead of two tables. `catalog_pulled_at` is copied from the catalog
    row so consumers can see the as-of timestamp without joining back.
    """
    workspace_id: str
    workspace_name: str
    report_id: str
    report_name: str
    page_id: str
    page_name: str
    page_ordinal: int
    catalog_pulled_at: str  # copied from the originating PageCatalogRow

# ---------------------------------------------------------------------------
# Adapter interface — same contract for live and mock
# ---------------------------------------------------------------------------

class CollectorAdapter:
    def list_workspaces(self) -> Iterator[Workspace]:
        raise NotImplementedError

    def list_reports(self, workspace: Workspace) -> Iterator[Report]:
        raise NotImplementedError

    def ensure_usage_metrics_dataset(self, report: Report) -> str:
        """Return the dataset id of the Usage Metrics semantic model that
        covers `report`. With the Modern Usage Metrics (preview) feature
        this is a per-workspace dataset shared by every report in the
        workspace; with the legacy variant it was per-report. Either way,
        an empty string is a signal that the dataset doesn't exist yet
        and the workspace needs a one-time bootstrap click in the portal
        ("..." → "View usage metrics report" on any report in the workspace).
        """
        raise NotImplementedError

    def query_page_views(
        self,
        dataset_id: str,
        since: date,
        until: date,
        report_id: str | None = None,
    ) -> Iterator[PageViewRow]:
        raise NotImplementedError

    def list_report_pages(self, report: Report) -> Iterator[PageCatalogRow]:
        """Return the full catalog of pages that exist in `report` at
        collection time. Source: `GET /admin/groups/{ws}/reports/{rep}/pages`
        — a supported, documented Power BI REST endpoint (not preview).
        Required to detect unused pages (LEFT JOIN with page_views).

        Default implementation yields nothing so third-party adapters
        that only implement page_views (the v0.2.x contract) keep working;
        the silver/page_catalog.csv will just be empty in that case.
        """
        return iter(())

    def query_report_views(
        self,
        dataset_id: str,
        since: date,
        until: date,
        report_id: str | None = None,
    ) -> Iterator[ReportViewRow]:
        """Report-level (no page dimension) views from the same Modern
        Usage Metrics semantic model as `query_page_views`. Equivalent to
        the top-level cards in the auto-generated per-report Usage Metrics
        report.

        Default impl: no rows (v0.2.x back-compat)."""
        return iter(())

    def query_user_views(
        self,
        dataset_id: str,
        since: date,
        until: date,
        report_id: str | None = None,
    ) -> Iterator[UserViewRow]:
        """Per-user views (hashed UPN). One row per user/report/date.
        Powers the User Analytics dashboard page.

        Default impl: no rows (v0.2.x back-compat)."""
        return iter(())


# ---------------------------------------------------------------------------
# LIVE adapter — Power BI Admin REST + XMLA read endpoint
# ---------------------------------------------------------------------------

class LiveAdapter(CollectorAdapter):
    """Real implementation. Requires a service principal with:
       * Tenant setting `Service principals can use Power BI APIs` ON
       * `Fabric administrator` (or member of the SP security group
         enabled for read-only admin APIs)
       * Read / member access to every workspace whose UM dataset will
         be queried (the SP that calls `executeQueries` must be a
         workspace contributor or admin on the workspace the dataset
         lives in)
       * Premium / Fabric / PPU capacity for the workspaces whose UM
         datasets are read (executeQueries requires it)

    Prerequisite per workspace
    --------------------------
    The Modern Usage Metrics (preview) dataset is created on first
    portal click of "View usage metrics report" on any report in the
    workspace. After that, Power BI accumulates per-page data for
    *every* report in the workspace into a single semantic model
    named "Usage Metrics Report", refreshed daily. This collector
    reads that one model per workspace.

    Workspaces that have never been bootstrapped are skipped with a
    clear warning. The driver's run summary lists every skipped
    workspace so an admin can do the one-time bootstrap in bulk.

    DAX execution path
    ------------------
    By default this adapter executes DAX via the Power BI REST endpoint:

        POST /v1.0/myorg/groups/{wsId}/datasets/{datasetId}/executeQueries

    It returns JSON. No native ADOMD DLLs are required, so the same
    collector runs identically from Linux Azure Functions, Fabric Spark,
    a macOS laptop, or a Windows scheduled task.

    For tenants that want true XMLA (e.g. for queries that exceed the
    REST endpoint's row / time limits) install `pyadomd` + the
    ADOMD.NET retail client and set `PBI_USE_PYADOMD=1`.
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
        # workspace_id -> Usage Metrics dataset id ("" means "not bootstrapped, do not retry")
        self._usage_metrics_dataset_per_workspace: dict[str, str | None] = {}
        # dataset_id -> workspace_id, for the executeQueries call
        self._workspace_for_dataset: dict[str, str] = {}
        # workspace_id -> Workspace, for pyadomd path that needs the friendly name
        self._workspaces_by_id: dict[str, Workspace] = {}
        # dataset_id -> dataset name, for pyadomd path that needs Initial Catalog
        self._dataset_name_by_id: dict[str, str] = {}
        # Track which workspaces we warned about — surface in run summary.
        self.workspaces_not_bootstrapped: list[str] = []
        # Override the UM dataset name via env var (e.g. legacy "Usage Metrics Report v2").
        self._usage_dataset_name = os.environ.get(
            "PBI_USAGE_DATASET_NAME", USAGE_METRICS_DATASET_NAME
        )
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
    ) -> requests.Response:
        """HTTP with exponential backoff on 429/5xx that honors `Retry-After`.

        `allow_status` lists status codes that should be returned to the
        caller without raising — useful for 404-as-info."""
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
                ws = Workspace(
                    id=w["id"],
                    name=w["name"],
                    capacity_id=w.get("capacityId"),
                )
                self._workspaces_by_id[ws.id] = ws
                yield ws
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
        """Return the workspace's Usage Metrics dataset id, cached per
        workspace. The dataset is auto-provisioned by Power BI on the
        FIRST "View usage metrics report" click in a workspace. There is
        no public REST API to create it (confirmed by Power BI PM
        David Browne) — so if a workspace has not been bootstrapped, we
        log a warning, surface it in the run summary, and skip the
        report.
        """
        ws_id = report.workspace_id
        if ws_id in self._usage_metrics_dataset_per_workspace:
            return self._usage_metrics_dataset_per_workspace[ws_id] or ""

        ds_id, ds_name = self._lookup_usage_metrics_dataset_for_workspace(ws_id)
        self._usage_metrics_dataset_per_workspace[ws_id] = ds_id or None
        if ds_id:
            self._workspace_for_dataset[ds_id] = ws_id
            self._dataset_name_by_id[ds_id] = ds_name
            print(
                f"  [usage-metrics] workspace '{report.workspace_name}' -> "
                f"dataset '{ds_name}' ({ds_id[:8]}...)",
                file=sys.stderr,
            )
        else:
            self.workspaces_not_bootstrapped.append(report.workspace_name)
            print(
                f"  ! workspace '{report.workspace_name}' has no "
                f"'{self._usage_dataset_name}' dataset. Bootstrap once by clicking "
                "'...' -> 'View usage metrics report' on any report in this "
                "workspace, then re-run. Skipping this workspace.",
                file=sys.stderr,
            )
        return ds_id

    def _lookup_usage_metrics_dataset_for_workspace(self, ws_id: str) -> tuple[str, str]:
        """Return (dataset_id, dataset_name) for the workspace's Usage
        Metrics dataset, or ("", "") if not found.
        Matches names that start with the configured prefix to cover
        both 'Usage Metrics Report' and the legacy 'Usage Metrics Report v2'.
        """
        r = self._request(
            "GET",
            f"{POWERBI_API}/admin/groups/{ws_id}/datasets",
        )
        for d in r.json().get("value", []):
            name = d.get("name") or ""
            if name.startswith(self._usage_dataset_name):
                return d["id"], name
        return "", ""

    def query_page_views(
        self,
        dataset_id: str,
        since: date,
        until: date,
        report_id: str | None = None,
    ) -> Iterator[PageViewRow]:
        """Execute the page-views DAX query against the per-workspace
        Usage Metrics semantic model, filtered to a single report id.

        Default path: POST /datasets/{id}/executeQueries (REST, JSON in/out).
        Advanced path: if pyadomd + ADOMD.NET are available AND
        `PBI_USE_PYADOMD=1` is set, use a true XMLA connection instead.

        Both paths produce the same `PageViewRow` shape downstream.
        """
        if not dataset_id:
            # Workspace wasn't bootstrapped; ensure_usage_metrics_dataset
            # has already warned and recorded it.
            return

        if not report_id:
            raise ValueError(
                "query_page_views() requires report_id when called against the "
                "per-workspace Usage Metrics dataset (so DAX can filter to one "
                "report)."
            )

        if os.environ.get("PBI_USE_PYADOMD") and self._xmla_available:
            yield from self._query_via_pyadomd(dataset_id, since, until, report_id)
            return

        ws_id = self._workspace_for_dataset.get(dataset_id)
        if not ws_id:
            raise RuntimeError(
                f"Could not resolve workspace for dataset {dataset_id} - "
                "ensure_usage_metrics_dataset must be called first."
            )

        dax = self._dax_with_filters(since, until, report_id)
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

    def _dax_with_filters(self, since: date, until: date, report_id: str) -> str:
        """Build the parameterised DAX. CALCULATETABLE pushes the date +
        report filters down before SUMMARIZECOLUMNS aggregates, which
        keeps the query cheap on workspaces with many reports."""
        return DAX_PAGE_VIEWS_TEMPLATE.format(
            since_y=since.year, since_m=since.month, since_d=since.day,
            until_y=until.year, until_m=until.month, until_d=until.day,
            # Report Ids are GUIDs - safe to embed in DAX without escaping.
            report_id=report_id,
        )

    def _coerce_executequeries_row(self, raw: dict, dataset_id: str) -> Iterator[PageViewRow]:
        """`executeQueries` returns rows keyed by the DAX column expressions -
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

    # ---- v0.3.0: page catalog (unused-page detection) ---------------------
    def list_report_pages(self, report: Report) -> Iterator[PageCatalogRow]:
        """Return the full roster of pages that exist in `report` at
        collection time. Source: `GET /v1.0/myorg/groups/{ws}/reports/{rep}/pages`
        — a supported, documented Power BI REST endpoint (not preview).
        The SP must be a member of the workspace, which it already needs
        to be to call executeQueries.

        Used to LEFT JOIN with page_views to find pages that exist but
        have zero views in a given window = unused pages.
        """
        pulled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        r = self._request(
            "GET",
            f"{POWERBI_API}/groups/{report.workspace_id}/reports/{report.id}/pages",
            allow_status=(403, 404),
        )
        if r.status_code in (403, 404):
            # Not fatal — happens for paginated reports, dataflows, etc.
            # Leaves the report invisible to unused-page detection but
            # doesn't break the run.
            print(
                f"  ! GET pages for {report.workspace_name}/{report.name} "
                f"returned HTTP {r.status_code}; skipping catalog for this report.",
                file=sys.stderr,
            )
            return
        for p in r.json().get("value", []):
            yield PageCatalogRow(
                workspace_id=report.workspace_id,
                workspace_name=report.workspace_name,
                report_id=report.id,
                report_name=report.name,
                page_id=p.get("name", "") or "",
                page_name=p.get("displayName", "") or "",
                page_ordinal=int(p.get("order", 0) or 0),
                catalog_pulled_at=pulled_at,
            )

    # ---- v0.3.0: report-level grain --------------------------------------
    def query_report_views(
        self,
        dataset_id: str,
        since: date,
        until: date,
        report_id: str | None = None,
    ) -> Iterator[ReportViewRow]:
        """Report-level views from the per-workspace UM model's
        `Report views` table. One row per report/date."""
        if not dataset_id:
            return
        if not report_id:
            raise ValueError(
                "query_report_views() requires report_id when called against "
                "the per-workspace Usage Metrics dataset."
            )
        dax = DAX_REPORT_VIEWS_TEMPLATE.format(
            since_y=since.year, since_m=since.month, since_d=since.day,
            until_y=until.year, until_m=until.month, until_d=until.day,
            report_id=report_id,
        )
        for row in self._execute_dax(dataset_id, dax):
            yield from self._coerce_report_view_row(row, dataset_id)

    # ---- v0.3.0: per-user grain ------------------------------------------
    def query_user_views(
        self,
        dataset_id: str,
        since: date,
        until: date,
        report_id: str | None = None,
    ) -> Iterator[UserViewRow]:
        """Per-user views (hashed UPN). One row per user/report/date."""
        if not dataset_id:
            return
        if not report_id:
            raise ValueError(
                "query_user_views() requires report_id when called against "
                "the per-workspace Usage Metrics dataset."
            )
        dax = DAX_USER_VIEWS_TEMPLATE.format(
            since_y=since.year, since_m=since.month, since_d=since.day,
            until_y=until.year, until_m=until.month, until_d=until.day,
            report_id=report_id,
        )
        for row in self._execute_dax(dataset_id, dax):
            yield from self._coerce_user_view_row(row, dataset_id)

    # ---- shared DAX execution helper -------------------------------------
    def _execute_dax(self, dataset_id: str, dax: str) -> Iterator[dict]:
        """POST DAX to executeQueries and yield raw row dicts. Shared by
        page-views / report-views / user-views queries.

        Centralising this means a future migration to true XMLA via pyadomd
        only needs to change one place."""
        ws_id = self._workspace_for_dataset.get(dataset_id)
        if not ws_id:
            raise RuntimeError(
                f"Could not resolve workspace for dataset {dataset_id} - "
                "ensure_usage_metrics_dataset must be called first."
            )
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
        yield from tables[0].get("rows", [])

    def _coerce_report_view_row(self, raw: dict, dataset_id: str) -> Iterator[ReportViewRow]:
        """Map an executeQueries row to ReportViewRow. Driver's _enrich_*
        step will fill workspace/report metadata from earlier enumeration."""
        def g(*keys: str, default=None):
            for k in keys:
                if k in raw and raw[k] is not None:
                    return raw[k]
            return default
        view_date = str(g("'Report views'[Date]", "[Date]") or "")[:10]
        yield ReportViewRow(
            workspace_id="",
            workspace_name="",
            capacity_name="",
            report_id=g("'Report views'[Report Id]", default="") or "",
            report_name="",
            view_date=view_date,
            view_count=int(g("[Views]", default=0) or 0),
            unique_users=int(g("[UniqueUsers]", default=0) or 0),
            avg_session_seconds=float(g("[AvgSessionSeconds]", default=0.0) or 0.0),
        )

    def _coerce_user_view_row(self, raw: dict, dataset_id: str) -> Iterator[UserViewRow]:
        """Map an executeQueries row to UserViewRow. UPN is hashed
        IMMEDIATELY — raw UPN never leaves this function."""
        def g(*keys: str, default=None):
            for k in keys:
                if k in raw and raw[k] is not None:
                    return raw[k]
            return default
        view_date = str(g("'Report page views'[Date]", "[Date]") or "")[:10]
        upn = g("'Report page views'[User]", default="") or ""
        yield UserViewRow(
            workspace_id="",
            workspace_name="",
            report_id=g("'Report page views'[Report Id]", default="") or "",
            report_name="",
            user_id_hash=_hash_upn(str(upn)),
            view_date=view_date,
            view_count=int(g("[Views]", default=0) or 0),
            distinct_pages_viewed=int(g("[DistinctPagesViewed]", default=0) or 0),
        )

    def _query_via_pyadomd(
        self,
        dataset_id: str,
        since: date,
        until: date,
        report_id: str,
    ) -> Iterator[PageViewRow]:
        from pyadomd import Pyadomd  # type: ignore
        ws_id = self._workspace_for_dataset.get(dataset_id, "")
        ws = self._workspaces_by_id.get(ws_id)
        if not ws:
            raise RuntimeError(f"Could not find workspace metadata for id {ws_id}")
        ds_name = self._dataset_name_by_id.get(dataset_id) or self._usage_dataset_name
        conn = (
            f"Provider=MSOLAP;"
            f"Data Source=powerbi://api.powerbi.com/v1.0/myorg/{ws.name};"
            f"Initial Catalog={ds_name};"
            f"User ID=app:{self.client_id}@{self.tenant_id};"
            f"Password={self.client_secret};"
        )
        dax = self._dax_with_filters(since, until, report_id)
        with Pyadomd(conn).cursor() as cur:
            cur.execute(dax)
            cols = [c.name for c in cur.description]
            for row in cur.fetchall():
                rec = dict(zip(cols, row, strict=False))
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
        # Intentionally-unused pages overlay. Lives next to the sample CSV.
        # Format: { "<report_id>": [ {"page_id": "...", "page_name": "...", "page_ordinal": N}, ... ] }
        # These are added to the page catalog but NEVER to page_views, so
        # the LEFT JOIN proves unused-page detection works end-to-end.
        self._unused_overlay: dict[str, list[dict]] = {}
        overlay_path = csv_path.parent / "unused_pages.json"
        if overlay_path.exists():
            try:
                self._unused_overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # Bad overlay file shouldn't break the run; just no unused pages.
                self._unused_overlay = {}

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

    def query_page_views(
        self,
        dataset_id: str,
        since: date,
        until: date,
        report_id: str | None = None,
    ) -> Iterator[PageViewRow]:
        # dataset_id maps back to a report_id via convention `ds-um::<report-id>`.
        # The optional `report_id` kwarg (used by LiveAdapter) is also accepted
        # for symmetry; mock-mode uses the dataset_id convention.
        rep_id = report_id or dataset_id.split("::", 1)[1]
        for r in self._rows:
            if r["report_id"] != rep_id:
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

    # ---- v0.3.0 mock implementations -------------------------------------
    def list_report_pages(self, report: Report) -> Iterator[PageCatalogRow]:
        """Synthesize the page catalog from page_views rows (every page
        that ever had a view), PLUS the intentionally-unused-pages overlay
        (pages that exist but have zero views — for testing unused detection).
        """
        # Deterministic timestamp so mock-mode silver is byte-reproducible
        # across machines and Python versions (real LiveAdapter uses wall clock).
        pulled_at = self.max_csv_date().isoformat() + "T00:00:00+00:00"
        seen: set[str] = set()
        for r in self._rows:
            if r["report_id"] != report.id:
                continue
            if r["page_id"] in seen:
                continue
            seen.add(r["page_id"])
            yield PageCatalogRow(
                workspace_id=r["workspace_id"],
                workspace_name=r["workspace_name"],
                report_id=r["report_id"],
                report_name=r["report_name"],
                page_id=r["page_id"],
                page_name=r["page_name"],
                page_ordinal=int(r["page_ordinal"]),
                catalog_pulled_at=pulled_at,
            )
        # Add the intentionally-unused pages — these will have NO matching
        # page_views rows, so a LEFT JOIN surfaces them as "unused".
        for extra in self._unused_overlay.get(report.id, []):
            pid = extra.get("page_id", "")
            if pid in seen:
                continue
            yield PageCatalogRow(
                workspace_id=report.workspace_id,
                workspace_name=report.workspace_name,
                report_id=report.id,
                report_name=report.name,
                page_id=pid,
                page_name=extra.get("page_name", ""),
                page_ordinal=int(extra.get("page_ordinal", 0) or 0),
                catalog_pulled_at=pulled_at,
            )

    def query_report_views(
        self,
        dataset_id: str,
        since: date,
        until: date,
        report_id: str | None = None,
    ) -> Iterator[ReportViewRow]:
        """Synthesize report-level rows by aggregating page_views by
        (report, date). Note: avg_session_seconds in mock-mode is the
        avg of page-level dwell weighted by views — it's an approximation
        of session time, not a true measurement (the real UM model has
        a dedicated `Report views`.[Average view time] measure)."""
        rep_id = report_id or dataset_id.split("::", 1)[1]
        # (report_id, view_date) -> aggregates
        agg: dict[tuple, dict] = {}
        for r in self._rows:
            if r["report_id"] != rep_id:
                continue
            d = date.fromisoformat(r["view_date"])
            if d < since or d > until:
                continue
            key = (r["report_id"], r["view_date"])
            slot = agg.setdefault(key, {
                "workspace_id": r["workspace_id"],
                "workspace_name": r["workspace_name"],
                "capacity_name": r["capacity_name"],
                "report_name": r["report_name"],
                "views": 0, "users_seen": set(), "dwell_weighted_sum": 0.0,
            })
            v = int(r["view_count"])
            slot["views"] += v
            slot["dwell_weighted_sum"] += v * float(r["avg_dwell_seconds"])
            # unique_users at page grain isn't user-level — best we can do
            # without raw UPNs is take the max across pages (a lower bound
            # on report-level unique users).
            slot.setdefault("max_pageusers", 0)
            slot["max_pageusers"] = max(slot["max_pageusers"], int(r["unique_users"]))
        for (rid, d), s in sorted(agg.items(), key=lambda kv: kv[0][1]):
            yield ReportViewRow(
                workspace_id=s["workspace_id"],
                workspace_name=s["workspace_name"],
                capacity_name=s["capacity_name"],
                report_id=rid,
                report_name=s["report_name"],
                view_date=d,
                view_count=s["views"],
                unique_users=s["max_pageusers"],
                avg_session_seconds=(
                    s["dwell_weighted_sum"] / s["views"] if s["views"] else 0.0
                ),
            )

    def query_user_views(
        self,
        dataset_id: str,
        since: date,
        until: date,
        report_id: str | None = None,
    ) -> Iterator[UserViewRow]:
        """Synthesize per-user rows. The mock CSV doesn't carry real UPNs,
        so we manufacture them deterministically from (report_id, page_ordinal,
        view_date) — same input always produces the same hash, so silver
        hashes stay reproducible across runs. Distinct page count is the
        number of distinct page_ordinals that report saw on that date."""
        rep_id = report_id or dataset_id.split("::", 1)[1]
        # (report_id, hashed_user, view_date) -> aggregates
        agg: dict[tuple, dict] = {}
        for r in self._rows:
            if r["report_id"] != rep_id:
                continue
            d = date.fromisoformat(r["view_date"])
            if d < since or d > until:
                continue
            views = int(r["view_count"])
            # Distribute the page's views across N synthetic users where
            # N == unique_users on that page/day. Cap at 1 to avoid zero.
            n_users = max(1, int(r["unique_users"]))
            # Synthesize a deterministic UPN per (report, view_date, user_index).
            for i in range(n_users):
                synth_upn = f"user{i:03d}@mock-{r['workspace_id'][:8]}"
                uhash = _hash_upn(synth_upn)
                key = (r["report_id"], uhash, r["view_date"])
                slot = agg.setdefault(key, {
                    "workspace_id": r["workspace_id"],
                    "workspace_name": r["workspace_name"],
                    "report_name": r["report_name"],
                    "views": 0, "pages": set(),
                })
                # Each synthetic user contributes proportional views.
                slot["views"] += max(1, views // n_users)
                slot["pages"].add(r["page_id"])
        for (rid, uhash, d), s in sorted(agg.items(), key=lambda kv: (kv[0][2], kv[0][1])):
            yield UserViewRow(
                workspace_id=s["workspace_id"],
                workspace_name=s["workspace_name"],
                report_id=rid,
                report_name=s["report_name"],
                user_id_hash=uhash,
                view_date=d,
                view_count=s["views"],
                distinct_pages_viewed=len(s["pages"]),
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
    emit four conformed silver CSVs and a `_run_summary.json` with the
    silver schema version.

    Bronze layout:
        bronze/dt=YYYY-MM-DD/page_views/{wsId}__{reportId}.csv
        bronze/dt=YYYY-MM-DD/page_catalog/{wsId}__{reportId}.csv
        bronze/dt=YYYY-MM-DD/report_views/{wsId}__{reportId}.csv
        bronze/dt=YYYY-MM-DD/user_views/{wsId}__{reportId}.csv

    Silver layout (each preceded by a `# silver_schema_version=...` comment):
        silver/page_views.csv       (one row per ws/report/page/date)
        silver/page_catalog.csv     (one row per ws/report/page — page roster)
        silver/report_views.csv     (one row per ws/report/date — no page dim)
        silver/user_views.csv       (one row per ws/report/user_hash/date)

    Unused-page detection = LEFT JOIN(page_catalog, page_views)
                              ON (workspace_id, report_id, page_id)
                            WHERE views IS NULL or 0.
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
    # One sub-folder per silver feed so bronze stays browsable.
    for feed in ("page_views", "page_catalog", "report_views", "user_views"):
        (bronze_run / feed).mkdir(parents=True, exist_ok=True)

    summary = {
        "schema_version": SILVER_SCHEMA_VERSION,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since.isoformat(), "until": until.isoformat(),
        "workspaces": 0, "reports": 0, "datasets": 0,
        "page_view_rows": 0,
        "page_catalog_rows": 0,
        "report_view_rows": 0,
        "user_view_rows": 0,
        # Derived metric — the headline value-add for v0.3.0. Computed
        # after all reports finish (needs the LEFT JOIN to run).
        # v0.3.1: also materializes the list to silver/unused_pages.csv.
        "unused_pages": 0,
        "reports_with_unused_pages": 0,
        # Top-10 of unused pages with human names (ops-log preview).
        # Full list lives in silver/unused_pages.csv.
        "unused_pages_sample": [],
        # Back-compat: keep `rows` as a synonym for page_view_rows so any
        # v0.2.x dashboard reading the summary still works.
        "rows": 0,
        "reports_skipped_no_bootstrap": 0,
        "workspaces_not_bootstrapped": [],
        "errors": [],
    }
    all_page_view_rows: list[PageViewRow] = []
    all_page_catalog_rows: list[PageCatalogRow] = []
    all_report_view_rows: list[ReportViewRow] = []
    all_user_view_rows: list[UserViewRow] = []
    seen_dataset_ids: set[str] = set()

    for ws in adapter.list_workspaces():
        summary["workspaces"] += 1
        print(f"[workspace] {ws.name} ({ws.id})")
        for rep in adapter.list_reports(ws):
            summary["reports"] += 1
            try:
                ds_id = adapter.ensure_usage_metrics_dataset(rep)
                if not ds_id:
                    # Workspace hasn't been bootstrapped; ensure_usage_metrics_dataset
                    # already logged the friendly warning. Skip this report.
                    summary["reports_skipped_no_bootstrap"] += 1
                    continue
                if ds_id not in seen_dataset_ids:
                    summary["datasets"] += 1
                    seen_dataset_ids.add(ds_id)
                # Pull all 4 feeds for this report.
                pv_raw = list(
                    adapter.query_page_views(ds_id, since, until, report_id=rep.id)
                )
                pc_raw = list(adapter.list_report_pages(rep))
                rv_raw = list(
                    adapter.query_report_views(ds_id, since, until, report_id=rep.id)
                )
                uv_raw = list(
                    adapter.query_user_views(ds_id, since, until, report_id=rep.id)
                )
            except Exception as e:
                msg = f"{rep.workspace_name}/{rep.name}: {type(e).__name__}: {e}"
                summary["errors"].append(msg)
                print(f"  ! {msg}")
                continue
            # Enrich rows that came from the live DAX path with bare identifiers.
            pv_rows = [_enrich_page_view(r, ws, rep) for r in pv_raw]
            pc_rows = list(pc_raw)  # always pre-enriched from REST
            rv_rows = [_enrich_report_view(r, ws, rep) for r in rv_raw]
            uv_rows = [_enrich_user_view(r, ws, rep) for r in uv_raw]
            _write_rows(bronze_run / "page_views"    / f"{rep.workspace_id}__{rep.id}.csv", pv_rows)
            _write_rows(bronze_run / "page_catalog"  / f"{rep.workspace_id}__{rep.id}.csv", pc_rows)
            _write_rows(bronze_run / "report_views"  / f"{rep.workspace_id}__{rep.id}.csv", rv_rows)
            _write_rows(bronze_run / "user_views"    / f"{rep.workspace_id}__{rep.id}.csv", uv_rows)
            all_page_view_rows.extend(pv_rows)
            all_page_catalog_rows.extend(pc_rows)
            all_report_view_rows.extend(rv_rows)
            all_user_view_rows.extend(uv_rows)
            summary["page_view_rows"]    += len(pv_rows)
            summary["page_catalog_rows"] += len(pc_rows)
            summary["report_view_rows"]  += len(rv_rows)
            summary["user_view_rows"]    += len(uv_rows)
            print(
                f"  [report] {rep.name}: "
                f"pv={len(pv_rows):,} cat={len(pc_rows)} "
                f"rv={len(rv_rows)} uv={len(uv_rows)}"
            )

    # Pull the bootstrap-needed list off the adapter if it tracked one.
    not_bootstrapped = getattr(adapter, "workspaces_not_bootstrapped", None)
    if not_bootstrapped:
        # de-dup while preserving order
        seen: set[str] = set()
        summary["workspaces_not_bootstrapped"] = [
            n for n in not_bootstrapped if not (n in seen or seen.add(n))
        ]

    # Compute unused-pages headline metric: pages that exist in the
    # catalog but have ZERO page-view rows in the collection window.
    viewed_page_keys = {
        (r.workspace_id, r.report_id, r.page_id) for r in all_page_view_rows
    }
    unused_keys = {
        (c.workspace_id, c.report_id, c.page_id) for c in all_page_catalog_rows
        if (c.workspace_id, c.report_id, c.page_id) not in viewed_page_keys
    }
    # v0.3.1: materialize the unused list, don't just count it. Consumers
    # asked "I can't see the report names of those unused" — fair feedback.
    # Sort key is (workspace_name, report_name, page_ordinal) so a human
    # opening unused_pages.csv in Excel sees pages in their natural order.
    all_unused_page_rows: list[UnusedPageRow] = sorted(
        (
            UnusedPageRow(
                workspace_id=c.workspace_id,
                workspace_name=c.workspace_name,
                report_id=c.report_id,
                report_name=c.report_name,
                page_id=c.page_id,
                page_name=c.page_name,
                page_ordinal=c.page_ordinal,
                catalog_pulled_at=c.catalog_pulled_at,
            )
            for c in all_page_catalog_rows
            if (c.workspace_id, c.report_id, c.page_id) in unused_keys
        ),
        key=lambda r: (r.workspace_name, r.report_name, r.page_ordinal),
    )
    summary["unused_pages"] = len(unused_keys)
    summary["reports_with_unused_pages"] = len({(ws, rep) for (ws, rep, _) in unused_keys})
    # Ops-log preview — first 10 unused pages with their human names so a
    # grep of the logs shows "Protocol v1 (legacy)" etc. instead of just "10".
    summary["unused_pages_sample"] = [
        {
            "workspace_name": r.workspace_name,
            "report_name": r.report_name,
            "page_name": r.page_name,
            "page_ordinal": r.page_ordinal,
        }
        for r in all_unused_page_rows[:10]
    ]

    # Silver: 5 conformed CSVs across the tenant, each prefaced with the
    # schema-version comment so downstream MERGEs can assert compatibility.
    pv_path  = silver / "page_views.csv"
    pc_path  = silver / "page_catalog.csv"
    rv_path  = silver / "report_views.csv"
    uv_path  = silver / "user_views.csv"
    up_path  = silver / "unused_pages.csv"
    _write_rows(pv_path, all_page_view_rows,    schema_version_comment=True)
    _write_rows(pc_path, all_page_catalog_rows, schema_version_comment=True)
    _write_rows(rv_path, all_report_view_rows,  schema_version_comment=True)
    _write_rows(uv_path, all_user_view_rows,    schema_version_comment=True)
    _write_rows(up_path, all_unused_page_rows,  schema_version_comment=True)
    summary["silver_paths"] = {
        "page_views":    str(pv_path),
        "page_catalog":  str(pc_path),
        "report_views":  str(rv_path),
        "user_views":    str(uv_path),
        "unused_pages":  str(up_path),
    }
    # Back-compat: keep the v0.2.x `silver_path` key (page_views only)
    # so any external dashboard reading the summary keeps working.
    summary["silver_path"] = str(pv_path)
    summary["rows"] = summary["page_view_rows"]
    summary["bronze_partition"] = str(bronze_run)
    summary["ended_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    (out_dir / "_run_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8",
    )
    return summary


def _enrich_page_view(row: PageViewRow, ws: Workspace, rep: Report) -> PageViewRow:
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


# Back-compat alias for code (or tests) that imported the v0.2.x name.
_enrich_row = _enrich_page_view


def _enrich_report_view(row: ReportViewRow, ws: Workspace, rep: Report) -> ReportViewRow:
    if row.workspace_id and row.workspace_name and row.report_name:
        return row
    return ReportViewRow(
        workspace_id=row.workspace_id or ws.id,
        workspace_name=row.workspace_name or ws.name,
        capacity_name=row.capacity_name or (ws.capacity_name or ""),
        report_id=row.report_id or rep.id,
        report_name=row.report_name or rep.name,
        view_date=row.view_date,
        view_count=row.view_count,
        unique_users=row.unique_users,
        avg_session_seconds=row.avg_session_seconds,
    )


def _enrich_user_view(row: UserViewRow, ws: Workspace, rep: Report) -> UserViewRow:
    if row.workspace_id and row.workspace_name and row.report_name:
        return row
    return UserViewRow(
        workspace_id=row.workspace_id or ws.id,
        workspace_name=row.workspace_name or ws.name,
        report_id=row.report_id or rep.id,
        report_name=row.report_name or rep.name,
        user_id_hash=row.user_id_hash,
        view_date=row.view_date,
        view_count=row.view_count,
        distinct_pages_viewed=row.distinct_pages_viewed,
    )


def _write_rows(
    path: Path,
    rows: Iterable,
    *,
    schema_version_comment: bool = False,
) -> None:
    rows = list(rows)
    if not rows:
        # For silver files we still want an empty file with the schema-version
        # comment so downstream MERGEs can detect the file exists and is
        # compatible. Bronze writes (no schema comment) just skip empty files.
        if schema_version_comment:
            with path.open("w", newline="", encoding="utf-8") as f:
                f.write(f"# silver_schema_version={SILVER_SCHEMA_VERSION}\n")
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
  PBI_USAGE_DATASET_NAME  Override the dataset name to look up
                          (default: "Usage Metrics Report")
  PBI_USE_PYADOMD         Set to "1" to route DAX via XMLA + ADOMD.NET
                          instead of REST (Windows-only, advanced).
  PBI_OUTPUT_DIR          Where to write bronze/ and silver/ (defaults to ./out)

Per-workspace bootstrap prerequisite (live mode)
------------------------------------------------
The Modern Usage Metrics semantic model is created on the FIRST click
of "View usage metrics report" on any report in a workspace. There is
no public REST API to create it (confirmed by Power BI PM David Browne).
Workspaces without a bootstrapped model are skipped at runtime and
listed in `_run_summary.json -> workspaces_not_bootstrapped` so an admin
can do the one-time click in bulk and re-run.

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
    p.add_argument("--metrics", choices=["none", "appinsights", "prometheus"],
                   default="none",
                   help="Emit a one-line operational metric line on completion. "
                        "'appinsights' = customMetric JSON (greppable from logs into a Workbook); "
                        "'prometheus' = exposition-format gauges (greppable into a textfile collector).")
    args = p.parse_args(argv)

    # Env-var equivalent of --mock so containerized deploys (Azure Function,
    # Container Apps) can be smoke-tested with no credentials before being
    # cut over to live mode.
    mock = args.mock or os.environ.get("PBI_MOCK", "").lower() in ("1", "true", "yes")

    out_dir = args.out or Path(os.environ.get("PBI_OUTPUT_DIR") or (HERE / "out"))

    until = date.today()
    # In mock mode shift the window to the synthetic data's actual date
    # range so the script always produces output, even years after the
    # sample CSV was generated.
    if mock:
        until = MockAdapter.max_csv_date()
    since = until - timedelta(days=args.days - 1)

    if mock:
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

    if args.metrics != "none":
        _emit_metrics(args.metrics, summary)

    return 0


def _emit_metrics(fmt: str, summary: dict) -> None:
    """Print a single greppable line summarizing this run for ops tooling.

    Both formats are intentionally one-line and stdout-only so that any log
    sink (Application Insights `traces`, Splunk, Elastic, Promtail) can
    parse them without extra plumbing."""
    workspaces = summary.get("workspaces", 0)
    reports = summary.get("reports", 0)
    datasets = summary.get("datasets", 0)
    rows = summary.get("rows", 0)
    errors = len(summary.get("errors", []) or [])
    schema = summary.get("schema_version", "")
    if fmt == "appinsights":
        # customMetric-shaped JSON; works as-is in App Insights `customEvents`
        # when ingested by the Functions worker, and is trivially parseable
        # by any other log scraper.
        line = {
            "metric": "pbi.page_telemetry.run",
            "schema_version": schema,
            "workspaces": workspaces,
            "reports": reports,
            "datasets": datasets,
            "rows": rows,
            "errors": errors,
            "ok": errors == 0,
        }
        print(f"METRIC {json.dumps(line, separators=(',', ':'))}")
    elif fmt == "prometheus":
        # Prometheus exposition format. Each line is a gauge; scrape via
        # the Prometheus node_exporter textfile collector or push via
        # statsd_exporter.
        labels = f'{{schema_version="{schema}"}}'
        out = [
            f"pbi_page_telemetry_workspaces{labels} {workspaces}",
            f"pbi_page_telemetry_reports{labels} {reports}",
            f"pbi_page_telemetry_datasets{labels} {datasets}",
            f"pbi_page_telemetry_rows{labels} {rows}",
            f"pbi_page_telemetry_errors{labels} {errors}",
            f"pbi_page_telemetry_ok{labels} {1 if errors == 0 else 0}",
        ]
        print("\n".join(out))


if __name__ == "__main__":
    raise SystemExit(main())
