# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.1]

### Fixed (Jon at Incyte v0.3.0 feedback: *"I cannot see the report names of those unused."*)

- **`silver/unused_pages.csv`** (NEW, 5th silver table) — first-class,
  fully-named list of every page that exists in the catalog and has
  zero views in the window. Same shape as `page_catalog.csv`
  (`workspace_id`, `workspace_name`, `report_id`, `report_name`,
  `page_id`, `page_name`, `page_ordinal`, `catalog_pulled_at`),
  sorted by `(workspace_name, report_name, page_ordinal)` for
  human readability. Replaces (and remains backed by) the in-memory
  LEFT JOIN that v0.3.0 ran in `run()` but threw away after counting.
  Any downstream consumer — the Power BI template, Fabric SQL, the
  bundled dashboard, an email-to-owner Logic App — now reads one
  table with no joins to surface the actual names.
- **`dashboard/PageUsageDashboard.html`** — new **Unused pages** tab
  before the existing Underused tab, listing every zero-view page
  with workspace + report + page name + ordinal. A KPI tile was also
  swapped to surface the unused count and affected-reports count.
- **`dashboard/PowerBI/PageTelemetry.Measures.dax`** — `[Unused Pages]`
  is now a one-liner `COUNTROWS('unused_pages')`. No `page_key`
  calculated column or relationship needed for Page 4 of the
  recommended layout — the unused_pages table stands alone. The
  legacy v0.3.0 LEFT-JOIN-EXCEPT pattern is preserved as a
  reference measure for anyone still on schema 1.1.0.
- **`dashboard/PowerBI/PageTelemetry.Connect.pq`** — returns 5 typed
  tables instead of 4; `ExpectedSchemaVersion = "1.2.0"`.
- **`dashboard/PowerBI/README.md`** — Page 4 (Unused Pages) rewritten
  to "drop the unused_pages table on the canvas, no DAX." Quickstart
  bumped from 4 → 5 queries; Fabric mode mentions all 5 Delta tables.
- **`deploy/fabric-notebook/PageTelemetryCollector.Notebook.py`** —
  added 5th block: REPLACE/overwrite for `unused_pages_silver`
  (same dim semantic as page_catalog — yesterday's "unused" list
  isn't useful once pages are deleted or opened, so each daily run
  is the truth). `COLLECTOR_REF = "v0.3.1"`,
  `EXPECTED_SCHEMA_VERSION = "1.2.0"`.
- **`_run_summary.json` new key**: `unused_pages_sample` (top-10 list
  with full names, inline in the summary for quick eyeballing of
  the first dead pages without opening the CSV).
- **`tests/test_v030_grains.py`** — 5 new tests pinning the
  invariant: unused_pages.csv exists, row count == summary's
  `unused_pages` count == 10, every row has non-empty
  workspace_name / report_name / page_name, set equality with the
  page_catalog – page_views LEFT JOIN, sorted by (ws, report,
  ordinal), byte-identical across mock runs.

### Changed

- **`SILVER_SCHEMA_VERSION = "1.2.0"`** — additive minor bump. Any
  v0.3.0 reader of the existing 4 silver tables keeps working
  (columns and grain unchanged); new readers can use the 5th table.
