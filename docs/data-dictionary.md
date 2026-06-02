# Silver layer data dictionary

This is the contract between the collector and any downstream
consumer — gold semantic model, ad-hoc SQL, the bundled dashboard,
or a custom Power BI report.

**Schema version**: `1.1.0` (matches `SILVER_SCHEMA_VERSION` in
[`etl/collector.py`](../etl/collector.py)). The version is emitted as
the first line of every silver CSV as `# silver_schema_version=1.1.0`
and in `_run_summary.json`. Downstream readers should assert on it
before reading.

**Source of truth**: the **Modern Usage Metrics for Workspaces**
preview semantic model that Power BI auto-provisions per workspace on
the first portal click of `... → View usage metrics report`. The
collector queries this model via the Power BI REST
`POST /datasets/{id}/executeQueries` endpoint with parameterised DAX
against the `'Report page views'` and `'Report views'` tables.
The `page_catalog` table is sourced separately from the documented
`GET /v1.0/myorg/groups/{ws}/reports/{rep}/pages` REST endpoint
(not from DAX — see [api-reference.md](api-reference.md) for why).

The David Browne (Power BI PG) confirmation from the May 2026 HLS
Roundtable: *"we don't have an API, but
[Monitor Usage Metrics for Workspaces] builds a semantic model you
can access."* This collector is the implementation of that pattern,
scaled across thousands of workspaces.

## File layout (v0.3.0)

```
out/
├── _run_summary.json          ← see § Run summary at the bottom
├── bronze/
│   └── dt=2026-05-27/         ← partitioned by collection date
│       ├── page_views/
│       │   └── <wsId>__<reportId>.csv
│       ├── page_catalog/
│       │   └── <wsId>__<reportId>.csv
│       ├── report_views/
│       │   └── <wsId>__<reportId>.csv
│       └── user_views/
│           └── <wsId>__<reportId>.csv
└── silver/
    ├── page_views.csv         ← first line "# silver_schema_version=1.1.0"
    ├── page_catalog.csv       ← (NEW in v0.3.0) every page that EXISTS
    ├── report_views.csv       ← (NEW in v0.3.0) report-level grain
    └── user_views.csv         ← (NEW in v0.3.0) per-user grain (hashed UPN)
```

## Table 1 — `page_views.csv` (v0.1.0+)

**Grain**: one row per `(workspace_id, report_id, page_id, view_date)`.
A page viewed by 12 people on the same day is one row with `view_count = 12`.

**Use for**: page-level fact analytics, time-series, dwell time, hot pages.

| Column | Type | Required | PII | Source | Description | Example |
|---|---|---|---|---|---|---|
| `workspace_id` | `string` (GUID) | Yes | No | `GET /v1.0/myorg/groups` `id` | Power BI workspace GUID. Stable. | `aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee` |
| `workspace_name` | `string` | Yes | No | `GET /v1.0/myorg/groups` `name` | Workspace display name. May change. Use `workspace_id` for joins. | `Clinical-Ops-Production` |
| `capacity_name` | `string` | No | No | `GET /v1.0/myorg/admin/capacities` `displayName` | Premium/Fabric capacity backing the workspace. Empty string if Pro. | `F64-East-US-2` |
| `report_id` | `string` (GUID) | Yes | No | `GET /v1.0/myorg/groups/{wsId}/reports` `id` | Report GUID. Stable. | `11111111-2222-3333-4444-555555555555` |
| `report_name` | `string` | Yes | No | `GET .../reports` `name` | Report display name. May change. | `Trial Enrollment Funnel` |
| `report_total_pages` | `int32` | Yes | No | Mock: count of distinct `page_id` in the report. Live: enriched from `page_catalog` join in gold. | Total pages in the report at collection time. | `14` |
| `page_id` | `string` | Yes | No | `'Report page views'[Report page id]` | Page section name — stable across renames. (Power BI calls this `ReportSection1` etc., not a GUID.) | `ReportSection47` |
| `page_name` | `string` | Yes | No | `'Report page views'[Report page]` | Page display name at collection time. | `Site Performance` |
| `page_ordinal` | `int32` | Yes | No | Mock: 1-based position within the report. Live: enriched from `page_catalog`. | Page position. | `3` |
| `view_date` | `string` (ISO-8601 `YYYY-MM-DD`) | Yes | No | `'Report page views'[Date]` truncated to day | Date the views happened, in the **Power BI service's tenant time zone**. | `2026-05-27` |
| `view_count` | `int32` | Yes | No | `'Report page views'[Views]` | Number of page-view events that day. | `47` |
| `unique_users` | `int32` | Yes | No | `[UniqueUsers]` measure (`DISTINCTCOUNT([User])`) | Distinct user UPNs for that page/day. | `19` |
| `avg_dwell_seconds` | `float64` | Yes | No | `[AvgDwellSeconds]` measure | Average time on page in seconds. | `42.7` |
| `top_persona` | `string` | No | No | Live: empty. Mock: simulated category. | Reserved for downstream enrichment (e.g. department lookup against a People semantic model). | `Clinical Operations` |

