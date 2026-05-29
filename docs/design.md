# Design notes

Why this reference implementation looks the way it does — the
architectural decisions that aren't obvious from the code, and the
alternatives we considered and rejected.

## 1. Why a custom collector at all?

Power BI's first-party telemetry stops at the **report** grain. The
admin-API `Get Activity Events`, the audit log, and the
[Microsoft 365 admin "Activity"](https://learn.microsoft.com/power-bi/admin/service-admin-portal-usage-metrics)
view all log `ViewReport` events without a page identifier. The only
place page granularity exists is inside the per-workspace
**Modern Usage Metrics** (preview) semantic model that Power BI
auto-provisions when a workspace admin or contributor clicks
`... → View usage metrics report` on any report in that workspace, once.
After the click the model accumulates page-level rows for **every**
report in the workspace into a single `Usage Metrics Report` semantic
model, refreshed daily by Microsoft. There is **no public REST API**
that does this provisioning or queries the model in bulk — confirmed by
Power BI PM David Browne at the May 2026 HLS Roundtable:

> *"That's this: Monitor Usage Metrics in Power BI Workspaces (preview).
> ... And we don't have an API, but it builds a semantic model you can
> access. But the scanner API doesn't have page-level activity events."*

So a customer needs three things first-party doesn't give them:
**enumerate** every workspace via admin REST, **discover** the
per-workspace Usage Metrics model (and surface workspaces that haven't
been bootstrapped yet for an admin to click), and **query** all the
discovered models on a schedule. That's exactly the collector.

## 1a. (HISTORICAL) Why this collector previously tried `POST /admin/reports/{id}/usageMetrics`

In v0.1.0 this collector attempted to call
`POST /v1.0/myorg/admin/reports/{id}/usageMetrics` to idempotently
provision the per-report Usage Metrics v2 dataset. **That REST
endpoint does not exist in the public Power BI API surface.** The
"View usage metrics report" button in the portal is a portal-internal
action; the public REST surface (per Microsoft Learn
`/rest/api/power-bi/admin`) exposes only Get / GetInGroup /
GetSubscriptions / GetUsers operations on admin Reports.

v0.2.0 corrects this by reading the per-workspace Modern Usage Metrics
semantic model (created lazily on the first portal click) instead.
Workspaces that haven't been bootstrapped surface in
`_run_summary.json → workspaces_not_bootstrapped` and are skipped, so
an admin can do the one-time click in bulk and re-run.

## 2. Why REST `executeQueries` instead of XMLA / ADOMD.NET by default?

ADOMD.NET was the original obvious choice — XMLA is *the* DAX query
endpoint and the Power BI documentation steers you there. But:

- ADOMD.NET is a Windows-only native DLL pair. Linux Azure Functions
  can't load it. Fabric Spark sessions can't load it. Customers on
  macOS dev boxes can't load it. We'd be telling everyone "use the
  Premium-feature endpoint from Windows," which is most of our
  customer base's least-favorite combination.
- `pyadomd` wraps it via Python.NET — adds another Windows-only build
  dep, mono on Linux (which is brittle), and a much larger image.
- The REST `executeQueries` endpoint accepts arbitrary DAX, returns
  JSON, and has zero native deps. It's been GA since 2021.

So `executeQueries` is the default. The pyadomd path is preserved
behind `PBI_USE_PYADOMD=1` for the very-large-dataset case (REST has a
100,000-row response cap; XMLA streams).

## 3. Why `executeQueries` per dataset, not `executeQueries` against
the Admin Monitoring lakehouse?

Microsoft ships an **Admin Monitoring** workspace in every tenant that
contains a "Feature Usage and Adoption" semantic model with tenant-wide
data. Tempting alternative. We rejected it because:

- Page-level tables are intentionally excluded — that workspace
  surfaces report-level, capacity-level, and dataset-level only.
- Even if a future schema added pages, the model is owned by Microsoft
  and changes without notice; a customer pipeline pinned to it would
  break.
- The per-report `Usage Metrics` model is documented, stable, and the
  page-level data has been there since v2 launched in 2019.

## 4. Why MERGE (upsert) into silver, not insert-only / append?

Page-view rows for a given `(workspace_id, report_id, page_id, view_date)`
are **eventually-consistent** in the Usage Metrics dataset. A page
viewed late in the day might not show up in that day's row until the
next morning's refresh of the Usage Metrics dataset itself
(Microsoft-owned, runs every ~24 hours). If we `INSERT` on every
collector run we get duplicates; if we `DELETE before INSERT` we lose
history that's already been correctly counted.

`MERGE` on the natural key `(workspace_id, report_id, page_id, view_date)`:

- Inserts new rows
- Updates `view_count` / `unique_users` / `avg_dwell_seconds` in place
  when the upstream value moves
- Leaves untouched rows untouched

This is also the cheapest path on Delta — a `MERGE INTO` with no
side effects on the unmatched rows skips data-file rewrites.

## 5. Why Managed Identity (not service principal with secret) on the Function path?

The Function deploy option provisions an Azure Function and grants its
**Managed Identity** the Power BI admin permissions and the Storage
Blob Data Contributor role on the output container. The Function never
sees a secret.

Service-principal-with-secret is supported (set `PBI_CLIENT_SECRET`)
because the **Fabric notebook** and **local** deploy options can't use
Managed Identity directly:

- Fabric notebooks run with a **workspace identity** by default, which
  is functionally equivalent but configured differently — the notebook
  pulls the token via `notebookutils.credentials.getToken("pbi")`.
- The local deploy assumes a developer laptop with no MSI, so we use
  Microsoft.PowerShell.SecretManagement to gate the secret behind the
  OS credential store (Windows Credential Manager / macOS Keychain /
  Linux Secret Service).

In all three options the secret never lands on disk in plaintext, and
in the Function option there is no secret at all.

## 6. Why CSV in silver, not Parquet?

Three reasons:

1. **Compatibility**: CSV is readable by anything — Spark, Power BI
   Desktop direct-query, SQL Server `OPENROWSET`, `pandas`, `awk`.
   Parquet requires a library on the reader side.
2. **Diff-friendly for tiny data**: a tenant of 50 reports produces
   ~50 KB of silver/day. Tiny. CSV's overhead is irrelevant at this
   scale, and a 50 KB diff is grep-able when investigating.
3. **The interesting performance work is in the gold layer**, not the
   silver layer. Most customers point a Fabric Spark MERGE at the
   silver CSV to produce a real Delta `page_views_silver` table — that
   Delta table is the actual high-performance read surface.

When tenant scale grows past ~10,000 reports the bronze partitions
will spill into the GB range; at that point switch the
`_write_rows` writer to `pyarrow.parquet.write_table`. That's a
20-line change and the schema is unchanged.

## 7. Why the adapter pattern (`LiveAdapter` + `MockAdapter`)?

The adapter pattern is overkill for two implementations… until you
remember:

- **CI**: The CI pipeline must run end-to-end without a Power BI
  tenant. `MockAdapter` makes the mock-mode regression test a real
  customer-facing reproducibility contract, not a stub.
- **Customer onboarding**: Every customer eval starts with "show me the
  dashboard without me having to deploy anything." `--mock` ships
  that experience.
- **Debugging at the customer**: when a customer hits a live-mode bug
  they can usually reproduce it offline by recording the failing DAX
  response into a fixture and adding a third adapter for it.

The cost is one extra class. Worth it.

## 8. Why hardcoded healthcare names in the bundled sample data?

The collector was originally built for a healthcare-pharma customer
(Incyte Genomics). The detailed clinical-trial / RWE / commercial /
manufacturing naming was retained because it makes the **underused
pages** narrative more visceral — "Phase III STUDY-101 has 9 pages no
one's opened in 90 days" lands harder than "Report A has 9 pages no
one's opened in 90 days."

For other industries, `python etl/generate_sample_data.py --theme generic`
and `--theme financial` re-roll the sample data with parallel structure
but renamed workspaces, reports, and pages. The numerical reproducibility
contract only applies to the default `healthcare` theme.

## 9. Why pin the Fabric notebook to a release tag, not `main`?

A Fabric notebook in production downloading `collector.py` from
`raw.githubusercontent.com/<owner>/<repo>/main/etl/collector.py` is a
supply-chain risk: a maintainer push to `main` immediately changes
behavior in every customer's production environment. So `COLLECTOR_REF`
defaults to `v0.1.0` (or the latest stable tag), the Lakehouse
caches the downloaded file at `Files/_cache/collector.<ref>.py`, and
the notebook asserts `SILVER_SCHEMA_VERSION` matches its expected
version before using it.

A determined attacker can still force-push to a tag, but that requires
write to the repo. Pin-to-tag plus schema-version assertion is the
right balance between operational agility ("we want the customer to be
able to pull our bugfix without a Fabric deploy") and supply-chain
caution.

## 10. Why a static HTML dashboard, not Power BI?

The bundled dashboard is **demo-ware**, deliberately. Its job is to
make the underused-page narrative tangible in the first five minutes
of a customer conversation, without requiring a Power BI workspace or
service principal — `start dashboard\PageUsageDashboard.html` is the
"hello world."

The real customer-facing analytics surface is the [Power BI template
in `dashboard/PowerBI/`](../dashboard/PowerBI/) — Power Query M to
connect to silver (Fabric Lakehouse Delta, Azure Blob CSV, or local
file), DAX measures, RLS pattern. Customers paste that into their own
Power BI Desktop and ship a real report in their own tenant.

## 11. What we considered and rejected

| Idea | Why rejected |
|---|---|
| Pull from Activity Events log | No page-level granularity. We'd ship the wrong thing. |
| Use the Admin scanner API directly | Returns metadata, not telemetry. |
| Build on top of Microsoft Purview audit log | PII-elevated surface, organization-wide retention policy mismatch, much heavier auth. |
| Make collector a Spark job natively | Locks customers into Fabric. Plain Python keeps the offering portable to Azure Functions, Container Apps, a developer laptop, or even an on-prem cron. |
| Ship a binary `.pbit` template | Format is brittle outside Desktop, can't be diffed, can't be cleanly version-pinned. Ship `.pq` + `.dax` text instead. |
| Use Azure Data Factory native Power BI activity | The activity invokes a refresh; it does not query a dataset. Wrong tool. |
| Use Microsoft Graph for telemetry | Graph does not expose Power BI per-page metrics. |