- **`EXPECTED_SCHEMA_VERSION = "1.2.0"`** in the Fabric notebook.
  Reads of older 1.1.0 silver still load the existing 4 tables and
  skip `unused_pages_silver` cleanly (the file simply isn't there).

### Documentation

- Updated `docs/data-dictionary.md`, `docs/pii-and-retention.md`,
  `docs/api-reference.md`, `docs/gold-queries.md` to document the
  5th table and explain it's a collector-side LEFT JOIN derivation
  (no extra Power BI REST call beyond what v0.3.0 already makes).
  Section 1a of the gold cookbook now offers the simpler
  `SELECT * FROM unused_pages_silver` query alongside the legacy
  LEFT JOIN equivalent.

## [0.3.0]

### Added (the headline: page_catalog → unused-page detection)

- **`silver/page_catalog.csv`** — every page that exists in every report,
  sourced from `GET /v1.0/myorg/groups/{ws}/reports/{rep}/pages` (a
  documented, GA Power BI REST endpoint — NOT preview, NOT DAX). This
  is the table that lets a LEFT JOIN against `page_views` surface
  pages with **zero views in the window** — directly answering Jon at
  Incyte's "which pages in our 60-page clinical-trial report has
  nobody opened?" question, which the v0.2.x page-views-only model
  couldn't.
- **`silver/report_views.csv`** — report-level grain (no page
  dimension) from the `'Report views'` table in the same Modern Usage
  Metrics semantic model. Gives parity with the cards in the existing
  auto-generated per-report Usage Metrics report and enables
  `[Pages Per Session]` as a measure.
- **`silver/user_views.csv`** — per-user grain (hashed UPN). Each row
  is one `(report, user_id_hash, day)` with `view_count` and
  `distinct_pages_viewed`. Powers the new User Analytics dashboard
  page. UPNs are **never** written to disk — only the SHA-256-of-
  lowercased-UPN, first 16 hex chars, is silvered.
- **3 new dataclasses + 3 new adapter methods**: `PageCatalogRow`,
  `ReportViewRow`, `UserViewRow`; `CollectorAdapter.list_report_pages()`,
  `query_report_views()`, `query_user_views()`. Defaults yield nothing
  on the base class so v0.2.x adapter consumers stay source-compatible.
- **`_run_summary.json` new keys** (additive — v0.2.x keys preserved):
  `page_view_rows`, `page_catalog_rows`, `report_view_rows`,
  `user_view_rows`, `unused_pages`, `reports_with_unused_pages`,
  `silver_paths` (dict of all 4 file paths).
- **`etl/sample_data/unused_pages.json`** — intentionally-dead-pages
  overlay for mock mode. Names like "Protocol v1 (legacy)",
  "DEBUG: per-site raw rates", "Enrollment funnel (deprecated)"
  across the 3 clinical-trial reports. Makes the v0.3.0 demo punch
  visible end-to-end with `--mock`.
- **`dashboard/PowerBI/`** rewritten for v0.3.0:
  - `PageTelemetry.Connect.pq` now returns a **record** with all 4
    silver tables and a single connection-mode toggle for
    fabric/blob/local. Schema-version pin bumped to `1.1.0`.
  - `PageTelemetry.Measures.dax` adds `[Unused Pages]`,
    `[% Pages Unused]`, `[Reports With Unused Pages]`,
    `[Pages Per Session]`, `[Avg Session Seconds]`, `[Unique Users]`,
    `[Power Users]`, `[Avg Pages Per User]` measures. Refactored
    page-views measures to reference unqualified table names so the
    measures work whether you load as `page_views_silver` or
    `page_views`.
  - `README.md` rewritten with a 5-page recommended layout
    (Overview / Report Drill / Page Drill / **Unused Pages** /
    User Analytics) and step-by-step setup including the recommended
    `page_key` calculated column for the relationship.
- **`deploy/fabric-notebook/PageTelemetryCollector.Notebook.py`** —
  MERGE blocks for all 4 silver tables. `page_views_silver`,
  `report_views_silver`, `user_views_silver` use INSERT/UPDATE MERGE.
  `page_catalog_silver` uses REPLACE (whole-table overwrite) because
  the catalog is a current-state dimension, not a fact table.
  `EXPECTED_SCHEMA_VERSION = "1.1.0"`. `COLLECTOR_REF` stays at
  `v0.2.0` and bumps to `v0.3.0` in a follow-up commit after the tag
  is pushed (chicken-and-egg).
- **`tests/test_v030_grains.py`** — 20 new tests covering the new
  schema, summary keys, byte-identical mock determinism for all 4
  silver files, the LEFT-JOIN math for unused-page detection, REST
  endpoint correctness with 403 tolerance, UPN hashing case-
  insensitivity and blank handling, and column-shape guarantees on
  each new silver table.

### Changed

- **`SILVER_SCHEMA_VERSION = "1.1.0"`** — additive minor bump. Any
  v0.2.x reader of `page_views.csv` keeps working (columns unchanged);
  new readers can use the 3 new silver tables.
- **Bronze layout** — `bronze/dt=YYYY-MM-DD/{wsId}__{reportId}.csv`
  in v0.2.x is now `bronze/dt=YYYY-MM-DD/{feed}/{wsId}__{reportId}.csv`
  with `feed ∈ {page_views, page_catalog, report_views, user_views}`.
  Silver is the contract — no external consumer reads bronze directly —
  but if you have a bespoke bronze reader, update its path pattern.
- **`_write_rows`** now writes an empty-but-header file (with schema
  comment) when the rows iterator is empty, so downstream MERGE jobs
  can distinguish "file exists, schema compatible, no new data" from
  "collector didn't run / file missing".

### Documentation

- Rewrote `docs/data-dictionary.md` for v0.3.0: 4 tables (was 1),
  source-of-truth pointer updated to the Modern Usage Metrics
  per-workspace semantic model, full PII column on every column,
  full `_run_summary.json` schema with v0.2.x back-compat aliases
  called out.

## [0.2.0]

### Breaking (live mode only — mock mode, silver schema, downstream all unchanged)

- **Rewrote `LiveAdapter` against the Modern Usage Metrics
  (preview) per-workspace semantic model.** v0.1.0 attempted to call
  `POST /admin/reports/{id}/usageMetrics` to idempotently provision a
  per-report Usage Metrics v2 dataset; **that REST endpoint does not
  exist in the public Power BI API** (verified against the Microsoft
  Learn `/rest/api/power-bi/admin` surface and confirmed by Power BI
  PM David Browne in the HLS Roundtable, May 2026). The fix:
  enumerate workspaces via admin REST, look up each workspace's
  `Usage Metrics Report` semantic model (auto-provisioned by Power BI
  on the first portal click of `... → View usage metrics report`),
  and run a filtered DAX `CALCULATETABLE(SUMMARIZECOLUMNS(...))` per
  report via `POST /datasets/{id}/executeQueries`.
- `LiveAdapter.ensure_usage_metrics_dataset()` now does a
  **per-workspace** lookup (cached), returns `""` for workspaces that
  haven't been bootstrapped, and logs a friendly warning explaining
  the one-time portal-click prerequisite.
- `LiveAdapter.query_page_views(...)` now takes a required
  `report_id` kwarg so the DAX filters down at the source. Calling
  without `report_id` raises `ValueError` (would otherwise scan an
  entire workspace's worth of page-views).
- `MockAdapter.query_page_views(...)` accepts and ignores the new
  optional `report_id` kwarg for signature symmetry.
- `_run_summary.json` now includes `workspaces_not_bootstrapped: list[str]`
  and `reports_skipped_no_bootstrap: int` for ops visibility.

### Added

- **`PBI_USAGE_DATASET_NAME`** env var — override the dataset name
  prefix (default `Usage Metrics Report`). Set to
  `Usage Metrics Report v2` for tenants on the legacy variant.
- **`PBI_USE_PYADOMD`** env var — explicit opt-in to the
  pyadomd / XMLA query path. Previously implicit on pyadomd presence,
  now explicit so the default REST path is deterministic across
  environments where pyadomd happens to be installed.
- **`PBI_MOCK`** env var — env-var equivalent of `--mock` so
  containerized deploys (Azure Function, Container Apps) can be
  smoke-tested with no credentials before being cut over to live mode.
- **`PBI_SAMPLE_CSV`** env var — explicit override for the bundled
  sample CSV's location, used by containerized deploys where
  `collector.py` is vendored away from its `sample_data/` folder.
- **Robust sample-CSV path resolver** — `MockAdapter` now searches
  several candidate paths (next to `collector.py`, repo `etl/`, two
  levels up for `deploy/<option>/collector.py`). Fixes mock mode
  inside the Azure Function deploy where `deploy.ps1` / `deploy.sh`
  copy `collector.py` into the Function root.
- **`--mock` / `-Mock` switches** on the local deploy wrappers
  (`run-collector.sh` / `run-collector.ps1`) so evaluators can test
  the full deployment without Power BI credentials.
- **`.gitattributes`** forcing LF line endings on shell scripts,
  systemd units, and YAML/JSON. Fixes a real bug where Windows-cloned
  copies of the repo couldn't run the bash wrappers on Linux.
- **`python3` fallback** in `run-collector.sh` (prefers `python3`,
  falls back to `python`). Ubuntu 22.04+ ships `python3` without a
  `python` symlink, which previously broke the bash wrapper there.
- **`tests/test_live_adapter.py`** — 6 new tests covering the new
  per-workspace lookup, executeQueries body shape, cached lookup,
  empty-dataset-id noop, `report_id`-required guard, and end-to-end
  run() summary accounting.

### Fixed

- `_run_summary.json["datasets"]` now counts **unique discovered
  datasets** (= unique bootstrapped workspaces) rather than once per
  report. Two reports in the same workspace no longer double-count
  the workspace's UM dataset.

### Documentation

- Rewrote `docs/api-reference.md` §2-§8 against the per-workspace
  Modern Usage Metrics model. Added the David Browne quote and the
  one-time portal-bootstrap prerequisite prominently in §2-§3.
- Added the bootstrap prereq to `docs/deployment-guide.md §0` and a
  new dedicated §1a "(HISTORICAL) Why v0.1.0's POST endpoint didn't
  exist" callout in `docs/design.md`.
- New `docs/runbook.md §H` "workspaces_not_bootstrapped listed"
  scenario with bulk-bootstrap guidance and the
  `PBI_USAGE_DATASET_NAME` legacy override.
- README now opens with the David Browne quote, makes the bootstrap
  prereq part of the quick start, and updates the Mermaid diagram to
  reflect the per-workspace model.

### Added (from previous Unreleased)

- **Tier 3 polish:**
  - Mermaid architecture diagram inline in README (renders on GitHub),
    plus the existing PNG re-encoded from 3.4 MB → 243 KB (lanczos +
    64-color palette).
  - `docs/runbook.md` — on-call response playbook with 7 named
    incident scenarios, severity ladder, and the exact diagnostic
    commands per scenario.
  - `docs/design.md` — 11 architectural decisions explained
    (REST executeQueries vs ADOMD.NET, MERGE vs append, MI vs SP,
    CSV vs Parquet, adapter pattern, Fabric tag-pinning, etc.) plus
    what we considered and rejected.
  - `docs/gold-queries.md` — 6 ready-to-paste DAX/SQL recipes:
    underused-pages top 10, report half-life, capacity long-tail,
    page-load funnel, new-page adoption velocity, workspace-owner
    accountability.
  - `NOTICE.md` — third-party attribution (Chart.js MIT, Python
    runtime deps with license + upstream URLs, dev tooling, diagram
    source).
  - `--metrics {none,appinsights,prometheus}` flag on `collector.py`
    emits a one-line greppable summary for log scrapers — App Insights
    `customMetric` JSON or Prometheus exposition format with
    `pbi_page_telemetry_*` gauges.
  - `--theme {healthcare,generic,financial}` flag on
    `generate_sample_data.py` regenerates the synthetic data with
    domain-specific workspace / report / page names (SaaS-product or
    capital-markets vocab) while preserving traffic patterns.
    Default (`healthcare`) regenerates the bundled fixture
    byte-identically.
  - README now opens with a Mermaid architecture diagram, an audience
    documentation matrix linking to each new doc, and `--theme` /
    `--metrics` examples in the quick-start.

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
[0.2.0]: https://github.com/anuraagr/powerbi-page-telemetry/releases/tag/v0.2.0
[0.3.0]: https://github.com/anuraagr/powerbi-page-telemetry/releases/tag/v0.3.0
[0.3.1]: https://github.com/anuraagr/powerbi-page-telemetry/releases/tag/v0.3.1
[Unreleased]: https://github.com/anuraagr/powerbi-page-telemetry/compare/v0.3.1...HEAD
