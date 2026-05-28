# Deploy as a Fabric notebook + Data Pipeline

This is the **recommended production deployment** for tenants that already use
Microsoft Fabric. It runs the collector daily inside Fabric, lands the silver
CSV in your governance Lakehouse, and promotes it to a Delta table that
DirectLake reports can read with zero ETL latency.

## What's in this folder

| File | What it is |
| --- | --- |
| `PageTelemetryCollector.Notebook.py` | Fabric-notebook-format Python source. Import this into Fabric as a Notebook. |
| `data-pipeline.json` | Fabric Data Pipeline definition — single Notebook activity, daily 02:00 UTC schedule, 2 retries. |
| `README.md` | This file. |

## Prerequisites

1. **Fabric capacity** (F-SKU or Premium P-SKU) with the **XMLA endpoint** set
   to **Read** on the capacity hosting your reports.
2. **Tenant setting** `Service principals can use Power BI APIs` enabled for a
   security group.
3. **Service principal** with one of:
   - `Tenant.Read.All` on the Power BI service (lowest-privilege path), OR
   - `Fabric Administrator` (gives admin REST + XMLA on every workspace).
4. **Azure Key Vault** holding three secrets, accessible from the Fabric
   workspace running this notebook:
   - `pbi-tenant-id`
   - `pbi-client-id`
   - `pbi-client-secret`
5. **Lakehouse** named `PageTelemetry` in the same workspace as the notebook
   (you can rename — just update `dependencies.lakehouse.default_lakehouse_name`
   at the top of the notebook).

See [`../../docs/deployment-guide.md`](../../docs/deployment-guide.md) for the
full prereq walkthrough including service-principal creation.

## Step-by-step

### 1. Import the notebook

In Fabric → your workspace → **+ New** → **Import notebook** → upload
`PageTelemetryCollector.Notebook.py`.

After import, open the notebook and:

- Edit the **`KEYVAULT_URL`** constant in the first cell to point at your Key
  Vault: `https://<your-vault>.vault.azure.net/`
- Attach your `PageTelemetry` lakehouse via the **Lakehouses** pane (the
  notebook's default-lakehouse metadata will auto-bind).
- Run all cells once manually to confirm:
  - Key Vault secret retrieval succeeds (no auth error).
  - `collector.py` downloads from GitHub.
  - The collector enumerates workspaces (you'll see them printed).
  - The silver CSV lands in `Files/page_telemetry/silver/page_views.csv`.
  - The `page_views_silver` Delta table appears in the lakehouse Tables view.

### 2. Create the Data Pipeline

In Fabric → your workspace → **+ New** → **Data Pipeline** → name it
`PageTelemetryDailyPipeline` → **Create**.

Add a **Notebook** activity:

- **Settings → Notebook** → pick `PageTelemetryCollector`
- **Settings → Retry**: 2
- **Settings → Retry interval**: 300 seconds
- **Settings → Timeout**: 02:00:00

Add a schedule:

- Pipeline canvas → **Schedule** → **On** → **Daily** → **02:00 UTC**.

`data-pipeline.json` in this folder is the equivalent definition. Fabric
doesn't currently support importing a Data Pipeline from a JSON file
through the UI, but the file is here as documentation-as-code and is
useful if you're managing pipelines via the
[Fabric REST API](https://learn.microsoft.com/rest/api/fabric/articles/).

### 3. Wire up Key Vault access

The notebook uses `notebookutils.credentials.getSecret(...)` which requires:

- The **workspace identity** (or your user identity, if running interactively)
  has **Key Vault Secrets User** on the vault.
- The Key Vault's **Networking** allows access from the Fabric trusted
  services (enable "Allow trusted Microsoft services").

If the secret fetch fails with a 403, the workspace identity is missing the
Key Vault RBAC role. Grant **Key Vault Secrets User** at the secret scope and
retry.

### 4. Monitor

- **Pipeline run history** → Fabric pipeline UI shows success/failure.
- **Notebook run snapshots** → click into a pipeline run → notebook activity
  → "View snapshot" for the captured cell output including the collector's
  workspace/report counts.
- **Lakehouse table** → `page_views_silver` should grow by ~one day of rows
  each run; if it stops growing, the collector ran but the merge didn't pick
  up new rows (almost always means the silver CSV wasn't written — check
  the collector logs in the notebook snapshot).

## Customizing

- **Different output location**: change `OUTPUT_DIR` in cell 3 to a path
  outside the default lakehouse — e.g., an ADLS Gen2 abfss URL mounted via
  `notebookutils.fs.mount(...)`.
- **Different schedule**: edit the pipeline's schedule. The collector tolerates
  multiple runs per day (the silver-layer `MERGE` is idempotent).
- **Pin the collector version**: change `COLLECTOR_REF` in cell 1 from
  `"main"` to a release tag (e.g. `"v0.2.0"`) so a downstream `collector.py`
  bug doesn't break production silently.
- **Add gold marts**: append cells that aggregate `page_views_silver` into
  KPI tables. See `docs/deployment-guide.md` §4 ("Storage layout") for the
  recommended gold-layer shape.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `KeyVaultErrorException: Forbidden` on cell 3 | Workspace identity is missing `Key Vault Secrets User` RBAC at the secret scope. |
| `urlretrieve` 404 on cell 4 | `COLLECTOR_REF` points at a branch/tag that doesn't exist on the public repo. |
| `AADSTS700016` from cell 5 | `pbi-client-id` secret is wrong, the SP was deleted, or the secret points at the wrong tenant. |
| `rows: 0` in cell 5 output | SP has REST access but the XMLA query returned nothing. Check capacity SKU and XMLA = Read. |
| Cell 6 `AnalysisException: cannot resolve view_date` | The silver CSV layout drifted. Check `collector.py` upstream — the row schema may have changed. |

For the underlying root-cause guide (AAD error codes, network issues, etc.),
see [`../../docs/deployment-guide.md#9-troubleshooting`](../../docs/deployment-guide.md#9-troubleshooting).
