"""Sample-CSV path resolution: the collector may be vendored away from
its `sample_data/` folder by deploy scripts (Azure Function copies
collector.py next to function_app.py; Fabric notebook downloads it to
/tmp). The resolver must find the bundled sample CSV via well-known
fallback paths, and the PBI_SAMPLE_CSV env var must always win.
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

import pytest

ETL = Path(__file__).resolve().parent.parent / "etl"
SAMPLE = ETL / "sample_data" / "page_views.csv"


def _reload_collector():
    if "collector" in sys.modules:
        del sys.modules["collector"]
    if str(ETL) not in sys.path:
        sys.path.insert(0, str(ETL))
    return importlib.import_module("collector")


def test_sample_csv_resolves_adjacent_in_dev_layout():
    """Default layout: collector.py is in etl/, sample CSV is in
    etl/sample_data/. Resolver picks the adjacent path."""
    os.environ.pop("PBI_SAMPLE_CSV", None)
    c = _reload_collector()
    assert c.SAMPLE_CSV.exists(), f"expected adjacent sample CSV at {c.SAMPLE_CSV}"
    assert c.SAMPLE_CSV.resolve() == SAMPLE.resolve()


def test_pbi_sample_csv_env_var_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Explicit PBI_SAMPLE_CSV env var must take precedence over every
    other candidate path. Ops use this in containers where collector.py
    is vendored away from sample_data/."""
    custom = tmp_path / "custom_sample.csv"
    shutil.copy(SAMPLE, custom)
    monkeypatch.setenv("PBI_SAMPLE_CSV", str(custom))
    c = _reload_collector()
    assert c.SAMPLE_CSV.resolve() == custom.resolve()
    # And it actually loads — proves MockAdapter accepts whatever the resolver returns.
    adapter = c.MockAdapter()
    assert len(adapter._rows) > 0


def test_sample_csv_resolves_two_levels_up_for_deploy_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Simulates deploy/azure-function/collector.py: there's no adjacent
    sample_data/ but there IS an etl/sample_data/ two levels up. The
    resolver must find it via the _resolve_sample_csv() ladder."""
    monkeypatch.delenv("PBI_SAMPLE_CSV", raising=False)
    fake_deploy = tmp_path / "myrepo" / "deploy" / "azure-function"
    fake_deploy.mkdir(parents=True)
    fake_etl = tmp_path / "myrepo" / "etl" / "sample_data"
    fake_etl.mkdir(parents=True)
    shutil.copy(SAMPLE, fake_etl / "page_views.csv")
    # Drive the resolver directly — no module reload needed (which trips
    # dataclasses' module-cache check on Python 3.13).
    c = _reload_collector()
    resolved = c._resolve_sample_csv(here=fake_deploy)
    assert resolved.exists(), (
        f"resolver failed to find sample CSV from deploy layout: {resolved}"
    )
    assert resolved.resolve() == (fake_etl / "page_views.csv").resolve()


def test_pbi_mock_env_var_equivalent_to_cli_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """PBI_MOCK=1 must put the collector in mock mode identically to --mock,
    so containerized deploys can be smoke-tested with no credentials."""
    monkeypatch.setenv("PBI_MOCK", "1")
    monkeypatch.setenv("PBI_OUTPUT_DIR", str(tmp_path))
    # Make sure live-mode creds are absent so we'd error if mock weren't engaged
    for v in ("PBI_TENANT_ID", "PBI_CLIENT_ID", "PBI_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)
    c = _reload_collector()
    # Run with no args; if --mock weren't honored via env var the missing creds
    # would force a return 2.
    rc = c.main([])
    assert rc == 0, f"PBI_MOCK env var did not engage mock mode (exit={rc})"
    assert (tmp_path / "silver" / "page_views.csv").exists()
