# Power BI Page-Level Telemetry — Reference Implementation

> **Tenant-wide, programmatic, page-level usage analytics for Power BI —
> without waiting for a first-party API.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![Power BI](https://img.shields.io/badge/Power%20BI-Admin%20REST%20%2B%20XMLA-F2C811.svg)](https://learn.microsoft.com/power-bi/)
[![Microsoft Fabric](https://img.shields.io/badge/Microsoft%20Fabric-Ready-0078D4.svg)](https://learn.microsoft.com/fabric/)

## The problem

Power BI's admin telemetry stops at the **report** grain. The
`Get Activity Events` REST API, the Admin scanner, and the audit log all
emit `ViewReport` events with no `Page` or `Section` field. Page-level
data *does* exist, but only inside the per-report auto-generated
**Usage Metrics Report v2** datasets — one dataset per report, accessible
only by opening that report's metrics page in the browser.

For BI teams running thousands of reports — especially long
multi-page reports (clinical trial trackers, ops scorecards, finance
packs) — this means:

- ❌ No way to see **which pages** are actually used across the tenant
- ❌ No way to find **never-viewed pages** sitting in production reports
- ❌ No way to feed page-level signals into a central monitoring report,
      Data Activator alert, or governance workflow
- ❌ Report-level view counts dramatically understate effort lost on
      pages no one opens

## What this repo gives you

A small, opinionated reference implementation that **closes the gap
today** using only documented Microsoft APIs:

1. **`etl/collector.py`** — a Python collector that:
   - Enumerates every workspace and report via the **Power BI Admin
     REST API**.
   - Idempotently ensures each report's **Usage Metrics v2** dataset
     exists (`POST /admin/reports/{id}/usageMetrics`).
   - Queries each dataset over the **XMLA read endpoint** with a single
     parameterised DAX `SUMMARIZECOLUMNS` (server-side aggregation, only
     summary rows transit the wire).
   - Lands rows in a `bronze/` → `silver/` layout that drops cleanly into
     a Fabric Lakehouse, ADLS Gen2, or local disk.
   - Ships with a `MockAdapter` so you can run the whole pipeline on a
     laptop with no tenant access.

2. **`dashboard/PageUsageDashboard.html`** — a self-contained HTML
   dashboard (Chart.js, no build step, no server) that demonstrates the
   three views you'll want from this data:
   - **Tenant overview** — daily trend, workspace breakdown, page-view
     distribution showing how long the tail really is, top-15 pages.
   - **Report drill-down** — every page ranked, with hatched bars for
     pages that have **never been viewed**.
   - **Underused pages** — every page tenant-wide with < 100 views in
     90 days, sorted ascending. The work list.

3. **`docs/deployment-guide.md`** + **`docs/api-reference.md`** —
   everything you need to stand the collector up in your tenant:
   service principal setup, capacity requirements, the exact REST
   endpoints, the exact DAX query, throttling guidance, and an RBAC
   matrix.

4. **`architecture.png`** — a one-glance architecture diagram (sources
   → collector → lakehouse → consumers).

## Quick start

```powershell
# 1. Open the dashboard. No setup required.
start dashboard\PageUsageDashboard.html

# 2. Run the collector end-to-end in mock mode (no tenant access).
cd etl
pip install -r requirements.txt
python collector.py --mock
# → writes out/bronze/*.csv, out/silver/page_views.csv, out/_run_summary.json

# 3. (Optional) Regenerate the synthetic sample data.
python generate_sample_data.py
python aggregate_for_dashboard.py
cd ..\dashboard
python bundle.py
```

To run against a real tenant, see [`docs/deployment-guide.md`](docs/deployment-guide.md).

## Repo layout

```
.
├── README.md                            ← this file
├── LICENSE                              ← MIT
├── architecture.excalidraw              ← editable architecture diagram
├── architecture.png                     ← exported PNG
├── dashboard/
│   ├── PageUsageDashboard.html          ← self-contained demo dashboard
│   ├── _template.html                   ← Chart.js + Clawpilot theme template
│   ├── bundle.py                        ← inlines page_views.json into template
│   └── page_views.json                  ← aggregated payload the dashboard reads
├── etl/
│   ├── collector.py                     ← the collector (mock + live modes)
│   ├── generate_sample_data.py          ← synthetic-data generator
│   ├── aggregate_for_dashboard.py       ← rolls silver CSV → dashboard JSON
│   ├── requirements.txt
│   └── sample_data/
│       ├── page_views.csv               ← 15k+ rows · 5 workspaces · 15 reports · 90 days
│       └── reports_catalog.csv          ← every defined page (incl. never-viewed)
└── docs/
    ├── deployment-guide.md              ← stand it up in your tenant
    └── api-reference.md                 ← REST endpoints, DAX, RBAC, throttling
```

## Architecture

![architecture](architecture.png)

Four columns, left to right:

1. **Sources** — Power BI Admin REST API (workspace & report enumeration),
   Usage Metrics v2 datasets (per-report, auto-generated by Power BI).
2. **Collector** — service principal auth, REST enumeration, idempotent
   `POST /usageMetrics`, XMLA DAX query, bronze CSV/Parquet per report.
3. **Lakehouse** — bronze (raw per-report) → silver (conformed fact) →
   gold (KPI marts) in OneLake / ADLS Gen2 / local disk.
4. **Consumers** — central Power BI report (DirectLake on gold), Data
   Activator alerts, Copilot Q&A, governance workflows.

The **adapter pattern** in `collector.py` (`CollectorAdapter` → `LiveAdapter`
or `MockAdapter`) is deliberate: when Microsoft's
**Monitor Usage Metrics for Workspaces** capability GAs (one
workspace-level semantic model that includes page activity), swap in a
`WorkspaceSemanticModelAdapter` and nothing downstream changes.

## The sample data

The bundled `sample_data/page_views.csv` is fully synthetic and
deterministic (seeded RNG). It simulates a healthcare BI tenant
because that's a domain where multi-page reports are common and the
long-tail problem is dramatic — but the collector itself is
industry-agnostic and will work against any Power BI tenant.

What's in the sample:

- **5 workspaces**: Clinical Operations, Medical Affairs, Real World
  Evidence, Commercial Analytics, Manufacturing & Supply.
- **15 reports** ranging from a 6-page daily ops dashboard to a
  **60-page Phase III clinical trial report** (STUDY-101).
- **90 days** of daily page-view rows with realistic seasonality
  (weekday peaks), Zipf-distributed page popularity, and a deliberate
  **9 pages that never get viewed** across the tenant (5 appendix pages
  on the long clinical trial report alone) — exactly the kind of waste
  page-level telemetry is designed to surface.

When you run `dashboard/PageUsageDashboard.html`, the headline numbers
you'll see are:

| Metric | Value |
| --- | --- |
| Workspaces | 5 |
| Reports | 15 |
| Distinct pages | 231 |
| **Pages with 0 views in 90 days** | **9** |
| **Pages with < 100 views in 90 days** | **74** |
| Total page views | ~155 k |

## Why an adapter pattern, not just one script?

Three reasons:

1. **The collector should not change when Microsoft ships the new API.**
   When Monitor Usage Metrics for Workspaces GAs, the bronze rows that
   land on disk should be identical so the silver/gold/dashboard layer
   never knows the data source changed.
2. **Customers want to run the dashboard before they install anything.**
   The `MockAdapter` exists so a 30-minute demo is possible without any
   tenant access, service principal, or capacity.
3. **Other data sources need the same shape.** Several teams have asked
   about layering Microsoft Sentinel audit-log signals or third-party
   web-analytics events on top of the same gold model. With the adapter
   pattern, that's a new class file, not a fork of the whole project.

## What about the Microsoft GA path?

Microsoft has previewed **Monitor Usage Metrics for Workspaces** which
provisions one workspace-level semantic model covering reports,
dashboards, paginated reports, and **page-level activity** — accessible
via the XMLA endpoint. It's not GA at the time of writing.

This repo is the bridge until then, and is forward-compatible: when GA
ships, drop in a `WorkspaceSemanticModelAdapter` (one DAX query per
workspace instead of one per report → 10–50× less capacity load) and
keep everything else.

## Contributing

Issues and PRs welcome. Likely high-value additions:

- A `WorkspaceSemanticModelAdapter` once Microsoft's new preview stabilises.
- A real Fabric notebook wrapper + Data Pipeline JSON in `docs/fabric/`.
- A T-SQL / Spark notebook that lands the silver layer in a Lakehouse
  Delta table.
- An optional `kusto` adapter for tenants that already pipe audit logs
  into Microsoft Sentinel / ADX.

## License

[MIT](LICENSE). Use, fork, sell, modify — just don't blame the authors.
