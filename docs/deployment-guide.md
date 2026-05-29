# Deployment Guide — Page Telemetry Collector

This walks you from "nothing" to "daily refresh into a central Power BI
report" in your tenant.

## 0. Prereqs to confirm

| Item | Why | Where to check |
| --- | --- | --- |
| Power BI tenant with Fabric capacity (F-SKU) or Premium P-SKU | XMLA read endpoint and Modern Usage Metrics both require it | Admin Portal → Capacity settings |
| `Service principals can use Power BI APIs` tenant setting **enabled** for a security group | Required for unattended REST calls | Admin Portal → Tenant settings → Developer settings |
| `XMLA endpoint` set to **Read** or **Read Write** on the target capacity | Required to query Usage Metrics datasets without scraping the UI | Admin Portal → Capacity settings → Power BI workloads |
| **Workspaces bootstrapped for Modern Usage Metrics (one-time)** | The Usage Metrics semantic model is created lazily on the first portal click; the collector can't provision it via REST | Open each workspace → any report → `...` → **View usage metrics report** (once) |
| Audit logs retained ≥ 90 days | Backstop for cross-check; not strictly required | M365 Purview |
| Workspace tagging convention | RLS later filters by owning BU | Your BI ops team |

> **About the workspace bootstrap step:** Power BI provisions one
> `Usage Metrics Report` semantic model per workspace, on the first
> portal click of `... → View usage metrics report` on any report in
> that workspace. There is **no public REST endpoint** to do this in
> bulk — confirmed by Power BI PM David Browne in the May 2026 HLS
> Roundtable. After bootstrapping, that one model captures page-level
> data for **every** report in the workspace, refreshed daily by
> Microsoft. Run the collector first and check
> `_run_summary.json → workspaces_not_bootstrapped` for the list of
> workspaces still needing the click.

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

The `deploy/` folder ships ready-to-use wrappers for each option below.
Pick one — each has a per-option `README.md` with full step-by-step
instructions, provisioning commands, and a troubleshooting table.

| Option | When to use | Wrapper |
| --- | --- | --- |
| **A — Fabric notebook + Data Pipeline** | You already run Fabric; you want the data to land in the same Lakehouse as everything else; you want one-pane-of-glass monitoring | [`deploy/fabric-notebook/`](../deploy/fabric-notebook/) |
| **B — Azure Function (TimerTrigger)** | You want this outside Fabric; you already have Azure CI/CD; you need Managed Identity to keep secrets out of code | [`deploy/azure-function/`](../deploy/azure-function/) |
| **C — Local / scheduled job** | POC, demo, or a single BI ops box running Task Scheduler / cron / systemd | [`deploy/local/`](../deploy/local/) |

### Option A — Fabric notebook (recommended for Fabric-first shops)

See [`deploy/fabric-notebook/README.md`](../deploy/fabric-notebook/README.md).

In short: import `PageTelemetryCollector.Notebook.py`, edit the `KEYVAULT_URL`
constant, attach a Lakehouse, and wire the included `data-pipeline.json`
to schedule it daily. Secrets come from Key Vault via
`notebookutils.credentials.getSecret`. The notebook MERGEs each run's
silver CSV into a Delta table named `page_views_silver`.

### Option B — Azure Function (TimerTrigger)

See [`deploy/azure-function/README.md`](../deploy/azure-function/README.md).

In short: an Azure Functions **v2 Python model** app that fires daily at
06:00 UTC, fetches secrets from Key Vault via app-setting references,
runs the collector, and uploads the silver CSV to ADLS Gen2 using
**Managed Identity** (`DefaultAzureCredential` + `BlobClient`). The folder
includes a full `az` provisioning script, `deploy.ps1` / `deploy.sh`,
and a `local.settings.json.example` for `func start` local debugging.

### Option C — Local POC / scheduled job

See [`deploy/local/README.md`](../deploy/local/README.md).

Three flavors:
- **Windows** — `run-collector.ps1` + `Microsoft.PowerShell.SecretManagement` + Task Scheduler.
- **Linux** — `run-collector.sh` + a systemd `.service` and `.timer` unit pair (`OnCalendar=*-*-* 06:00:00 UTC`).
- **macOS / plain cron** — same `run-collector.sh` wrapped in a cron entry that sources an `~/.config/<app>/env` file (`chmod 600`) so secrets never appear in the crontab.

Quickest possible smoke test (no scheduler at all):

```powershell
cd etl
pip install -r requirements.txt
python collector.py --mock      # offline demo, deterministic output
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

## 9. Troubleshooting

### Em-dashes or other Unicode garbled in the console output

The collector emits UTF-8 to stdout (report names contain em-dashes etc.).
If you see `ΓÇö` or `ù` instead of `—`:

- **Windows PowerShell 5.x**: run ``[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()`` before invoking the script (or upgrade to PowerShell 7+).
- **cmd.exe (legacy)**: run ``chcp 65001`` once per session.
- **PowerShell 7+, macOS Terminal, Linux**: should work out of the box.

File output (CSV, JSON) is always UTF-8 — only the console rendering is affected.

### `ERROR: collector failed: ReadTimeout`  /  `ConnectionError`  in live mode

The collector cannot reach `login.microsoftonline.com` or `api.powerbi.com`.
Check corporate proxy / VPN / firewall.  `--tenant` accepts either the GUID
or the tenant domain (`contoso.onmicrosoft.com`).

### `AADSTS700016`: Application not found in the directory

The service principal's `client-id` is wrong, or it's been deleted, or you're
signing into the wrong tenant.

### `AADSTS7000215`: Invalid client secret provided

The `client-secret` has expired or is incorrect. Mint a new one in the Entra portal.

### Live mode runs but `rows: 0`  /  empty CSV

The service principal has REST access (it enumerated workspaces) but cannot
query the per-report Usage Metrics dataset via XMLA. Check:

1. The hosting capacity is **Fabric F-SKU or Premium P-SKU** (not PPU and not shared).
2. **XMLA endpoint** is set to **Read** on the capacity (Admin Portal → Capacities).
3. The SP is a **Workspace Member** (or higher) on each workspace, OR `Service principals can use read-only admin APIs` is enabled tenant-wide AND the SP is in the named security group.
4. `pyadomd` is installed and `Microsoft.AnalysisServices.AdomdClient` is available — the bundled stub returns 0 rows.

### `PermissionError` writing to `out/`  on a network drive

Use `--out C:\\local\\path` or set `PBI_OUTPUT_DIR`. The collector does not
require OneDrive/SharePoint; local disk is faster.

### Set `PBI_DEBUG=1` to see the full stack trace

The collector swallows uncaught exceptions and prints a one-line summary by
default. Set the env var to bypass and get the full traceback.

