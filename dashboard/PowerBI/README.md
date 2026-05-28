# Power BI template — Page Telemetry semantic model

Two small assets you can paste into Power BI Desktop or Tabular Editor
to stand up a real Power BI report on top of the collector output —
no Power BI Desktop file needed, so nothing binary in the repo.

| File | Purpose |
|---|---|
| [`PageTelemetry.Connect.pq`](PageTelemetry.Connect.pq) | A Power Query M script. Reads the silver layer from one of three sources: Fabric Lakehouse Delta table, Azure Blob CSV, or local file. Handles the `# silver_schema_version=...` preamble. |
| [`PageTelemetry.Measures.dax`](PageTelemetry.Measures.dax) | DAX measures: KPIs, underused-page detection, time-series, capacity attribution, page-ordinal funnel. |

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
   - `Local_CsvPath  = "C:\PowerBI-PageTelemetry\out\silver\page_views.csv"`
4. Click **Done** → **Close & Apply**.
5. Open **Model view** → for each block in
   `PageTelemetry.Measures.dax`, **New measure** → paste → Enter.
6. Drag `view_date` to a chart axis and `[Total Views]` to values.
   Add a slicer on `workspace_name`.

## Fabric Lakehouse mode

Same flow, but in step 3 use:

```
ConnectionMode       = "fabric"
Fabric_WorkspaceName = "Analytics-Ops"      // your Fabric workspace
Fabric_LakehouseName = "PageTelemetry_LH"   // lakehouse created by the notebook
Fabric_TableName     = "page_views_silver"  // table written by the notebook
```

If you used the **fabric-notebook** deploy option, the notebook writes
the Delta table to `<lakehouse>/Tables/page_views_silver` and the
PowerQuery script above reads it directly through the Fabric connector.

## Azure Blob mode (Azure Function deploy option)

In step 3 use:

```
ConnectionMode  = "blob"
Blob_AccountUrl = "https://<account>.blob.core.windows.net"
Blob_Container  = "page-telemetry"
Blob_FilePath   = "silver/page_views.csv"
```

Power BI Desktop will prompt for credentials. Pick **Account Key**,
**OAuth (your AAD account)**, or **SAS** depending on how the storage
account is gated. The Function writes the CSV via Managed Identity, so
your account needs **Storage Blob Data Reader** on the container.

## Why not a `.pbit` file?

Constructing a binary `.pbit` outside of Power BI Desktop is brittle
(the format mixes XML, JSON, and a compressed model) and the file would
have to be regenerated every time the schema changes. Shipping the
Power Query M and the DAX as source is portable, diff-friendly, and
forces a deliberate paste action — which is the right behavior for
governance-conscious tenants who don't auto-trust binary BI artifacts
from GitHub.

If you do want a binary `.pbit` to share with end users, build it
yourself in Desktop after pasting the two scripts above, then **File →
Export → Power BI template (.pbit)**.

## Recommended visuals

Page 1 — **Executive summary**:
- KPI cards: `[Total Views]`, `[Unique Pages]`, `[% Pages Never Viewed]`,
  `[Underused Pages]`, `[WoW Change %]`
- Line chart: `view_date` × `[Total Views]`
- Bar chart: `workspace_name` × `[Total Views]`

Page 2 — **Underused-page hit list**:
- Table: `workspace_name`, `report_name`, `page_name`, total
  `view_count`, sorted ascending. Add a measure-driven conditional
  format on `view_count` so zero-view pages turn red.

Page 3 — **Long-report funnel**:
- Line chart: `page_ordinal` × `[Total Views]` faceted by
  `report_name`. Pages where the line drops off a cliff are
  candidates for trimming.

Page 4 — **Capacity / cost attribution**:
- Pie/donut: `capacity_name` × `[Total Views]`
- Helps justify Fabric SKU sizing.
