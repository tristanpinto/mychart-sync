# Tidepool (optional, less tested)

[Tidepool](https://www.tidepool.org/how-it-works) brings together diabetes-device
data: glucose readings, insulin delivery, and related records. This importer adds
supported records to your local files alongside MyChart. Skip it unless you already
use Tidepool; MyChart sync does not depend on it.

Start with [local installation](../SETUP.md#1-install-locally). Use your own Tidepool
account with device data already uploaded. This tool does not connect to devices
or implement clinician/caregiver access to another person's account.

## API sync

Sign in with your Tidepool email and password, then download records directly to
local files. The client uses Tidepool's legacy login at `/auth/login`, with no
client-ID configuration. Compatibility with newly created accounts has not been verified.

Ask before storing the user's password locally. Use API access only where permitted
by Tidepool. If legacy login fails or needs unsupported authentication, use the export
fallback below; don't disable account security to make it work.

Add this to the `providers` array in `config/providers.json`, only if absent:

```json
{"slug":"tidepool","name":"Tidepool","kind":"tidepool","patient":"me","enabled":false}
```

```sh
.venv/bin/mychart-sync auth tidepool
.venv/bin/mychart-sync sync --provider tidepool --start YYYY-MM-DD --dry-run
.venv/bin/mychart-sync sync --provider tidepool --start YYYY-MM-DD
```

Replace `YYYY-MM-DD` with the user's chosen first date. Otherwise the first sync
and `--full` start at 2022-09-01, which may miss earlier history. Later incremental
syncs re-fetch 30 days; use `--start` again for older uploads. Auth stores the password
in `secrets/tokens/me/tidepool_credentials.json`; session tokens are kept in memory.
Leave `enabled:false` for explicit runs only. Set it to `true` only if the user
wants Tidepool included whenever they run `mychart-sync sync`.

**Legacy-auth caveat:** this client does not implement modern Tidepool OAuth.
The [developer instructions](https://tidepool.redocly.app/) require a client identifier
for that route, and Tidepool currently says [new client IDs are paused](https://support.tidepool.org/hc/en-us/articles/360029368812-Tidepool-API-3rd-Party-Integrations).
Legacy access may stop working. Don't develop against production or put real health
records in Tidepool's integration environment.

## Fallback: import a JSON export

Use this if API login is unavailable or you prefer not to store a password.
No developer registration, provider entry, or API login is needed.

1. Sign in at [app.tidepool.org](https://app.tidepool.org/) and confirm your data is there.
2. Choose **Export Data**, a date range (up to 90 days), **mg/dL**, and **JSON**.
   Download the file; repeat for earlier ranges as needed. See Tidepool's
   [export instructions](https://support.tidepool.org/hc/en-us/articles/360044350552-Exporting-Tidepool-data).
3. From this checkout, import the downloaded JSON array, not an Excel or ZIP file:

```sh
.venv/bin/mychart-sync tidepool import-export --person me --file /path/to/TidepoolExport.json --dry-run
.venv/bin/mychart-sync tidepool import-export --person me --file /path/to/TidepoolExport.json
```

The code merges history, removes duplicates, normalizes units/timezones, and writes
the same summaries as API sync. Only the download step is manual. Keep the export
private too; it may still be in your Downloads folder.

## Output and limits

Both routes write `output/loop/raw/*.jsonl`, `loop/daily/*.md`, and `loop/loop_telemetry.md`.
Supported data: glucose readings, basal/bolus insulin, and carbohydrates. Other record
types are not retained, so keep the original export for a complete source copy.
JSONL means one JSON record per line. API basal durations are normalized to minutes.

Overlapping imports merge by record ID before summaries are computed. Previously
downloaded records remain even if later deleted from Tidepool. Summaries use retained
history and are not treatment guidance. Spot-check them against the source.
