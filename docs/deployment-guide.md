# Deployment Guide — Page Telemetry Collector

This walks you from "nothing" to "daily refresh into a central Power BI
report" in your tenant.

## 0. Prereqs to confirm

| Item | Why | Where to check |
| --- | --- | --- |
| Power BI tenant with Fabric capacity (F-SKU) or Premium P-SKU | XMLA read endpoint and Usage Metrics v2 both require it | Admin Portal → Capacity settings |
| `Service principals can use Power BI APIs` tenant setting **enabled** for a security group | Required for unattended REST calls | Admin Portal → Tenant settings → Developer settings |
| `XMLA endpoint` set to **Read** or **Read Write** on the target capacity | Required to query Usage Metrics datasets without scraping the UI | Admin Portal → Capacity settings → Power BI workloads |
| Audit logs retained ≥ 90 days | Backstop for cross-check; not strictly required | M365 Purview |
| Workspace tagging convention | RLS later filters by owning BU | Your BI ops team |

## 1. Identity setup

Create an Entra service principal:

```powershell
$sp = New-AzADServicePrincipal -DisplayName "pbi-page-telemetry-collector"
# capture sp.AppId  → PBI_CLIENT_ID
# create a secret in Key Vault, never store in plain text → PBI_CLIENT_SECRET
# capture (Get-AzContext).Tenant.Id → PBI_TENANT_ID
```

Add the SP to the security group that's allowed to use Power BI APIs.
Grant it **Fabric Administrator** (lowest-privilege option that gives
admin REST + XMLA on every workspace). If your governance forbids
tenant-wide admin, alternatively make it a **Member of every
workspace** plus assign **`Tenant.Read.All`** on the Fabric service —
but that's a per-workspace dance.

## 2. Capacity / XMLA

Verify the XMLA read endpoint string for one workspace:

```
powerbi://api.powerbi.com/v1.0/myorg/<workspace-name>
```

Connect with SSMS using:

```
Provider=MSOLAP;
Data Source=powerbi://api.powerbi.com/v1.0/myorg/<workspace-name>;
User ID=app:<PBI_CLIENT_ID>@<PBI_TENANT_ID>;
Password=<PBI_CLIENT_SECRET>;
```

You should see the workspace's datasets in the object explorer. If you
don't, the SP doesn't have access OR the XMLA endpoint is off.

## 3. Deploy the collector

### Option A — Fabric notebook (recommended)

1. In Fabric, create a new **Notebook** under your governance workspace.
2. Paste `etl/collector.py` into a cell, plus a thin wrapper:
   ```python
   import os, sys
   sys.argv = ["collector.py", "--tenant", os.environ["PBI_TENANT_ID"]]
   from collector import main; main()
   ```
3. Wire the secrets via **Notebook → Spark settings → Environment variables**
   (or pull them from Key Vault with `notebookutils.credentials.getSecret`).
4. Create a **Fabric Data Pipeline** with a single **Notebook activity**
   pointed at this notebook. Schedule daily at 02:00 local time.
5. Set the pipeline's failure policy to "fail on activity error" so
   throttling surfaces in monitoring.

### Option B — Azure Function (TimerTrigger)

If you prefer running outside Fabric, package `etl/collector.py` as a
Function with a TimerTrigger (`0 0 6 * * *` UTC). Store secrets in
Key Vault, bind via Managed Identity. Write output to ADLS Gen2 and use
a Fabric shortcut to surface it as a Lakehouse table.

### Option C — Local POC

```powershell
cd etl
pip install -r requirements.txt
python collector.py --mock      # offline demo
python collector.py --tenant <id> --client-id <id> --client-secret <secret>
```

## 4. Storage layout

```
OneLake/<workspace>/PageTelemetry.Lakehouse/Files/
├── bronze/dt=YYYY-MM-DD/ws=<id>/report=<id>/page_views.parquet
├── silver/fact_page_views/ (Delta)        — conformed daily fact
├── gold/
│   ├── kpi_page_usage_daily/              — pre-aggregated for tile load
│   ├── kpi_underused_pages/               — filtered to views < threshold
│   ├── kpi_long_report_engagement/        — for high-page-count reports
│   └── dim_report/  dim_page/  dim_workspace/  dim_date/
```

Silver schema (Delta):

| Column | Type | Notes |
| --- | --- | --- |
| `workspace_id` | string | PK part |
| `workspace_name` | string | denormalised |
| `capacity_name` | string | denormalised |
| `report_id` | string | PK part |
| `report_name` | string | denormalised |
| `report_total_pages` | int | from dim_report |
| `page_id` | string | PK part — composite of report_id + page index |
| `page_name` | string | display name |
| `page_ordinal` | int | 1-based |
| `view_date` | date | PK part |
| `view_count` | int | SUM('Report page views'[Views]) |
| `unique_users` | int | DISTINCTCOUNT |
| `avg_dwell_seconds` | double | AVERAGE |
| `top_persona` | string | optional — joined from people dim |

## 5. Semantic model + central report

- Build a DirectLake semantic model on the `gold/` tables.
- Star schema: fact_page_views ← dim_workspace, dim_report, dim_page, dim_date.
- Recreate the three dashboard tabs as Power BI report pages.
- Apply **RLS** by workspace owner:
  ```
  [WorkspaceOwnerEmail] = USERPRINCIPALNAME()
  ```
  …or by BU using a `dim_workspace[BusinessUnit]` column joined against
  an Entra group membership table.

## 6. Operations / monitoring

- Use **Workspace Monitoring** (preview) on the governance workspace to
  watch pipeline run time and capacity utilisation.
- Surface `_run_summary.json` (per-run) as a small table; alert if
  `errors > 0` or `rows < 50% of 28-day median`.
- For high-value reports, alert via **Data Activator** when
  `view_count = 0` for ≥ 30 consecutive days (likely retired but not
  decommissioned).

## 7. When the platform catches up

Microsoft's **Monitor Usage Metrics for Workspaces** capability is in
preview at the time of writing. When it GAs:

- Replace the per-report `ensure_usage_metrics_dataset` + per-report XMLA
  call with a single XMLA query against the per-workspace semantic model.
- Keep `silver/` and `gold/` schemas. The collector class boundary
  (`CollectorAdapter`) is designed for this swap.
- Net effect: one DAX query per workspace instead of one per report
  → 10–50× faster, much less capacity load.

## 8. Hardening checklist before go-live

- [ ] Service principal secret rotated quarterly via Key Vault
- [ ] All Power BI Admin API calls retried with exponential backoff on 429
- [ ] Activity logs from the collector itself shipped to Sentinel
- [ ] RLS validated with three test personas (BU admin, BU user, exec)
- [ ] Bronze retention set (e.g., 13 months) for audit/replay
- [ ] Disaster-recovery: bronze is the source of truth; silver/gold can
      always be rebuilt
