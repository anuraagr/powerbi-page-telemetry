# PII and retention guidance

This collector aggregates Power BI usage telemetry. The data is
relatively low-risk — it does not capture report contents, query
results, or user inputs — but it is **not PII-free**. Treat it
accordingly before exposing it to a broader audience.

## What is and isn't PII in the silver layer

The shipped silver schema (see [`data-dictionary.md`](data-dictionary.md))
is **all non-PII**. The DAX query the collector issues against the
Usage Metrics dataset is intentionally **aggregated** at the
`(workspace, report, page, day)` grain, with `unique_users` exposed
only as a count (`DISTINCTCOUNT`), not as a list of UPNs.

| Field | PII? | Notes |
|---|---|---|
| `workspace_id` / `workspace_name` | No | Tenant metadata. |
| `report_id` / `report_name` | No | Tenant metadata. |
| `page_id` / `page_name` | No | Report design metadata. |
| `view_date` | No | Day-level date, no time-of-day. |
| `view_count` | No | Aggregate count. |
| `unique_users` | No | Aggregate count (no UPNs). |
| `avg_dwell_seconds` | No | Aggregate. |
| `top_persona` | No | Optional enrichment category, not user-identifying. |
| `capacity_name` | No | Tenant metadata. |

The **risk** therefore comes from how the data is **combined and
exposed**, not from the data itself.

### Where PII can sneak in

1. **Custom DAX that selects `[User]`**. The
   `Report page views` table has a `[User]` column (UPN). If you write
   a custom DAX query that projects it — e.g. for an exec
   "top viewers" page — you are now handling PII. The shipped collector
   does not do this.
2. **Reports whose names contain patient or clinician identifiers**.
   Some healthcare tenants name reports `MRN_123456_Trial_Status`. The
   `report_name` field will carry those forward. If your tenant does
   this, either rename the reports, or strip the field before exposing
   the silver layer outside admin.
3. **Workspaces named after individuals**. Same problem as above for
   `workspace_name`.

## Retention

We recommend **90 days** for page-attributable rows in production.

- The historical Power BI Usage Metrics dataset itself retains
  90 days of data, so anything older than that is read from your
  silver / gold archive only.
- For Incyte-style multi-tenant clinical-trial workspaces, GxP
  retention rules typically run 7–25 years, but those apply to
  **patient data**, not to BI-tool usage telemetry — which is
  IT-operations data. Confirm with your Records Management team.

### How to enforce retention

**Bronze (date-partitioned)** — easy. Run a daily prune:

```bash
# Delete bronze partitions older than 90 days.
find ./out/bronze -mindepth 1 -maxdepth 1 -type d -name 'dt=*' \
  -mtime +90 -exec rm -rf {} +
```

```python
# Or in Fabric Spark, against the Files mount:
from datetime import date, timedelta
import os, shutil
cutoff = (date.today() - timedelta(days=90)).isoformat()
for d in sorted(os.listdir("/lakehouse/default/Files/page_telemetry/bronze")):
    if d.startswith("dt=") and d[3:] < cutoff:
        shutil.rmtree(f"/lakehouse/default/Files/page_telemetry/bronze/{d}")
```

**Silver (Delta table)** — preferred:

```sql
-- Fabric SQL endpoint
DELETE FROM page_views_silver
WHERE view_date < current_date() - INTERVAL 90 DAYS;

OPTIMIZE page_views_silver;
VACUUM page_views_silver RETAIN 168 HOURS;  -- 7 days of time-travel
```

**Gold / dashboard** — re-aggregate from silver after each prune; the
bundled `aggregate_for_dashboard.py` is idempotent.

## Row-Level Security on the gold semantic model

If you publish a Power BI report on top of the gold layer for broad
distribution, gate workspace/report visibility by the consumer's role.
The shipped Power BI template ([`dashboard/PowerBI/`](../dashboard/PowerBI/))
includes a stub role you can adapt:

```dax
// Role: "Workspace owner"
// Applied to: page_views_silver
[workspace_name] IN VALUES('WorkspaceOwners'[workspace_name])
&& USERPRINCIPALNAME() = LOOKUPVALUE(
    'WorkspaceOwners'[owner_upn],
    'WorkspaceOwners'[workspace_name],
    [workspace_name]
)
```

You'll need a small `WorkspaceOwners` table (workspace_name, owner_upn)
that you maintain separately — the collector does not populate it
because the Power BI REST API doesn't return rich workspace ACLs in a
single call. A common pattern is to sync it once a day from
`GET /v1.0/myorg/admin/groups?$expand=users`.

## DSAR ("right to be forgotten") workflow

Because the silver layer is **aggregated** and contains no UPNs, a
DSAR for a specific individual normally requires **no action on this
data**. The user's identity isn't in it.

If you have extended the schema to include `[User]` for your own
purposes, you'll need to:

1. Identify the user's UPN.
2. Delete or anonymize matching rows in silver and any gold copies:
   ```sql
   DELETE FROM page_views_with_user
   WHERE user_upn = '<the.user@tenant.com>';
   ```
3. Re-run `aggregate_for_dashboard.py` so the static dashboard reflects
   the new state.
4. Record the action per your tenant's DSAR log policy.

## Audit log integration (optional)

For full per-user attribution beyond the 90-day Usage Metrics window,
the **Power BI audit log** (Microsoft 365 admin center → Security &
Compliance → Audit) records every `ViewReport` event with the UPN and
timestamp, retained per your tenant's audit retention. This is a
**different data source from this collector** and a **different PII
risk profile**. Treat it as a separate project with its own retention,
RBAC, and DSAR plan.
