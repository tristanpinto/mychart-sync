# MyChart Sync

Local patient-authorized Epic FHIR sync, with optional Tidepool imports.
[README.md](README.md) is the human introduction; follow [SETUP.md](SETUP.md) when
helping a user install, connect, and download records. Runtime package: `health_sync`.

- For development: `.venv/bin/python -m pip install -e '.[dev]'`, then
  `.venv/bin/python -m pytest -q`. If using uv already, `uv sync --extra dev` and
  `uv run pytest -q` are equivalent. Tests use temporary directories and mocked HTTP.
- Keep parsers and Markdown/JSON schemas stable. Preserve person-scoped routing and tokens.
- Never commit local config, secrets, records, cache, or real clinical test fixtures.
- No live sync, auth, hosting, or publishing as part of tests.
- Legacy `health-sync` CLI and `brain_dir` config aliases remain supported.
- FHIR JSON retains the latest version per hospital/type/ID; missing IDs survive
  full and incremental fetches. Summaries are generated, with backups for prior
  edits. Keep manual notes in `health_profile.md`; never merge Markdown as data.

`bash scripts/sync_person.sh me hospital-a hospital-b -- --dry-run` scopes a batch
and reports partial failures. It enforces person assignments and installs no schedule.
The optional [Tidepool guide](docs/tidepool.md) distinguishes file import from the
legacy API login; don't present that client as implementing current Tidepool OAuth.