## Table 2 — `page_catalog.csv` (NEW in v0.3.0)

**Grain**: one row per `(workspace_id, report_id, page_id)`.
**Current-state**: this is a snapshot of every page that EXISTS at
collection time — including pages with **zero views** (the whole point).

**Use for**: unused-page detection (`page_catalog LEFT JOIN page_views`
WHERE `page_views.page_id IS NULL` = unused), page roster, ordinals.

**Source**: `GET /v1.0/myorg/groups/{ws}/reports/{rep}/pages` — a
documented, GA Power BI REST endpoint (NOT preview, NOT DAX). The
service principal needs workspace membership (same as `executeQueries`).
Paginated reports return 403/404 on this endpoint and are silently
skipped (logged to `_run_summary.json["errors"]`).

| Column | Type | Required | PII | Source | Description | Example |
|---|---|---|---|---|---|---|
| `workspace_id` | `string` (GUID) | Yes | No | `GET /groups` | Workspace GUID. | `aaaaaaaa-...` |
| `workspace_name` | `string` | Yes | No | `GET /groups` | Workspace display name. | `Clinical-Ops-Production` |
| `report_id` | `string` (GUID) | Yes | No | `GET /groups/{ws}/reports` | Report GUID. | `11111111-...` |
| `report_name` | `string` | Yes | No | `GET /groups/{ws}/reports` | Report display name. | `Trial Enrollment Funnel` |
| `page_id` | `string` | Yes | No | `GET .../pages` `name` | Page section name (matches `page_views.page_id`). | `ReportSection47` |
| `page_name` | `string` | Yes | No | `GET .../pages` `displayName` | Page display name at collection time. | `Protocol v1 (legacy)` |
| `page_ordinal` | `int32` | Yes | No | `GET .../pages` `order` | 0-based or 1-based position within the report (whatever the REST API returns). | `47` |
| `catalog_pulled_at` | `string` (ISO-8601 with TZ) | Yes | No | `datetime.now(tz=UTC)` at run time | When this catalog row was pulled. Used to detect stale catalogs / rebuild order. | `2026-05-27T18:51:19+00:00` |

## Table 3 — `report_views.csv` (NEW in v0.3.0)

**Grain**: one row per `(workspace_id, report_id, view_date)`.

**Use for**: report-level KPIs (parity with the existing per-report
Usage Metrics report), session counts, average session time. Combine
with `page_views` to compute `[Pages Per Session] =
SUM(page_views.view_count) / SUM(report_views.view_count)`.

**Source**: `'Report views'` table in the same Modern Usage Metrics
semantic model. Same DAX path (`executeQueries`), different table.

| Column | Type | Required | PII | Source | Description | Example |
|---|---|---|---|---|---|---|
| `workspace_id` | `string` (GUID) | Yes | No | `GET /groups` | Workspace GUID. | `aaaaaaaa-...` |
| `workspace_name` | `string` | Yes | No | `GET /groups` | Workspace display name. | `Clinical-Ops-Production` |
| `capacity_name` | `string` | No | No | `GET /admin/capacities` | Capacity backing the workspace. | `F64-East-US-2` |
| `report_id` | `string` (GUID) | Yes | No | `GET /groups/{ws}/reports` | Report GUID. | `11111111-...` |
| `report_name` | `string` | Yes | No | `GET /groups/{ws}/reports` | Report display name. | `Trial Enrollment Funnel` |
| `view_date` | `string` (ISO-8601 `YYYY-MM-DD`) | Yes | No | `'Report views'[Date]` | Date in tenant TZ. | `2026-05-27` |
| `view_count` | `int32` | Yes | No | `'Report views'[Views]` | Report-level session count. Always ≥ max single-page count for the same day. | `90` |
| `unique_users` | `int32` | Yes | No | `[UniqueUsers]` measure on report-grain | Distinct UPNs for that report/day. | `12` |
| `avg_session_seconds` | `float64` | Yes | No | `[Average view time]` measure | Average report-session length in seconds (NOT per-page dwell). | `127.4` |

> **Mock-mode caveat**: `MockAdapter` synthesizes report-views by
> aggregating page-views (`SUM(view_count)` for the report, MAX of
> per-page `unique_users` as a lower bound, weighted-average of
> page dwells as a proxy for `avg_session_seconds`). Live mode reads
> the real measures from `'Report views'`.

## Table 4 — `user_views.csv` (NEW in v0.3.0)

**Grain**: one row per `(workspace_id, report_id, user_id_hash, view_date)`.

**Use for**: user-level analytics: power-user detection, % of users
who touch >1 page, average pages per user, capacity SKU sizing.

