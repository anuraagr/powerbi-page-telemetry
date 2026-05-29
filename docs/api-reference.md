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
| `GET /v1.0/myorg/admin/groups/{wsId}/datasets` | Find the per-workspace **Modern Usage Metrics** semantic model | shared bucket |
| `POST /v1.0/myorg/groups/{wsId}/datasets/{dsId}/executeQueries` | Execute DAX against the Usage Metrics dataset (REST path, no DLLs) | 120 req/min per user |
| `GET /v1.0/myorg/admin/capacities` | (optional) Map workspaces → capacities for tier-of-service reporting | shared bucket |

> **There is no public REST endpoint that provisions the Usage Metrics
> dataset.** This is confirmed by Power BI PM David Browne (HLS
> Roundtable, May 2026): *"We don't have an API, but
> [Monitor Usage Metrics for Workspaces (preview)] builds a semantic
> model you can access."* The semantic model is created lazily on the
> **first portal click** of `...` → `View usage metrics report` in a
> workspace — see §3 below. Workspaces that have never been
> bootstrapped surface as `workspaces_not_bootstrapped` in
> `_run_summary.json` and are skipped.

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

→ no `Page` or `Section` field. That's why this collector goes via the
per-workspace Usage Metrics semantic model instead.

## 3. The one-time workspace bootstrap (no public REST)

For each workspace whose reports you want page-level telemetry for, an
admin or workspace contributor must do this **once**, by hand, in the
Power BI portal:

1. Open any report in the workspace.
2. Click the `...` menu in the report header.
3. Click **View usage metrics report**.
4. Wait ~5 seconds while Power BI provisions a semantic model named
   **`Usage Metrics Report`** in the workspace. The portal will then
   redirect to the auto-generated usage report — you can close that tab.

After step 4, Power BI accumulates page-level data for **every**
report in the workspace into that one semantic model, refreshed every
~24h by Microsoft. The collector reads that one model per workspace
via `executeQueries` (§4).

The Power BI engineering team's roadmap confirms this preview feature
is targeting **GA in September 2026** (per Rui Romano, HLS Roundtable
May 2026). After GA there will still be no public REST to provision
the model — the one-time portal click remains the prerequisite.

If a tenant has the legacy variant of this dataset (named
`Usage Metrics Report v2 - <reportname>`), set
`PBI_USAGE_DATASET_NAME=Usage Metrics Report v2` and the collector's
prefix-match will pick it up.

## 4. DAX execution endpoint

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

## 5. The DAX query

Run this against the per-workspace Usage Metrics semantic model, once
per report you want page-level telemetry for. `CALCULATETABLE` pushes
the report-id and date filters down **before** aggregation — cheaper
than filtering after `SUMMARIZECOLUMNS` on workspaces with many
reports.

```dax
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
```

The Modern Usage Metrics dataset schema (relevant tables, best-effort —
Microsoft does not publish the schema explicitly):

| Table | Columns of interest |
| --- | --- |
| `Reports` | `Report Id`, `Report name`, `Workspace Id` |
| `Report pages` | `Report Id`, `Page Id`, `Page name`, `Page ordinal` |
| `Report page views` | `Report Id`, `Date`, `Report page name`, `Views`, `User`, `Average view time` |
| `Users` | `User`, `User principal name` |

If your tenant's schema uses different column names, override
`DAX_PAGE_VIEWS_TEMPLATE` in `collector.py` or open an issue with
the introspection output of `EVALUATE INFO.TABLES()` +
`EVALUATE INFO.COLUMNS()`.

## 6. Throttling, retries, and quota

- Admin REST API: 200 req/hour tenant-wide on most endpoints; some
  (`getActivityEvents`) are 200 req/hour per tenant. Retry on 429 with
  exponential backoff (`Retry-After` honored).
- `executeQueries`: 120 req/min per user. With one query per report,
  a tenant with thousands of reports must throttle. Use the
  Modern Usage Metrics dataset's coverage of an entire workspace to
  collapse N queries into 1 per workspace if your downstream model
  doesn't need per-report rows.
- XMLA: governed by the capacity itself. One DAX query at a time per
  connection is safe; parallelism is bounded by capacity CU.

## 7. RBAC matrix

| Action | Required role |
| --- | --- |
| `GET /admin/*` REST endpoints | Fabric Administrator **or** SG enabled for Read-only admin APIs |
| Portal "View usage metrics report" click (one-time bootstrap per workspace) | Workspace Admin or Member |
| `POST /datasets/{id}/executeQueries` against the Usage Metrics model | Workspace Member or Admin **and** capacity admin's `Allow XMLA endpoints to use service principals` setting enabled |
| RLS-aware read of gold semantic model | Granted via Entra group → workspace role |

## 8. What changes at GA of Monitor Usage Metrics for Workspaces

The Modern Usage Metrics feature this collector reads is in **public
preview** today (May 2026) and targeting **GA September 2026** per Rui
Romano (Power BI PM, HLS Roundtable). At GA:

- Semantic-model schema may stabilize and become publicly documented;
  expect minor column-name tweaks. If they happen, the collector's
  `PBI_USAGE_DATASET_NAME` / `DAX_PAGE_VIEWS_TEMPLATE` knobs absorb
  the change without code edits.
- The one-time portal bootstrap step may move into the Admin Portal
  as a tenant-wide toggle (no per-workspace clicks). Watch the
  Power BI release notes.
- Microsoft may eventually ship a public REST endpoint that does the
  same enumeration + DAX execution this collector does — at which
  point this repo becomes a reference implementation of the same
  pattern, not the only way to do it.

Silver / gold schemas in this repo are forward-compatible — no change
needed to the dashboard or downstream consumers.
