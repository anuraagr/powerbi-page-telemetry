# Runbook — on-call response

Operational runbook for engineers paged when the page-telemetry
collector fails. Optimized for the **Azure Function** deploy option;
notes for **Fabric notebook** and **local cron** are inline. The
collector emits stable error messages and a `_run_summary.json` after
every run — quote both when escalating.

## Severity

| Severity | Trigger | Owner response |
|---|---|---|
| **SEV-3** | Single run failed; next-run window > 1 hour away | First on-call eyes within 1 business hour. |
| **SEV-2** | Two consecutive runs failed, or `errors[]` > 25% of `reports` | Eyes within 30 minutes. |
| **SEV-1** | 4+ consecutive runs failed, OR data is stale > 36h, OR Function App is in a crash loop | Page the on-call engineer immediately. |

The bundled `--metrics prometheus` / `--metrics appinsights` output makes
SEV-2 / SEV-1 trivial to alert on — see
[`docs/deployment-guide.md`](deployment-guide.md) for the queries.

## First 5 minutes — confirm and characterize

1. **Look at `_run_summary.json` from the most recent run.**
   - Azure Function: `az functionapp log tail -g <rg> -n <fn>` and grep
     for `"schema_version"` — it's the summary print at the end.
   - Fabric notebook: open the notebook run in Fabric → cell output of
     the final cell prints the same JSON.
   - Local cron: tail `~/.local/state/pbi-telemetry/last-run.log`.
2. **Note three numbers from the summary:**
   - `workspaces` (should be > 0 in a healthy tenant)
   - `reports` (should be roughly the same as yesterday)
   - `len(errors)` (should be 0 or very small)
3. **Classify by which of those three is wrong:**

