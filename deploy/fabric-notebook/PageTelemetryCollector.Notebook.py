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

# Pin the collector release. Production should use a tagged release (e.g. v0.1.0);
# use "main" only during development. The notebook caches the downloaded file to
# the lakehouse so production keeps running even if GitHub is unreachable.
COLLECTOR_REF = "v0.3.0"  # pinned to the released tag — bump after each release
KEYVAULT_URL  = "https://<your-keyvault>.vault.azure.net/"

# Silver schema version this notebook is built for. Must match
# `SILVER_SCHEMA_VERSION` in the collector; if upstream bumps it,
# revisit the MERGE blocks in the final cell before promoting.
# v0.3.0 = additive (3 new silver tables: page_catalog, report_views, user_views)
EXPECTED_SCHEMA_VERSION = "1.1.0"

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

# Download collector.py from the public repo at the pinned ref. Cache it to
# the lakehouse Files/_cache/ folder so production keeps running even if
# GitHub is briefly unreachable. To force a re-download (e.g. after bumping
# COLLECTOR_REF), delete the cached file in the Lakehouse explorer.
COLLECTOR_URL = (
    f"https://raw.githubusercontent.com/anuraagr/powerbi-page-telemetry/"
    f"{COLLECTOR_REF}/etl/collector.py"
)
CACHE_DIR = Path("/lakehouse/default/Files/_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
cached_collector = CACHE_DIR / f"collector.{COLLECTOR_REF}.py"
local_collector = Path("/tmp/collector.py")

if cached_collector.exists() and cached_collector.stat().st_size > 0:
    print(f"Using cached collector at {cached_collector}")
    local_collector.write_bytes(cached_collector.read_bytes())
else:
    try:
        urllib.request.urlretrieve(COLLECTOR_URL, local_collector)
        cached_collector.write_bytes(local_collector.read_bytes())
        print(f"Fetched collector ({local_collector.stat().st_size:,} bytes) from {COLLECTOR_URL}; cached to {cached_collector}")
    except Exception as exc:
        raise RuntimeError(
            f"Could not download collector from {COLLECTOR_URL} and no cache "
            f"exists at {cached_collector}. Check network access from Fabric "
            "to GitHub, or pre-upload the file to the lakehouse cache folder."
        ) from exc

sys.path.insert(0, str(local_collector.parent))
import importlib
if "collector" in sys.modules:
    del sys.modules["collector"]
collector = importlib.import_module("collector")

# Schema-version compatibility check.
actual = getattr(collector, "SILVER_SCHEMA_VERSION", "0.0.0")
if actual != EXPECTED_SCHEMA_VERSION:
    raise RuntimeError(
        f"Collector schema version {actual!r} does not match notebook's "
        f"expected {EXPECTED_SCHEMA_VERSION!r}. Review the final MERGE cell "
        "before unpinning, then bump EXPECTED_SCHEMA_VERSION."
    )

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

# Promote each of the FOUR silver CSVs (v0.3.0) to a Delta table in the
# attached lakehouse so DirectLake reports pick them up immediately. The
# collector writes them to `Files/page_telemetry/silver/`.
#
# Schema (silver_schema_version=1.1.0):
#   page_views.csv      fact (page, day)            MERGE keys: workspace_id, report_id, page_id, view_date
#   page_catalog.csv    dim  (latest catalog wins)  REPLACE strategy: catalog reflects current state
#   report_views.csv    fact (report, day)          MERGE keys: workspace_id, report_id, view_date
#   user_views.csv      fact (hashed user, day)     MERGE keys: report_id, user_id_hash, view_date
from pyspark.sql.functions import col, to_date, to_timestamp

SILVER_DIR = "Files/page_telemetry/silver"

def _read_silver_csv(path: str):
    return (
        spark.read                                  # noqa: F821
             .option("header", "true")
             .option("inferSchema", "true")
             .option("comment", "#")               # skip the schema-version preamble
             .csv(path)
    )

# ---------- 1. page_views (the original v0.1.0 table) ---------------------
df_pv = (
    _read_silver_csv(f"{SILVER_DIR}/page_views.csv")
        .withColumn("view_date", to_date(col("view_date")))
)
df_pv.createOrReplaceTempView("incoming_page_views")

spark.sql("""                                       -- noqa: F821
CREATE TABLE IF NOT EXISTS page_views_silver
USING DELTA
AS SELECT * FROM incoming_page_views LIMIT 0
""")
spark.sql("""                                       -- noqa: F821
MERGE INTO page_views_silver t
USING incoming_page_views s
ON  t.workspace_id = s.workspace_id
AND t.report_id    = s.report_id
AND t.page_id      = s.page_id
AND t.view_date    = s.view_date
WHEN MATCHED     THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
""")

# ---------- 2. page_catalog (v0.3.0 — dimension) --------------------------
# The catalog is a "current state" snapshot of every page that exists in
# every report. We REPLACE the whole table on each run so renamed /
# deleted pages drop out — that's the right semantic for a dim that
# powers the LEFT JOIN against page_views to find unused pages.
import os
catalog_path = f"{SILVER_DIR}/page_catalog.csv"
try:
    df_pc = (
        _read_silver_csv(catalog_path)
            .withColumn("catalog_pulled_at", to_timestamp(col("catalog_pulled_at")))
    )
    df_pc.write.mode("overwrite").format("delta").saveAsTable("page_catalog_silver")
except Exception as e:                              # noqa: BLE001
    # File may not exist on first v0.3.0 run if an old collector ran first.
    print(f"page_catalog.csv not loaded ({e!r}); skipping page_catalog_silver replace")

# ---------- 3. report_views (v0.3.0 — fact) -------------------------------
rv_path = f"{SILVER_DIR}/report_views.csv"
try:
    df_rv = (
        _read_silver_csv(rv_path)
            .withColumn("view_date", to_date(col("view_date")))
    )
    df_rv.createOrReplaceTempView("incoming_report_views")
    spark.sql("""                                   -- noqa: F821
    CREATE TABLE IF NOT EXISTS report_views_silver
    USING DELTA
    AS SELECT * FROM incoming_report_views LIMIT 0
    """)
    spark.sql("""                                   -- noqa: F821
    MERGE INTO report_views_silver t
    USING incoming_report_views s
    ON  t.workspace_id = s.workspace_id
    AND t.report_id    = s.report_id
    AND t.view_date    = s.view_date
    WHEN MATCHED     THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
    """)
except Exception as e:                              # noqa: BLE001
    print(f"report_views.csv not loaded ({e!r}); skipping report_views_silver MERGE")

# ---------- 4. user_views (v0.3.0 — fact, hashed PII) ---------------------
# user_id_hash is the SHA-256 hex prefix of the lowercased UPN (see
# docs/pii-and-retention.md). Treat this column as PII-equivalent for
# RLS / retention purposes even though it's not directly reversible.
uv_path = f"{SILVER_DIR}/user_views.csv"
try:
    df_uv = (
        _read_silver_csv(uv_path)
            .withColumn("view_date", to_date(col("view_date")))
    )
    df_uv.createOrReplaceTempView("incoming_user_views")
    spark.sql("""                                   -- noqa: F821
    CREATE TABLE IF NOT EXISTS user_views_silver
    USING DELTA
    AS SELECT * FROM incoming_user_views LIMIT 0
    """)
    spark.sql("""                                   -- noqa: F821
    MERGE INTO user_views_silver t
    USING incoming_user_views s
    ON  t.workspace_id = s.workspace_id
    AND t.report_id    = s.report_id
    AND t.user_id_hash = s.user_id_hash
    AND t.view_date    = s.view_date
    WHEN MATCHED     THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
    """)
except Exception as e:                              # noqa: BLE001
    print(f"user_views.csv not loaded ({e!r}); skipping user_views_silver MERGE")

# ---------- Row count summary --------------------------------------------
for tname in (
    "page_views_silver",
    "page_catalog_silver",
    "report_views_silver",
    "user_views_silver",
):
    try:
        n = spark.sql(f"SELECT COUNT(*) FROM {tname}").collect()[0][0]  # noqa: F821
        print(f"{tname}: {n:,} rows")
    except Exception as e:                          # noqa: BLE001
        print(f"{tname}: not yet created ({e!r})")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
