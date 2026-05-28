# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse_name": "PageTelemetry",
# META       "known_lakehouses": []
# META     }
# META   }
# META }

# MARKDOWN ********************

# # Power BI Page Telemetry — daily collector
#
# This notebook is meant to be scheduled by a **Fabric Data Pipeline** with a
# single Notebook activity. It:
#
# 1. Pulls secrets from the workspace's linked **Azure Key Vault**.
# 2. Downloads the latest pinned release of `collector.py` from the public repo.
# 3. Runs the collector against every workspace the service principal can see.
# 4. Lands the silver CSV in the attached lakehouse's `Files/silver/` folder.
# 5. Promotes the silver CSV to a Delta table (`page_views_silver`) so DirectLake
#    / Direct Query reports can read it without an additional refresh.
#
# **One-time setup** (see `deploy/fabric-notebook/README.md`):
#
# - Service principal with `Tenant.Read.All` + workspace access
# - Key Vault with secrets: `pbi-tenant-id`, `pbi-client-id`, `pbi-client-secret`
# - Lakehouse named `PageTelemetry` attached to this notebook (default lakehouse)

# CELL ********************

# Pin the collector release. Change this to a tagged release (e.g. v0.2.0)
# for production; keep `main` for "always latest".
COLLECTOR_REF = "main"
KEYVAULT_URL  = "https://<your-keyvault>.vault.azure.net/"

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# MAGIC %pip install --quiet "requests>=2.31"

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

import os
import sys
import urllib.request
from pathlib import Path

# Pull SP credentials from Key Vault using Fabric's built-in helper.
# `notebookutils` is injected by the Fabric runtime; running locally will fail
# (which is intentional - this notebook only makes sense inside Fabric).
PBI_TENANT_ID     = notebookutils.credentials.getSecret(KEYVAULT_URL, "pbi-tenant-id")        # noqa: F821
PBI_CLIENT_ID     = notebookutils.credentials.getSecret(KEYVAULT_URL, "pbi-client-id")        # noqa: F821
PBI_CLIENT_SECRET = notebookutils.credentials.getSecret(KEYVAULT_URL, "pbi-client-secret")    # noqa: F821

os.environ["PBI_TENANT_ID"]     = PBI_TENANT_ID
os.environ["PBI_CLIENT_ID"]     = PBI_CLIENT_ID
os.environ["PBI_CLIENT_SECRET"] = PBI_CLIENT_SECRET

# Output goes into the attached lakehouse's Files/ root so the silver CSV is
# directly browsable in the Lakehouse explorer.
OUTPUT_DIR = "/lakehouse/default/Files/page_telemetry"
os.environ["PBI_OUTPUT_DIR"] = OUTPUT_DIR
Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Download collector.py from the public repo at the pinned ref. We do this at
# runtime rather than bundling it into the notebook so an `etl/collector.py`
# bug fix lands on the next scheduled run without re-importing the notebook.
COLLECTOR_URL = (
    f"https://raw.githubusercontent.com/anuraagr/powerbi-page-telemetry/"
    f"{COLLECTOR_REF}/etl/collector.py"
)
local_collector = Path("/tmp/collector.py")
urllib.request.urlretrieve(COLLECTOR_URL, local_collector)
print(f"Fetched collector ({local_collector.stat().st_size:,} bytes) from {COLLECTOR_URL}")

sys.path.insert(0, str(local_collector.parent))
import importlib
if "collector" in sys.modules:
    del sys.modules["collector"]
collector = importlib.import_module("collector")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Run the collector. --days 1 because we run daily; the silver layer below
# handles deduplication if you want longer windows.
sys.argv = [
    "collector.py",
    "--tenant",        PBI_TENANT_ID,
    "--client-id",     PBI_CLIENT_ID,
    "--client-secret", PBI_CLIENT_SECRET,
    "--days",          "1",
    "--out",           OUTPUT_DIR,
]
exit_code = collector.main()
if exit_code != 0:
    raise RuntimeError(f"collector.main() exited with code {exit_code} — see logs above")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Promote the silver CSV to a Delta table in the attached lakehouse so
# DirectLake reports pick it up immediately. The collector writes a single
# fully-conformed CSV at `silver/page_views.csv`.
from pyspark.sql.functions import col, to_date

silver_csv = f"Files/page_telemetry/silver/page_views.csv"

df = (
    spark.read                                  # noqa: F821
         .option("header", "true")
         .option("inferSchema", "true")
         .csv(silver_csv)
         .withColumn("view_date", to_date(col("view_date")))
)

# MERGE upsert so partial-day reruns don't duplicate. Schema and grain match
# what `etl/collector.py` emits: one row per (workspace, report, page, view_date).
target_table = "page_views_silver"
df.createOrReplaceTempView("incoming_page_views")

spark.sql(f"""                                  -- noqa: F821
CREATE TABLE IF NOT EXISTS {target_table}
USING DELTA
AS SELECT * FROM incoming_page_views LIMIT 0
""")

spark.sql(f"""                                  -- noqa: F821
MERGE INTO {target_table} t
USING incoming_page_views s
ON  t.workspace_id = s.workspace_id
AND t.report_id    = s.report_id
AND t.page_id      = s.page_id
AND t.view_date    = s.view_date
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
""")

row_count = spark.sql(f"SELECT COUNT(*) FROM {target_table}").collect()[0][0]  # noqa: F821
print(f"page_views_silver now holds {row_count:,} rows")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
