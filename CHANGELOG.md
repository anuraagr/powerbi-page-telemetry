# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Tier 2 hygiene pass: pytest suite (`tests/`), GitHub Actions CI matrix
  (3.10/3.11/3.12 × ubuntu/windows), `SECURITY.md`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `docs/data-dictionary.md`, `docs/pii-and-retention.md`,
  `dashboard/PowerBI/` template assets (Power Query M + DAX measures +
  README), `CHANGELOG.md`.

## [0.1.0] — 2026-05-28

First tagged release. Production-grade live-mode collector + three deploy
options + offline demo + customer-readiness pass.

### Added

- **Collector** (`etl/collector.py`)
  - `LiveAdapter` and `MockAdapter` sharing the same row contract
  - Live mode executes DAX via Power BI REST
    `POST /datasets/{id}/executeQueries` (no native deps) with an
    optional pyadomd XMLA fallback when `PBI_USE_PYADOMD=1`
  - Exponential-backoff retry on 429 / 5xx that honors `Retry-After`
  - LRO polling on `POST /admin/reports/{id}/usageMetrics` 202 responses
  - `SILVER_SCHEMA_VERSION = "1.0.0"` emitted in `_run_summary.json` and
    as a `# silver_schema_version=...` comment line in
    `silver/page_views.csv`
  - Date-partitioned bronze layer (`bronze/dt=YYYY-MM-DD/...`)
  - Friendly AAD error messages, UTF-8 console fixes, `--mock` /
    `--days` / `--out` CLI
  - Deterministic mock-mode dates derived from the bundled CSV
- **Deploy wrappers** (`deploy/`)
  - `fabric-notebook/`: Fabric notebook source + Data Pipeline JSON +
    README; pins `COLLECTOR_REF`, caches the downloaded collector to
    Lakehouse Files, asserts `EXPECTED_SCHEMA_VERSION`
  - `azure-function/`: Functions v2 TimerTrigger + `host.json` +
    `requirements.txt` + `local.settings.json.example` + `deploy.ps1` /
    `deploy.sh` + full `az` provisioning README; deferred collector
    import for clean error surfaces
  - `local/`: PowerShell wrapper with `Microsoft.PowerShell.SecretManagement`,
    bash wrapper + systemd `.service` + `.timer` unit pair, env example,
    README for Windows / Linux / macOS
  - Top-level `deploy/README.md` options index
- **Dashboard** (`dashboard/`)
  - Self-contained `PageUsageDashboard.html` (Chart.js + Clawpilot theme)
  - `bundle.py` inlines `page_views.json` into the template
  - Inline SVG favicon
- **Documentation**
  - `docs/deployment-guide.md` — end-to-end tenant standup
  - `docs/api-reference.md` — REST endpoints, DAX query, RBAC,
    throttling
  - `docs/dashboard-*.png` screenshots
  - Architecture diagram (`architecture.excalidraw` + `.png`)
- **Sample data**: 15 reports across 5 workspaces, 90 days,
  15,480 rows / 154,815 views / 231 pages / 9 never-viewed / 67
  underused — fully deterministic CRC32-seeded generator

[0.1.0]: https://github.com/anuraagr/powerbi-page-telemetry/releases/tag/v0.1.0
[Unreleased]: https://github.com/anuraagr/powerbi-page-telemetry/compare/v0.1.0...HEAD