**Source**: same `'Report page views'` DAX, but GROUPed by `[User]`
instead of `[Report page]`, then `[User]` is **hashed at the
collector** before landing in silver. Raw UPNs never leave the
collector process — see [`pii-and-retention.md`](pii-and-retention.md).

| Column | Type | Required | PII | Source | Description | Example |
|---|---|---|---|---|---|---|
| `workspace_id` | `string` (GUID) | Yes | No | `GET /groups` | Workspace GUID. | `aaaaaaaa-...` |
| `workspace_name` | `string` | Yes | No | `GET /groups` | Workspace display name. | `Clinical-Ops-Production` |
| `report_id` | `string` (GUID) | Yes | No | `GET /groups/{ws}/reports` | Report GUID. | `11111111-...` |
| `report_name` | `string` | Yes | No | `GET /groups/{ws}/reports` | Report display name. | `Trial Enrollment Funnel` |
| `user_id_hash` | `string` (16 hex chars) | Yes | **Pseudonymised** | SHA-256 of `lower(strip(UPN))`, first 16 hex chars | Stable per-tenant pseudonym. Same user = same hash forever. **NOT** reversible to UPN by anyone outside the tenant. Empty for system jobs / refresh-on-publish. | `7b8c8f3a1e4d9a02` |
| `view_date` | `string` (ISO-8601 `YYYY-MM-DD`) | Yes | No | `'Report page views'[Date]` | Date in tenant TZ. | `2026-05-27` |
| `view_count` | `int32` | Yes | No | `SUM('Report page views'[Views])` | Total pages this user opened in this report that day. | `8` |
| `distinct_pages_viewed` | `int32` | Yes | No | `DISTINCTCOUNT('Report page views'[Report page id])` | How many unique pages this user touched (≤ `view_count`). | `5` |

> **Hashing rationale**: 16 hex = 64 bits. Birthday-bound collision
> probability for 1M users is ~2.7×10⁻⁸. Brute-forcing the preimage
> requires a known plaintext oracle — i.e. an adversary already
> inside your tenant, in which case they have the UPNs anyway.

## Run summary — `_run_summary.json`

```json
{
  "schema_version": "1.1.0",
  "started_at": "2026-06-02T18:51:19+00:00",
  "ended_at":   "2026-06-02T18:51:20+00:00",
  "since": "2026-02-27",
  "until": "2026-05-27",
  "workspaces": 5,
  "reports": 15,
  "datasets": 15,
  "page_view_rows": 15480,
  "page_catalog_rows": 232,
  "report_view_rows": 1350,
  "user_view_rows": 6289,
  "unused_pages": 10,
  "reports_with_unused_pages": 3,
  "rows": 15480,                          ← v0.2.x back-compat alias of page_view_rows
  "reports_skipped_no_bootstrap": 0,
  "workspaces_not_bootstrapped": [],
  "errors": [],
  "silver_paths": {                       ← v0.3.0 — all 4 silver files
    "page_views":   "out/silver/page_views.csv",
    "page_catalog": "out/silver/page_catalog.csv",
    "report_views": "out/silver/report_views.csv",
    "user_views":   "out/silver/user_views.csv"
  },
  "silver_path": "out/silver/page_views.csv",   ← v0.2.x back-compat alias
  "bronze_partition": "out/bronze/dt=2026-05-27"
}
```

The `rows` and `silver_path` keys are aliases preserved from v0.2.x so
any v0.2.x dashboard or alerting that reads the summary keeps working.
New consumers should prefer `page_view_rows` and `silver_paths`.

## Reading the silver CSV

```python
import csv

with open("silver/page_views.csv", "r", encoding="utf-8") as f:
    first = f.readline()
    assert first.startswith("# silver_schema_version=1.0.0"), \
        "schema mismatch — regenerate gold artifacts"
    # Don't seek back — DictReader picks up from where we left off.
    rows = list(csv.DictReader(f))
```

```sql
-- Fabric / Spark via SQL endpoint
SELECT * FROM page_views_silver
WHERE view_date >= current_date() - INTERVAL 30 DAYS;
```

```dax
// Power BI (after pointing the .pq template at the silver Delta table)
Total Views = SUM('page_views_silver'[view_count])
```

## Schema evolution policy

We follow [Semantic Versioning](https://semver.org/) for
`SILVER_SCHEMA_VERSION`:

- **PATCH** (`1.0.0` → `1.0.1`): no schema change; cosmetic /
  documentation / performance only.
- **MINOR** (`1.0.0` → `1.1.0`): a new column is added at the end, or
  an optional column becomes populated. **Old readers keep working.**
- **MAJOR** (`1.0.0` → `2.0.0`): a column is renamed, removed, or
  changes type. **Old readers will fail the assertion.**

When introducing a breaking change, ship a migration note in
`CHANGELOG.md` and bump `EXPECTED_SCHEMA_VERSION` in
`deploy/fabric-notebook/PageTelemetryCollector.Notebook.py`.
