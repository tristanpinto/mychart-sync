# Chartstash

Local patient-authorized Epic FHIR sync, with optional Tidepool imports. Start with
[README.md](README.md) for setup and output. Runtime Python package: `health_sync`.

- `uv sync --extra dev`; `uv run pytest -q`. Tests use temporary directories and mocked HTTP.
- Keep parsers and Markdown/JSON schemas stable. Preserve person-scoped routing and tokens.
- Never commit local config, secrets, records, cache, or real clinical test fixtures.
- No live sync, auth, hosting, or publishing as part of tests.
- Legacy `health-sync` CLI and `brain_dir` config aliases remain supported.
