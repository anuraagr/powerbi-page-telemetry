# API Reference & DAX Cheat Sheet

Everything the collector touches in the Power BI / Fabric platform,
exact endpoints and payloads.

## 1. Auth

`POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`

```http
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
client_id=<service principal app id>
client_secret=<secret>
scope=https://analysis.windows.net/powerbi/api/.default
```

The returned access_token is used as `Authorization: Bearer …` for both
REST and XMLA (XMLA accepts the same token via `Password=` when the
`User ID=app:<clientId>@<tenantId>` form is used).

## 2. Power BI Admin REST API endpoints used

| Verb & path | Purpose | Throttle |
| --- | --- | --- |
| `GET /v1.0/myorg/admin/groups?$top=100&$skip={n}&$filter=type eq 'Workspace' and state eq 'Active'` | List workspaces | 200 req/h tenant-wide |
| `GET /v1.0/myorg/admin/groups/{wsId}/reports` | List reports in a workspace | shared bucket |
| `POST /v1.0/myorg/admin/reports/{reportId}/usageMetrics` | Ensure Usage Metrics v2 dataset exists; returns `datasetId` | shared bucket |
| `GET /v1.0/myorg/admin/groups/{wsId}/datasets` | Find Usage Metrics dataset id if POST returned 409 | shared bucket |
| `POST /v1.0/myorg/groups/{wsId}/datasets/{dsId}/executeQueries` | Execute DAX against the Usage Metrics dataset (REST path, no DLLs) | 120 req/min per user |
| `GET /v1.0/myorg/admin/capacities` | (optional) Map workspaces → capacities for tier-of-service reporting | shared bucket |

All admin endpoints require the service principal to be a **Fabric
Administrator** OR to be in a security group enabled for `Read-only
admin APIs` tenant setting.

### Activity Events API — why this isn't enough

`GET /v1.0/myorg/admin/activityevents?startDateTime=...&endDateTime=...`

This endpoint **does not emit page-level events**. The `ViewReport`
event is the finest grain available:

```json
{
  "Id": "1db4c464-...",
  "CreationTime": "2026-05-23T08:43:34",
  "Operation": "ViewReport",
  "Activity": "ViewReport",
  "ItemName": "Capacity Metrics Analysis",
  "WorkspaceName": "Premium Capacity Utilization And Metrics …",
  "DatasetName": "...",
  "ReportName": "Capacity Metrics Analysis",
  "ReportId": "ae596344-7fe6-43cb-baa7-c7ddc63271c8",
  "ArtifactKind": "Report"
}
```

→ no `Page` or `Section` field. That's why this collector goes via XMLA
on the Usage Metrics dataset instead.

## 3. DAX execution endpoint

The collector executes DAX two possible ways. By default it uses the **REST
endpoint**, which has zero native dependencies and works identically from
Linux Functions, Fabric Spark, and any laptop:

`POST https://api.powerbi.com/v1.0/myorg/groups/{wsId}/datasets/{datasetId}/executeQueries`

```http
Content-Type: application/json
Authorization: Bearer <token>

{
  "queries": [{"query": "EVALUATE ..."}],
  "serializerSettings": {"includeNulls": false}
}
```

Returns JSON. Each `results[].tables[].rows[]` entry is keyed by the DAX
column expression — `'Report page views'[Report Id]`, `[Views]`, etc.
Row limit: 100,000 rows per query. For queries that exceed that, page
by date filter or fall back to the XMLA path below.

### Optional: true XMLA via pyadomd

For very large datasets or on-prem Analysis Services hybrid scenarios,
set `PBI_USE_PYADOMD=1` and install pyadomd + the ADOMD.NET retail
client. The connection string template (use with MSOLAP / ADOMD.NET /
pyadomd / SSMS) is:

```
Provider=MSOLAP;
Data Source=powerbi://api.powerbi.com/v1.0/myorg/{workspaceName};
Initial Catalog={usageMetricsDatasetName};
User ID=app:{clientId}@{tenantId};
Password={clientSecret};
```

Requires:
- Premium / Fabric capacity backing the workspace.
- Admin Portal → Capacity → **XMLA endpoint = Read** or **Read Write**.
- The service principal is a workspace member or has admin rights.
- Windows host (ADOMD.NET is Windows-only). For non-Windows runtimes,
  stick with the REST path.

## 4. The DAX query

Run this against the Usage Metrics dataset for each report. It uses
server-side `SUMMARIZECOLUMNS` so only the aggregated rows traverse the
wire — works fine for high-cardinality datasets.

```dax
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
```

The Usage Metrics v2 dataset schema (relevant tables):

| Table | Columns of interest |
| --- | --- |
| `Reports` | `Report Id`, `Report name`, `Workspace Id` |
| `Report pages` | `Report Id`, `Page Id`, `Page name`, `Page ordinal` |
| `Report page views` | `Report Id`, `Page Id`, `Date`, `Views`, `Unique users`, `Average view time`, `User` |
| `Report views` | (report-level totals — for cross-check) |
| `Users` | `User`, `User principal name` |

For page-level analysis you almost always want `Report page views`. For
auditing, joins to `Users` give you the email back (PII — apply RLS).

## 5. Throttling, retries, and quota

- Admin REST API: 200 req/hour tenant-wide on most endpoints; some
  (`getActivityEvents`) are 200 req/hour per tenant. Retry on 429 with
  exponential backoff (`Retry-After` honored).
- XMLA: governed by the capacity itself. One DAX query at a time per
  connection is safe; parallelism is bounded by capacity CU.
- POST `/usageMetrics` is async on first call; the response is 202 with
  `Location`. Subsequent calls are idempotent (200).

## 6. RBAC matrix

| Action | Required role |
| --- | --- |
| `GET /admin/*` REST endpoints | Fabric Administrator **or** SG enabled for Read-only admin APIs |
| `POST /admin/reports/{id}/usageMetrics` | Fabric Administrator |
| XMLA read on Usage Metrics dataset | Workspace Contributor + capacity admin's `Allow XMLA endpoints to use service principals` setting |
| RLS-aware read of gold semantic model | Granted via Entra group → workspace role |

## 7. What changes at GA of Monitor Usage Metrics for Workspaces

The new (currently preview) feature provisions **one** semantic model
per workspace (instead of one per report) covering reports, dashboards,
paginated reports, page-level activity, and consumption. After GA:

- Replace step 3 (POST `/usageMetrics` per report) with a single
  workspace-level dataset reference.
- Replace step 4 with a single DAX query per workspace, joining
  Reports × Pages × Activity.
- Collector throughput improves 10–50×; capacity cost drops proportionally.
- Silver / gold schemas in this repo are forward-compatible — no change
  needed to the dashboard or downstream consumers.
