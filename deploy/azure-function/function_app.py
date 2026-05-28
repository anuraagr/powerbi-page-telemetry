"""Azure Function entry point — daily Power BI page-telemetry collection.

This is a v2 Python programming model Function App. The TimerTrigger fires
once a day at 06:00 UTC and runs the same `collector.py` you'd use locally,
writing the silver CSV to a mounted output directory (Azure Blob via
azure-storage-blob, or a Files share mounted by the Function App).

Set these app settings (use Key Vault references for secrets):

    PBI_TENANT_ID         @Microsoft.KeyVault(SecretUri=https://<kv>.vault.azure.net/secrets/pbi-tenant-id/)
    PBI_CLIENT_ID         @Microsoft.KeyVault(SecretUri=https://<kv>.vault.azure.net/secrets/pbi-client-id/)
    PBI_CLIENT_SECRET     @Microsoft.KeyVault(SecretUri=https://<kv>.vault.azure.net/secrets/pbi-client-secret/)
    PBI_OUTPUT_BLOB_URL   https://<storage>.blob.core.windows.net/page-telemetry/silver/page_views.csv
    PBI_SCHEDULE_CRON     0 0 6 * * *      # (NCRONTAB - sec min hr dom mon dow) optional override
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

import azure.functions as func

app = func.FunctionApp()

DEFAULT_CRON = "0 0 6 * * *"  # 06:00 UTC daily


@app.timer_trigger(
    schedule=os.environ.get("PBI_SCHEDULE_CRON", DEFAULT_CRON),
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def daily_page_telemetry(timer: func.TimerRequest) -> None:
    """Runs the collector once and uploads the silver CSV to blob storage."""
    if timer.past_due:
        logging.warning("Timer is past due — running anyway.")

    for var in ("PBI_TENANT_ID", "PBI_CLIENT_ID", "PBI_CLIENT_SECRET"):
        if not os.environ.get(var):
            raise RuntimeError(
                f"Required app setting '{var}' is missing — wire it via Key Vault "
                "reference in the Function App configuration."
            )

    # Import collector lazily so a missing copy at deploy time surfaces as a
    # clean runtime error inside an invocation, rather than as an opaque
    # "Worker failed to function index" at host startup that prevents the
    # function from ever being visible in the portal.
    try:
        from collector import main as collector_main  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "collector.py not packaged alongside function_app.py. "
            "Run deploy.ps1 / deploy.sh from this folder, or copy "
            "../../etl/collector.py here before `func azure functionapp publish`."
        ) from exc

    with tempfile.TemporaryDirectory(prefix="pbi-page-telemetry-") as tmp:
        os.environ["PBI_OUTPUT_DIR"] = tmp
        sys.argv = ["collector.py"]  # all config comes from env vars

        logging.info("Starting collector — output dir: %s", tmp)
        exit_code = collector_main()
        if exit_code != 0:
            raise RuntimeError(f"collector exited {exit_code}")

        silver_csv = Path(tmp) / "silver" / "page_views.csv"
        if not silver_csv.exists():
            raise RuntimeError(f"collector finished but produced no silver CSV at {silver_csv}")

        size = silver_csv.stat().st_size
        logging.info("Silver CSV written (%d bytes); uploading to blob …", size)

        blob_url = os.environ.get("PBI_OUTPUT_BLOB_URL")
        if not blob_url:
            logging.warning(
                "PBI_OUTPUT_BLOB_URL not set — silver CSV stays in the Function's "
                "temp dir and will be cleaned up on exit. Configure blob storage "
                "for production."
            )
            return

        _upload_to_blob(silver_csv, blob_url)
        logging.info("Upload complete: %s", blob_url)


def _upload_to_blob(local_path: Path, blob_url: str) -> None:
    """Upload using the Function App's Managed Identity. No secrets stored locally."""
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobClient

    credential = DefaultAzureCredential()
    blob = BlobClient.from_blob_url(blob_url, credential=credential)
    with local_path.open("rb") as f:
        blob.upload_blob(f, overwrite=True)