| Symptom | Likely cause | Jump to |
|---|---|---|
| `workspaces = 0` | Auth or scanner permissions revoked | [§A](#a-workspaces--0) |
| `reports` much smaller than yesterday | Workspace scope changed, or admin permission scoped tighter | [§B](#b-fewer-reports-than-yesterday) |
| `len(errors)` is high, mostly `HTTP 429` | Throttle storm — the retry helper exhausted | [§C](#c-many-429s) |
| `len(errors)` is high, mostly `XmlaTokenError` / `InvalidRequest` | XMLA endpoint disabled on a capacity, OR SP not in workspace | [§D](#d-xmla-or-executequeries-401403) |
| `workspaces_not_bootstrapped` is non-empty | Those workspaces have never had `... → View usage metrics report` clicked once | [§H](#h-workspaces_not_bootstrapped-listed) |
| Function host never started — opaque "Worker failed to function index" | Missing `collector.py` next to `function_app.py` | [§E](#e-function-app-wont-start) |
| Fabric notebook fails at the schema-version assertion | `EXPECTED_SCHEMA_VERSION` drift | [§F](#f-fabric-schema-version-assertion-failed) |
| Data is stale but the run reports success | Run is "succeeding" against the wrong workspace scope (e.g. SP lost group membership) | [§G](#g-stale-data-despite-success) |

## A. `workspaces = 0`

**Diagnosis (3 minutes):**

```bash
# Smoke-test the SP credential and Power BI scope.
curl -sS -X POST "https://login.microsoftonline.com/${PBI_TENANT_ID}/oauth2/v2.0/token" \
  -d "grant_type=client_credentials&client_id=${PBI_CLIENT_ID}&client_secret=${PBI_CLIENT_SECRET}&scope=https%3A%2F%2Fanalysis.windows.net%2Fpowerbi%2Fapi%2F.default" \
  | jq -r '.access_token // .error_description'
```

- If `error_description` says `AADSTS7000215` → secret expired. Rotate
  it in Entra → app registrations → `<sp>` → Certificates & secrets,
  then update Key Vault.
- If you get a token but `GET /v1.0/myorg/admin/groups?$top=5` returns
  `401` / `403` → the SP is no longer a member of the
  *Power BI Admin API* security group. Re-add it via Fabric Admin Portal
  → Tenant settings → "Allow service principals to use Power BI APIs" →
  Specific security groups.

**Fix:** rotate secret OR re-add SP to the API-enabled SG. Re-run.

## B. Fewer reports than yesterday

**Diagnosis (5 minutes):**

```bash
# Compare today's bronze partition vs yesterday's.
diff <(ls out/bronze/dt=$(date -d 'yesterday' +%F)/ | sort) \
     <(ls out/bronze/dt=$(date +%F)/ | sort)
```

Reports that disappeared usually fall into three buckets:

1. **Workspace was deleted or moved out of Premium/Fabric.** Confirm in
   Fabric Admin Portal → Workspaces. Acceptable; document and move on.
2. **Workspace was renamed.** `workspace_id` stays the same, so the
   delta should be a name change, not a missing report. If you see
   missing reports here, escalate.
3. **SP was removed from the workspace.** The dataset enumeration call
   for that workspace returns 403, which our retry helper does not
   retry (it's a hard auth error). Look at `errors[]` — entries say
   `<workspace_name>: HTTPError: 403 Forbidden`. Re-add the SP as a
   workspace member.

## C. Many 429s

The `_request` helper retries up to 5 times honoring `Retry-After`. If
errors are still piling up, the tenant is in a sustained throttle.
Options:

1. **Reduce parallelism.** This collector is sequential by design, but
   if a customer fork is parallelizing the workspace loop with
   `asyncio` / threads, lower the worker count and retest.
2. **Stagger the schedule.** If multiple Functions in the tenant pull
   admin APIs at the same minute, change the cron expression in
   `local.settings.json` / the deploy template to offset them by
   5–10 minutes.
3. **Shorter date window.** Re-run with `--days 1` once a day instead
   of a 90-day re-pull — the silver layer's date-partitioned bronze
   keeps history regardless.
4. **Escalate to MSRC / Power BI support** with the X-MS-CorrelationId
   from a representative 429 response if the throttle persists more
   than 24 hours.

## D. XMLA or executeQueries 401/403

The collector defaults to REST `executeQueries`. If you see XMLA
errors specifically, someone enabled `PBI_USE_PYADOMD=1` — check the
app settings.

**Most common causes (in order):**

1. **Capacity setting**: Admin Portal → Capacity settings →
   `<capacity>` → XMLA endpoint must be `Read` or `Read Write`. The
   default for a fresh F-SKU is `Read Write`; for legacy P-SKUs the
   default may be `Off`.
2. **Workspace membership**: the SP must be a workspace **Member** or
   **Admin** (not just have admin REST access). XMLA / executeQueries
   are workspace-scoped, not tenant-scoped.
3. **Dataset doesn't exist**: see §H — the workspace has never had
   the one-time `... → View usage metrics report` portal click, so the
   Modern Usage Metrics semantic model was never provisioned. There
   is no public REST to provision it.

## H. `workspaces_not_bootstrapped` listed

Symptom: `_run_summary.json` shows e.g.
`"workspaces_not_bootstrapped": ["Clinical Operations", "Sales NA"]`
and `reports_skipped_no_bootstrap > 0`. Those workspaces are returning
zero page-view rows from the collector.

**Cause:** The Modern Usage Metrics (preview) semantic model is created
lazily on the first portal click of `... → View usage metrics report`
on any report in a workspace. There is **no public REST API** that
does this provisioning — confirmed by Power BI PM David Browne (HLS
Roundtable, May 2026). Workspaces that have never been bootstrapped
have no `Usage Metrics Report` semantic model for the collector to
read.

**Fix (one workspace at a time, or in bulk):**

1. Have a workspace admin or contributor open any report in the
   workspace.
2. Click the `...` menu in the report header.
3. Click **View usage metrics report**.
4. Wait ~5 seconds while Power BI provisions the semantic model.
5. Close the resulting tab — you only needed the provisioning side
   effect.

After the click, the next collector run will pick the workspace up
automatically. The model accumulates **page-level data for every
report** in the workspace from that point forward; previously-collected
data does not back-fill (the Usage Metrics models cover the last 30
days going forward from creation).

**Tenant-wide bulk bootstrap:** if you have dozens of workspaces, a
governance script can iterate every workspace via the admin REST API
and post a one-time `View usage metrics report` portal request per
workspace using a workspace contributor's delegated token. See
`docs/api-reference.md §3` for the per-workspace bootstrap details
and tenant-scope caveats.

**Legacy variant**: if a tenant has the older `Usage Metrics Report
v2 - <reportname>` per-report model instead of the modern
workspace-level one, set the env var
`PBI_USAGE_DATASET_NAME=Usage Metrics Report v2` and the collector's
prefix-match will pick it up without code changes.

## E. Function App won't start

Symptom: deploy succeeded but `/api/admin/host/status` returns
`Functions runtime not ready` or `Worker failed to function index`,
and `func logs` shows no per-invocation output.

**Most common cause:** `collector.py` wasn't copied into the
deployment package next to `function_app.py`. The deploy scripts
(`deploy.ps1` / `deploy.sh`) do this automatically; if you deployed
manually, run the script.

Tier 1's deferred-import fix means an import failure now surfaces as a
runtime error inside an invocation, not as a startup crash — so check
`Application Insights traces` for an explicit
`ImportError: collector.py not found alongside function_app.py — run deploy.ps1`.

## F. Fabric schema-version assertion failed

Symptom: the first cell after the collector download fails with
`AssertionError: SCHEMA VERSION MISMATCH`.

**Cause:** `COLLECTOR_REF` in the notebook is pinned to a tag whose
`SILVER_SCHEMA_VERSION` doesn't match the notebook's
`EXPECTED_SCHEMA_VERSION`.

**Fix paths:**

1. **Intentional schema bump**: bump `EXPECTED_SCHEMA_VERSION` in the
   notebook to match, after reading the `CHANGELOG.md` entry that
   introduced the bump and confirming the downstream Delta table can
   handle the new shape.
2. **Pin drift**: roll `COLLECTOR_REF` back to the last release tag
   whose schema version matches.
3. **Cache poisoning**: if a previous run cached a bad version, delete
   `/lakehouse/default/Files/_cache/collector.<ref>.py` and re-run.

## G. Stale data despite success

If `_run_summary.json` shows `errors: []`, `rows > 0`, and a recent
`ended_at`, but the dashboard / Delta table doesn't reflect new
reports, the most likely cause is the collector ran successfully
against a **smaller scope** than expected:

1. The SP was removed from some workspaces; the collector enumerated
   the ones it can see and reported success on those.
2. The Function App settings were updated to a different tenant or
   different `PBI_OUTPUT_BLOB_URL` and the data is being written to a
   different blob.
3. The Fabric notebook is now writing to a different lakehouse than
   the gold semantic model is reading from.

Check `_run_summary.json`'s `workspaces` and `reports` against
yesterday — if they're smaller without a documented reason, treat as
SEV-2 §B.

## Permanent fixes after incident closure

After any SEV-2 / SEV-1:

- File a postmortem in `docs/postmortems/YYYY-MM-DD-<slug>.md`.
- If the bug is in the collector, open an issue and tag it
  `runbook-gap`; the maintainers will roll it into the next release's
  runbook entry.
- Confirm `--metrics` is enabled in production so the alert fires next
  time without a human in the loop.
