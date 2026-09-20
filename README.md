# Chartstash

Your Epic MyChart records as local Markdown and JSON, readable by any file-capable agent.
A small reference for people comfortable with a terminal and registering their own Epic app.
Uses the patient-authorized FHIR API, not website scraping. No hosted service or MCP server.

## Start here

Requires Python 3.10+, [uv](https://docs.astral.sh/uv/getting-started/installation/), OpenSSL,
and a MyChart account at each hospital. Run from this checkout:

```sh
umask 077
uv sync --extra dev
cp config/app.example.json config/app.json
cp config/providers.example.json config/providers.json
```

Edit `config/app.json`: set your client IDs, timezone, and `output_dir`.
Relative output paths resolve from the checkout. Config, credentials, cache, and `output/`
are gitignored; add any custom output directory to `.gitignore` yourself.

## Register with Epic

1. Create an account at [Epic on FHIR](https://fhir.epic.com/) and register a **patient-facing**
   app using standalone SMART OAuth. Select read-only access, **Enable Auto-download**,
   **USCDI v3**, and refresh tokens (confidential client). Stay within the USCDI API set.
2. Register exactly `https://localhost:8080/callback`. Save both client IDs in `config/app.json`.
   Review the details, accept Epic's terms, and mark **Ready for Production**.
3. Under **Build Apps > Review & Manage Downloads**, provision a distinct client secret
   for each hospital and environment. Activate non-production before production if prompted.
   Save the secrets before enabling. Requests can take an hour to appear; distribution may
   take longer. "Ready" alone does not finish a refresh-token setup.

The per-hospital credential step is easy to miss. Without it, refresh tokens are unavailable.
Auto-download also depends on the hospital's participation. See Epic's
[distribution and credential instructions](https://fhir.epic.com/Documentation?docId=epicidtypes).

## Connect and sync

```sh
uv run chartstash providers search "Your hospital"
uv run chartstash providers add
# Choose a slug (for example, hospital), its FHIR R4 URL, and patient key: me.
mkdir -p secrets
cp config/provider_secrets.example.json secrets/hospital_secrets.json
chmod 600 secrets/hospital_secrets.json
# Edit that file with this hospital's client IDs and secrets.
uv run chartstash auth hospital
uv run chartstash sync --provider hospital --dry-run
uv run chartstash sync --provider hospital
```

Secret filenames replace slug hyphens with underscores: `my-hospital` becomes
`secrets/my_hospital_secrets.json`. First auth opens the hospital's browser login and consent.
The callback uses a generated self-signed certificate; a warning is expected only on your
own `https://localhost:8080/callback`. Never bypass a hospital certificate warning.
Confirm auth reports `Refresh token: yes`. Repeat per hospital, then `uv run chartstash sync`
syncs enabled providers. Expired/revoked access needs browser auth again. `--full` ignores
incremental state. A dry run still contacts the hospital and saves local raw data.

**Audience-alias failure:** a valid FHIR URL can be an alias that the OAuth portal rejects
as `aud`, sometimes with a misleading `launch/patient` error. Chartstash reads
`/metadata`'s `CapabilityStatement.implementation.url` for the OAuth audience while keeping
the configured URL for data requests. Don't remove `launch/patient` to work around it.

## Read the files

```text
output/
  clinical_extract.md     conditions, medications, visits, procedures, and more
  lab_results.md         longitudinal labs with source attribution
  raw/<hospital>/*.json   most recently fetched FHIR resources
  documents/             original clinical documents (PDF, HTML, or text)
```

Raw JSON is a fetch cache, not a complete versioned archive; incremental runs can replace
it with only recent resources. Markdown merges across runs. `health_profile.md` is reserved
for your own notes and is never overwritten. Keep backups; this is not a complete medical record.
Old medication orders can remain marked active; verify current use against the source.

Point an agent at the absolute output path: "Read my clinical extract and labs in
`/path/to/chartstash/output`. Cite source files and dates; do not modify them."
Giving a cloud agent access may upload those records to its vendor. Keep `secrets/` out of
agent access and never attach tokens, raw records, or debug logs to public issues.

## Tidepool (less tested)

The simpler route is a JSON export:
```sh
uv run chartstash tidepool import-export --person me --file /path/to/export.json
```
It writes `loop/raw/*.jsonl`, daily Markdown, and `loop/loop_telemetry.md`.
For API sync, add this entry to the `providers` array in `config/providers.json`:

```json
{"slug":"tidepool","name":"Tidepool","kind":"tidepool","patient":"me","enabled":false}
```

Run `uv run chartstash auth tidepool`, then `uv run chartstash sync --provider tidepool`.
This legacy login stores your Tidepool password locally with mode `0600`; it may stop working.
`--start YYYY-MM-DD` selects a start date; `--full` defaults to 2022-09-01.
Lessons retained in tests: deduplicate readings, honor per-record timezone offsets, and
convert API basal durations from milliseconds to the export format's minutes. Spot-check
summaries against the source; do not use them to automate treatment.

## Development

`uv run pytest -q` runs offline tests. To scope a batch and report partial failures:
```sh
bash scripts/sync_person.sh me hospital-a hospital-b -- --dry-run
```
Existing `health-sync` commands and `brain_dir` configurations remain compatible.
The optional `scripts/health_sync.sh` change-signal helper requires `jq`; set its
`PERSON` and `OUTPUT_DIR` to match your config. It installs no schedule.
[MIT licensed](LICENSE).
