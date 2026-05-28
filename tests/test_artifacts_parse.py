"""Syntactic validity of every deployable artifact.

Catches the failure mode where a `deploy/<option>/` artifact diverges
from the collector and breaks at deploy time instead of at PR review.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

JSON_FILES = [
    REPO_ROOT / "deploy" / "fabric-notebook" / "data-pipeline.json",
    REPO_ROOT / "deploy" / "azure-function" / "host.json",
    REPO_ROOT / "deploy" / "azure-function" / "local.settings.json.example",
    REPO_ROOT / "dashboard" / "page_views.json",
]

PY_FILES = [
    REPO_ROOT / "etl" / "collector.py",
    REPO_ROOT / "etl" / "generate_sample_data.py",
    REPO_ROOT / "etl" / "aggregate_for_dashboard.py",
    REPO_ROOT / "dashboard" / "bundle.py",
    REPO_ROOT / "deploy" / "azure-function" / "function_app.py",
    REPO_ROOT / "deploy" / "fabric-notebook" / "PageTelemetryCollector.Notebook.py",
]


@pytest.mark.parametrize("path", JSON_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_json_artifacts_are_valid(path: Path) -> None:
    json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_python_artifacts_parse(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"))


def test_fabric_pipeline_has_required_fields() -> None:
    p = REPO_ROOT / "deploy" / "fabric-notebook" / "data-pipeline.json"
    body = json.loads(p.read_text(encoding="utf-8"))
    assert body["name"] == "PageTelemetryDailyPipeline"
    activities = body["properties"]["activities"]
    assert len(activities) == 1
    assert activities[0]["type"] == "TridentNotebook"


def test_function_host_uses_extension_bundle_v4() -> None:
    p = REPO_ROOT / "deploy" / "azure-function" / "host.json"
    body = json.loads(p.read_text(encoding="utf-8"))
    bundle = body["extensionBundle"]["version"]
    assert bundle.startswith("[4."), f"host.json should pin bundle v4, got {bundle!r}"


def test_function_local_settings_template_redacted() -> None:
    """Make sure nobody committed a real local.settings.json by accident.
    Only secret-bearing fields are required to be redacted; cron schedules
    and similar non-secret operational settings can be real values."""
    p = REPO_ROOT / "deploy" / "azure-function" / "local.settings.json.example"
    body = json.loads(p.read_text(encoding="utf-8"))
    values = body.get("Values", {})
    secret_fields = {"PBI_TENANT_ID", "PBI_CLIENT_ID", "PBI_CLIENT_SECRET", "PBI_OUTPUT_BLOB_URL"}
    for k in secret_fields:
        assert k in values, f"local.settings.json.example missing required field {k!r}"
        v = values[k]
        assert "<" in v and ">" in v, (
            f"local.settings.json.example has a non-template value for {k!r}: {v!r}"
        )


def test_deploy_readme_links_resolve_locally() -> None:
    """Each per-option README references files in the same folder — make
    sure none have rotted."""
    for option in ("fabric-notebook", "azure-function", "local"):
        readme = REPO_ROOT / "deploy" / option / "README.md"
        assert readme.exists(), f"missing {readme.relative_to(REPO_ROOT)}"
