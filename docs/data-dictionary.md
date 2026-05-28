# Silver layer data dictionary

This is the contract between the collector and any downstream
consumer — gold semantic model, ad-hoc SQL, the bundled dashboard,
or a custom Power BI report.

**Schema version**: `1.0.0` (matches `SILVER_SCHEMA_VERSION` in
[`etl/collector.py`](../etl/collector.py)). The version is emitted as
the first line of `silver/page_views.csv` as
`# silver_schema_version=1.0.0` and in `_run_summary.json`. Downstream
readers should assert on it before reading.

**Source of truth**: the Power BI per-report Usage Metrics dataset,
specifically the `Report page views` table, joined with workspace and
report enumeration from the Power BI REST admin / groups APIs.

**Grain**: one row per `(workspace_id, report_id, page_id, view_date)`.
A page viewed by 12 people on the same day is one row with
`view_count = 12`.

## Columns

| Column | Type | Required | PII | Source | Description | Example |
|---|---|---|---|---|---|---|
| `workspace_id` | `string` (GUID) | Yes | No | `GET /v1.0/myorg/groups` `id` | Power BI workspace GUID. Stable. | `aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee` |
| `workspace_name` | `string` | Yes | No | `GET /v1.0/myorg/groups` `name` | Workspace display name. May change. Use `workspace_id` for joins. | `Clinical-Ops-Production` |
| `capacity_name` | `string` | No | No | `GET /v1.0/myorg/admin/capacities` `displayName` | Premium/Fabric capacity backing the workspace. Empty string if Pro. | `F64-East-US-2` |
| `report_id` | `string` (GUID) | Yes | No | `GET /v1.0/myorg/groups/{wsId}/reports` `id` | Report GUID. Stable. | `11111111-2222-3333-4444-555555555555` |
| `report_name` | `string` | Yes | No | `GET .../reports` `name` | Report display name. May change. | `Trial Enrollment Funnel` |
| `report_total_pages` | `int32` | Yes | No | Live: zero (not in DAX). Mock: count of distinct `page_id` in the report. | Total pages in the report at collection time. **Live deploys**: enriched downstream via a `Report pages` DAX query (planned for v0.2.0). | `14` |
| `page_id` | `string` (GUID) | Yes | No | `'Report page views'[Report page id]` | Page section GUID — stable across renames. | `66666666-7777-8888-9999-aaaaaaaaaaaa` |
| `page_name` | `string` | Yes | No | `'Report page views'[Report page]` | Page display name at collection time. | `Site Performance` |
| `page_ordinal` | `int32` | Yes | No | Live: zero. Mock: 1-based position within the report. | Page position. **Live deploys**: enriched downstream when `report_total_pages` is enriched. | `3` |
| `view_date` | `string` (ISO-8601 `YYYY-MM-DD`) | Yes | No | `'Report page views'[Date]` truncated to day | Date the views happened, in the **Power BI service's tenant time zone**. | `2026-05-27` |
| `view_count` | `int32` | Yes | No | `'Report page views'[Views]` | Number of page-view events that day. | `47` |
| `unique_users` | `int32` | Yes | No | `[UniqueUsers]` measure (`DISTINCTCOUNT([User]))` | Distinct user UPNs for that page/day. | `19` |
| `avg_dwell_seconds` | `float64` | Yes | No | `[AvgDwellSeconds]` measure | Average time on page in seconds. Power BI computes this from open / close events. | `42.7` |
| `top_persona` | `string` | No | No | Live: empty. Mock: simulated category. | Reserved for downstream enrichment (e.g. department lookup against a People semantic model). Optional. | `Clinical Operations` |

> **Note on `report_total_pages` / `page_ordinal` / `top_persona`**:
> the live `executeQueries` DAX path queries the `Report page views`
> table directly, which does not contain page ordering metadata. The
> sample data generator populates them so the bundled dashboard
> renders nicely. The fields are kept in the schema so v0.2.0 can
> populate them in live mode from a second DAX query
> (against `Report pages`) without breaking the schema contract.

## File layout

```
out/
├── _run_summary.json          ← {schema_version, workspaces, reports, rows, errors, bronze_partition}
├── bronze/
│   └── dt=2026-05-27/         ← partitioned by collection date
│       ├── <wsId>__<reportId>.csv
│       ├── ...
└── silver/
    └── page_views.csv         ← first line is "# silver_schema_version=1.0.0"
```

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
