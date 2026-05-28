# Deploy as an Azure Function (TimerTrigger)

This is the recommended deployment for tenants that **don't** run Fabric, or
that want the collector to live alongside other Azure automation. It runs
`collector.py` once a day via a TimerTrigger, writes the silver CSV to a
mounted blob, and uses **Managed Identity** for both Key Vault and Storage
access — no secrets in app settings or in code.

## What's in this folder

| File | What it is |
| --- | --- |
| `function_app.py` | v2 programming-model Function App — single TimerTrigger `daily_page_telemetry`. |
| `host.json` | Function host config — bundle v4, App Insights sampling enabled. |
| `requirements.txt` | Python deps installed on every deploy. |
| `local.settings.json.example` | Template — copy to `local.settings.json` for local debugging (gitignored). |
| `.funcignore` | Files excluded from the publish zip. |
| `deploy.ps1` / `deploy.sh` | One-shot deploy script — copies `etl/collector.py`, publishes, cleans up. |
| `README.md` | This file. |

## Architecture

```
TimerTrigger (06:00 UTC daily)
    └── function_app.daily_page_telemetry
            ├── DefaultAzureCredential          → Managed Identity
            │       ├── @Microsoft.KeyVault refs → Key Vault → SP secrets
            │       └── BlobClient (MI auth)
            ├── collector.main()                 → /tmp/silver/page_views.csv
            └── upload to blob                   → https://<sa>/page-telemetry/silver/page_views.csv
```

Downstream: a Fabric **Shortcut** on the `page-telemetry` container surfaces
the silver CSV as a Lakehouse table — no separate data movement.

## Prerequisites

1. **Azure CLI** logged in (`az login`).
2. **Azure Functions Core Tools v4** (`func --version` ≥ 4.0.5390).
3. **Python 3.10+** locally (matches the Function App runtime).
4. **Resource group** with:
   - A **Function App** on the **Python 3.10+** runtime (Linux Consumption,
     Premium, or App Service plan).
   - A **system-assigned Managed Identity** enabled on the Function App.
   - A **Key Vault** holding three secrets:
     - `pbi-tenant-id`
     - `pbi-client-id`
     - `pbi-client-secret`
   - A **Storage account** with a container named `page-telemetry`.
5. **RBAC roles** on the Function App's Managed Identity:
   - `Key Vault Secrets User` on the vault
   - `Storage Blob Data Contributor` on the storage account
6. **Power BI** service principal as described in
   [`../../docs/deployment-guide.md`](../../docs/deployment-guide.md) §1.

## Step-by-step

### 1. Provision the Azure resources

```bash
# Set vars
RG=rg-pbi-page-telemetry
LOCATION=eastus
SUFFIX=$RANDOM
KV=kv-pbipt-$SUFFIX
SA=sapbipt$SUFFIX
APP=func-pbipt-$SUFFIX
PLAN=plan-pbipt-$SUFFIX

az group create -n $RG -l $LOCATION

# Storage (Function App requires one, and we reuse it for the silver CSV)
az storage account create -n $SA -g $RG -l $LOCATION --sku Standard_LRS
az storage container create --account-name $SA -n page-telemetry --auth-mode login

# Key Vault
az keyvault create -n $KV -g $RG -l $LOCATION --enable-rbac-authorization true

# Store the SP secrets (you'll be prompted)
az keyvault secret set --vault-name $KV --name pbi-tenant-id     --value "<your-tenant-guid>"
az keyvault secret set --vault-name $KV --name pbi-client-id     --value "<your-sp-client-id>"
az keyvault secret set --vault-name $KV --name pbi-client-secret --value "<your-sp-secret>"

# Function App (Python 3.11 consumption plan)
az functionapp plan create -g $RG -n $PLAN --sku Y1 --is-linux true
az functionapp create -g $RG -n $APP --plan $PLAN \
    --runtime python --runtime-version 3.11 \
    --functions-version 4 --storage-account $SA \
    --assign-identity '[system]'

# Capture the Function App's principalId
PID=$(az functionapp identity show -g $RG -n $APP --query principalId -o tsv)

# Grant RBAC
KV_ID=$(az keyvault show -n $KV -g $RG --query id -o tsv)
SA_ID=$(az storage account show -n $SA -g $RG --query id -o tsv)
az role assignment create --assignee $PID --role "Key Vault Secrets User"           --scope $KV_ID
az role assignment create --assignee $PID --role "Storage Blob Data Contributor"    --scope $SA_ID

# App settings (Key Vault references resolve at runtime)
az functionapp config appsettings set -g $RG -n $APP --settings \
    "PBI_TENANT_ID=@Microsoft.KeyVault(SecretUri=https://$KV.vault.azure.net/secrets/pbi-tenant-id/)" \
    "PBI_CLIENT_ID=@Microsoft.KeyVault(SecretUri=https://$KV.vault.azure.net/secrets/pbi-client-id/)" \
    "PBI_CLIENT_SECRET=@Microsoft.KeyVault(SecretUri=https://$KV.vault.azure.net/secrets/pbi-client-secret/)" \
    "PBI_OUTPUT_BLOB_URL=https://$SA.blob.core.windows.net/page-telemetry/silver/page_views.csv" \
    "PBI_SCHEDULE_CRON=0 0 6 * * *" \
    "AzureWebJobsFeatureFlags=EnableWorkerIndexing"
```

