# PII and retention guidance

This collector aggregates Power BI usage telemetry. The data is
relatively low-risk — it does not capture report contents, query
results, or user inputs — but it is **not PII-free**. Treat it
accordingly before exposing it to a broader audience.

## What is and isn't PII in the silver layer

`page_views`, `page_catalog`, and `report_views` are **all non-PII**.
`user_views` (v0.3.0) carries **pseudonymized PII** (hashed UPN).

The DAX queries the collector issues against the Usage Metrics
dataset are intentionally **aggregated**: `page_views` at
`(workspace, report, page, day)`, `report_views` at
`(workspace, report, day)`, `user_views` at
`(workspace, report, user_id_hash, day)`. Raw UPNs are hashed inside
the collector process (SHA-256 of `lower(strip(upn))`, first 16 hex
chars) **before** they are written to silver — UPN strings never
touch disk.

| Table.Field | PII? | Notes |
|---|---|---|
| `page_views.workspace_id` / `workspace_name` | No | Tenant metadata. |
| `page_views.report_id` / `report_name` | No | Tenant metadata. |
| `page_views.page_id` / `page_name` | No | Report design metadata. |
| `page_views.view_date` | No | Day-level date, no time-of-day. |
| `page_views.view_count` | No | Aggregate count. |
| `page_views.unique_users` | No | Aggregate count (no UPNs). |
| `page_views.avg_dwell_seconds` | No | Aggregate. |
| `page_views.top_persona` | No | Optional enrichment category, not user-identifying. |
| `page_views.capacity_name` | No | Tenant metadata. |
| `page_catalog.*` (v0.3.0) | No | Page roster only — no view data, no users. |
| `report_views.*` (v0.3.0) | No | Same shape as page_views minus the page dimension. |
| **`user_views.user_id_hash`** (v0.3.0) | **Pseudonymized** | SHA-256-of-lowercased-UPN, first 16 hex chars. **NOT** reversible by anyone outside the tenant. Treat as PII-equivalent for retention, RLS, and DSAR. |
| `user_views.view_count` / `distinct_pages_viewed` | No (in isolation) | Aggregates per hashed user. |

### Why hash UPNs instead of dropping them entirely?

`user_views` exists to answer 2 real customer questions Jon at Incyte
asked for:

1. **Power-user identification** — "who in my org actively uses this
   60-page report?" so we know who to call before deprecating a page.
2. **Capacity SKU sizing** — `[Unique Users]` and `[Power Users]`
   measures justify Fabric capacity tier and CU pricing.

Both need *per-user* aggregates without needing to read the actual
identity. Hashing preserves the join key (same user → same hash, so
GROUP BY works) while making the value un-enumerable from outside.

To convert a hash back to a name (e.g. for a top-50 power-users
mailout), re-hash the candidate UPN and look it up — don't try to
reverse the hash. The cleanest pattern is a *separate, governed*
"people" semantic model with RLS that joins `user_id_hash` →
`display_name` only for callers who already have AAD permission to
see those names.

### Where PII can still sneak in

1. **Custom DAX that selects `[User]` from the raw model**. If you
   write a custom DAX query outside the collector that projects the
   raw `[User]` column — e.g. directly against Power BI's Modern
   Usage Metrics semantic model from Power BI Desktop — you are now
   handling unhashed PII. The shipped collector never does this.
2. **Reports whose names contain patient or clinician identifiers**.
   Some healthcare tenants name reports `MRN_123456_Trial_Status`. The
   `report_name` field will carry those forward. If your tenant does
   this, either rename the reports, or strip the field before exposing
   the silver layer outside admin.
3. **Workspaces named after individuals**. Same problem as above for
   `workspace_name`.
4. **Joining `user_views` against a "people" table inside the same
   dataset**. If a BI developer joins `user_id_hash` → display name
   inside `page_telemetry_gold`, the join now exposes UPNs to
   anyone with read on the gold model. Keep the people-table join in
   a *separate*, RLS-gated semantic model.

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
