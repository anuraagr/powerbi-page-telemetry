# Deployment options

The collector is one Python file. How you schedule it depends on what your
tenant already runs. Pick one:

| Option | When to use | Folder |
| --- | --- | --- |
| **A. Fabric notebook + Data Pipeline** | You already run Fabric. Lands silver in a Lakehouse Delta table for DirectLake reports with zero ETL latency. **Recommended** for Fabric tenants. | [`fabric-notebook/`](fabric-notebook/) |
| **B. Azure Function (TimerTrigger)** | You run on Azure but not Fabric. Managed Identity for both Key Vault and Storage — zero secrets in code. Silver CSV in a blob container that a Fabric Shortcut can pick up. | [`azure-function/`](azure-function/) |
| **C. Local scheduler (cron / systemd / Task Scheduler)** | Prototypes, small tenants, or running before you've got a Fabric / Azure provisioning ticket through. | [`local/`](local/) |

All three import the same `etl/collector.py` and produce the same
bronze/silver layer. Switching between them is just a redeploy — no schema
changes, no consumer changes.

## What this folder does NOT include

- A **central Power BI report** that reads the silver layer. The dashboard
  in `dashboard/PageUsageDashboard.html` is the offline demo equivalent.
  In production, swap it for a Power BI Desktop report bound to your
  Lakehouse / OneLake silver table.
- An **alerting wrapper** (Data Activator / Logic App / Azure Monitor
  alert). The collector already emits a non-zero exit code on failure;
  hook your platform's standard alerting to that.
- **Bicep / Terraform**. The Azure Function folder has bash commands you
  can lift into IaC; the rest is platform-native click-ops. PRs welcome.