### 2. Publish the Function

```powershell
# From this folder (deploy/azure-function/)
./deploy.ps1 -ResourceGroup $RG -FunctionApp $APP
```

…or on macOS / Linux:

```bash
./deploy.sh $RG $APP
```

The script:

1. Copies `../../etl/collector.py` into this folder so the publish zip
   includes it (we don't commit it here to avoid drift).
2. Runs `func azure functionapp publish` with the Python runtime.
3. Removes the local `collector.py` copy.

### 3. Verify

```bash
# Confirm the function is registered
az functionapp function show -g $RG -n $APP --function-name daily_page_telemetry

# Stream logs
az functionapp logs tail -g $RG -n $APP

# Manually trigger (TimerTrigger HTTP admin endpoint)
KEY=$(az functionapp keys list -g $RG -n $APP --query "masterKey" -o tsv)
curl -X POST "https://$APP.azurewebsites.net/admin/functions/daily_page_telemetry?code=$KEY" \
     -H "Content-Type: application/json" -d "{}"
```

In the streamed logs you should see:

```
Starting collector — output dir: /tmp/pbi-page-telemetry-XXXXXX
[workspace] Clinical Operations (ws-clinops)
  [report] ...
Silver CSV written (NNNN bytes); uploading to blob …
Upload complete: https://<sa>.blob.core.windows.net/page-telemetry/silver/page_views.csv
```

### 4. Wire to Fabric (optional but typical)

In Fabric → your governance Lakehouse → **+ Get data** → **New shortcut** →
**Azure Data Lake Storage Gen2** → point at
`https://<sa>.blob.core.windows.net/page-telemetry/`.

The silver CSV now shows up as a Files entry; promote it to a Delta table
with a simple notebook (see `deploy/fabric-notebook/PageTelemetryCollector.Notebook.py`
for the MERGE SQL).

## Local debugging

```powershell
# From this folder
copy local.settings.json.example local.settings.json
# Edit local.settings.json with real values (it is gitignored)
copy ..\..\etl\collector.py .

# Install deps
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Run the host locally
func start
```

In another terminal, manually trigger the timer:

```bash
curl -X POST http://localhost:7071/admin/functions/daily_page_telemetry \
     -H "Content-Type: application/json" -d "{}"
```

Remove the local `collector.py` copy before committing anything.

## Cost

- **Consumption plan**: ~$0.50/month for a single daily run (well under the
  free grant of 1M executions + 400,000 GB-s).
- **Key Vault**: ~$0.03 per 10,000 secret operations — daily runs cost cents.
- **Storage**: the silver CSV is ~50 KB per tenant per day. Negligible.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Required app setting 'PBI_TENANT_ID' is missing` | Key Vault reference didn't resolve. Check Managed Identity has `Key Vault Secrets User` and the SecretUri is correct (case-sensitive). |
| `azure.identity.exceptions.CredentialUnavailableError` | Function App doesn't have a Managed Identity, or it isn't assigned to the right Key Vault. |
| `403 ManagedIdentityCredential authentication unavailable` on blob upload | Missing `Storage Blob Data Contributor` on the storage account for the Function App's MI. |
| Function appears in portal but never runs | `AzureWebJobsFeatureFlags=EnableWorkerIndexing` not set — required for the v2 Python programming model. |
| `ModuleNotFoundError: No module named 'collector'` | `deploy.ps1` / `deploy.sh` didn't copy `collector.py` before publish. Run from this folder. |
| Collector errors with `AADSTS700016` / `AADSTS7000215` | See [`../../docs/deployment-guide.md#9-troubleshooting`](../../docs/deployment-guide.md#9-troubleshooting). |
