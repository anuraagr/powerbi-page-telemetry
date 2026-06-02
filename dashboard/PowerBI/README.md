# Power BI template — Page Telemetry semantic model  (v0.3.0)

Three small assets you can paste into Power BI Desktop or Tabular
Editor to stand up a real Power BI report on top of the collector
output — no Power BI Desktop file needed, so nothing binary in the repo.

| File | Purpose |
|---|---|
| [`PageTelemetry.Connect.pq`](PageTelemetry.Connect.pq) | A Power Query M script. Reads **all 4 silver tables** (`page_views`, `page_catalog`, `report_views`, `user_views`) from one of three sources: Fabric Lakehouse, Azure Blob CSVs, or local files. Handles the `# silver_schema_version=...` preamble. |
| [`PageTelemetry.Measures.dax`](PageTelemetry.Measures.dax) | DAX measures: headline KPIs, **unused-page detection** (LEFT-JOIN-is-null), underused detection, report-level rollups, user analytics (hashed UPNs), time-series, capacity attribution, page-ordinal funnel. |
| **Recommended dashboard layout** | See [§ Recommended visuals](#recommended-visuals) below — 5 pages: Overview, Report Drill, Page Drill, **Unused Pages**, User Analytics. |

## What's new in v0.3.0

The collector now emits **four** silver tables (was 1 in v0.2.x), all
co-located in `silver/`:

| Table | Grain | Source DAX |
|---|---|---|
| `page_views` | `(workspace, report, page, date)` | `'Report page views'` (preview) |
| `page_catalog` | `(workspace, report, page)` — every page that EXISTS | `GET /v1.0/myorg/groups/{ws}/reports/{rep}/pages` |
| `report_views` | `(workspace, report, date)` | `'Report views'` (preview) |
| `user_views` | `(report, hashed_user, date)` | `'Report page views'` GROUP BY `[User]` |

The schema bumps to **`1.1.0`** (additive — old v0.2.x readers of
`page_views.csv` keep working).

The headline new measure is `[Unused Pages]` — pages that EXIST in
the catalog but have ZERO matching rows in `page_views`. This is what
Jon at Incyte asked for and what the page-only model in v0.2.x
couldn't answer.

## 30-second quickstart (local CSV)

1. Run the collector at least once:
   ```powershell
   python etl/collector.py --mock --out C:\PowerBI-PageTelemetry\out
   ```
2. Open **Power BI Desktop** → **Home** → **Get Data** → **Blank Query**
   → **Advanced Editor**.
3. Paste the entire contents of `PageTelemetry.Connect.pq`. Edit the
   parameters at the top:
   - `ConnectionMode = "local"`
   - `Local_SilverDir = "C:\PowerBI-PageTelemetry\out\silver"`
4. Click **Done**. The query returns a **record** with 4 tables.
   In the editor, **right-click → Convert to Table → To Table**, then
   click the expand button on the `Value` column and load all 4.
5. Click **Close & Apply**. Power BI loads 4 queries:
   `page_views`, `page_catalog`, `report_views`, `user_views`.
6. **Model view** → add a calculated column on **both** `page_views`
   and `page_catalog`:
   ```dax
   page_key = [workspace_id] & "|" & [report_id] & "|" & [page_id]
   ```
   Hide both (right-click → Hide in report view). Create a
   single-direction relationship `page_views[page_key] *-> page_catalog[page_key]`.
7. **Model view** → for each measure in `PageTelemetry.Measures.dax`,
   **New measure** → paste → Enter. Pin to the `page_views` table.
8. Build the recommended 5-page layout below.

## Fabric Lakehouse mode

Same flow, but in step 3 use:

```
ConnectionMode       = "fabric"
Fabric_WorkspaceName = "Analytics-Ops"
Fabric_LakehouseName = "PageTelemetry_LH"
```

The Fabric notebook (`deploy/fabric-notebook/`) lands all 4 Delta
tables under `<lakehouse>/Tables/`. The Power Query script reads them
through the Fabric connector.

## Azure Blob mode (Azure Function deploy option)

```
ConnectionMode  = "blob"
Blob_AccountUrl = "https://<account>.blob.core.windows.net"
Blob_Container  = "page-telemetry"
```

The 4 file paths default to `silver/page_views.csv`,
`silver/page_catalog.csv`, etc. Power BI Desktop will prompt for
credentials — pick **OAuth (your AAD account)** if your account has
Storage Blob Data Reader on the container.

## Why not a `.pbit` file?

Constructing a binary `.pbit` outside of Power BI Desktop is brittle
(the format mixes XML, JSON, and a compressed model) and the file
would have to be regenerated every time the schema changes. Shipping
the Power Query M and the DAX as source is portable, diff-friendly,
and forces a deliberate paste action — which is the right behavior
for governance-conscious tenants who don't auto-trust binary BI
artifacts from GitHub.

If you do want a binary `.pbit` to share with end users, build it
yourself in Desktop after pasting the scripts above, then
**File → Export → Power BI template (.pbit)**.

## Recommended visuals

### Page 1 — **Overview**
- KPI cards: `[Total Views]`, `[Unique Pages Defined]`,
  `[Unique Reports]`, `[Unique Users]`, `[Unused Pages]`,
  `[% Pages Unused]`, `[WoW Change %]`
- Line chart: `view_date` × `[Total Views]` and `[Report Views]`
  side-by-side (shows the page-level signal the report-only API misses)
- Bar chart: `workspace_name` × `[Total Views]`
- Donut: `capacity_name` × `[Total Views]`

### Page 2 — **Report Drill** (Bookmark off Overview's workspace bar)
- Matrix: `report_name` rows × KPI columns
  (`[Total Views]`, `[Pages Per Session]`, `[Avg Session Seconds]`,
  `[Top Page Share]`, `[Unused Pages]`)
- Conditional format `[Unused Pages]`: red ≥ 5, yellow 1-4, blank if 0
- Line chart: `page_ordinal` × `[Cumulative Views at Page Ordinal]`
  faceted by `report_name`

### Page 3 — **Page Drill** (drillthrough from Report Drill)
- Table: every page in the selected report, sorted by `view_count` desc.
  Columns: `page_ordinal`, `page_name`, `view_count`, `unique_users`,
  `avg_dwell_seconds`. Page-level signal the report-only API can't show.
- Sparkline: `view_date` × `[Total Views]` for the selected page

### Page 4 — **Unused Pages** ← Jon's headline page
- Cards: `[Unused Pages]`, `[Reports With Unused Pages]`,
  `[% Pages Unused]`
- Table from `page_catalog` (NOT page_views — that's the whole point):
  `workspace_name`, `report_name`, `page_name`, `page_ordinal`.
  Filter to pages where `[Total Views] = BLANK()` or use a
  `IsUnused = IF([Total Views] = BLANK(), 1, 0)` flag.
- This is the work list to take to report owners.

### Page 5 — **User Analytics**
- Cards: `[Unique Users]`, `[Power Users]`, `[Avg Pages Per User]`
- Table: top 25 `user_id_hash` by `[Total Views]` — note the hash is
  **not reversible** so this is a power-user *signal*, not user names.
  Pair with a separate, **governed** "people" semantic model if you
  want to join hash → display name behind RLS.

## Headline numbers (mock data)

After `python etl/collector.py --mock`, the v0.3.0 silver layer holds:

| Table | Rows |
|---|---|
| `page_views.csv` | 15,480 |
| `page_catalog.csv` | 232 |
| `report_views.csv` | 1,350 |
| `user_views.csv` | 6,289 |

And the dashboard's headline cards will read:

- **10 unused pages** across **3 clinical-trial reports** — the
  intentional dead-page overlay in `etl/sample_data/unused_pages.json`
  (names like "Protocol v1 (legacy)", "DEBUG: per-site raw rates",
  "Enrollment funnel (deprecated)")
- **231** pages defined / **221** pages viewed
- **154,815** total views
