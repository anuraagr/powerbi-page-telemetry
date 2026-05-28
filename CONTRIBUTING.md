# Contributing

Thank you for considering a contribution. This is a small reference
project; the bar for changes is "does this make the collector more
useful to a real Power BI / Fabric customer." Personal-preference
refactors and pure cosmetic changes are unlikely to merge.

## Before you start

1. Open an issue describing what you want to change and why. Bug reports
   should include the collector commit SHA, deploy option, and any
   redacted log lines.
2. Wait for a maintainer to confirm direction before opening a PR — this
   saves you and us a lot of time.

## Development setup

Requires **Python 3.10 or later**.

```bash
git clone https://github.com/anuraagr/powerbi-page-telemetry
cd powerbi-page-telemetry
python -m venv .venv
. .venv/bin/activate         # Windows: .venv\Scripts\Activate.ps1
pip install -r etl/requirements.txt
pip install -r tests/requirements.txt

# Run the offline demo end-to-end:
python etl/collector.py --mock

# Run the tests:
pytest -v
```

## Code conventions

- Format / lint with `ruff` (config in `pyproject.toml`).
- Keep the collector single-file. If a change would require splitting
  `etl/collector.py` into a package, raise it in the issue first.
- All public functions get a docstring; complex logic gets a short
  comment explaining "why," not "what".
- Preserve the adapter contract — `LiveAdapter` and `MockAdapter` must
  produce identical downstream row shapes.

## Tests

We use `pytest`. New code should add tests. At minimum:

- New live-mode behaviour: mock `requests.request` with `unittest.mock`
  the way `tests/test_retry_logic.py` does.
- New silver-schema columns: bump `SILVER_SCHEMA_VERSION`, update
  `tests/test_mock_reproducibility.py`, and update the Fabric
  notebook's `EXPECTED_SCHEMA_VERSION` constant.
- New deploy artifact: add a parse / validity check to
  `tests/test_artifacts_parse.py`.

## Commit messages

- Use the imperative: "Add bronze partitioning" not "Added".
- First line ≤ 72 chars; blank line; then a fuller explanation if needed.
- Reference issues with `Fixes #N` or `Refs #N`.

## Release process

Maintainers handle releases. The flow is:

1. Update `CHANGELOG.md` with the new version section.
2. Bump `EXPECTED_SCHEMA_VERSION` in the Fabric notebook only if the
   silver schema changed.
3. Tag: `git tag -a v0.X.0 -m "Release v0.X.0" && git push origin v0.X.0`.
4. Create a GitHub release pointing at the tag.

## Code of conduct

Participation in this project is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).
