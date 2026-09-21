# MyChart Sync

Download your MyChart records as Markdown and JSON files, with optional Tidepool imports. Let an AI agent analyze them, or use them however you like.

## Start here

Give a coding agent that can work on your computer this repository and say:

> Follow [SETUP.md](SETUP.md) to set up MyChart Sync and download my health records. Ask which hospitals and sources I use, and walk me through any login or approval you need from me.

The agent handles installation, configuration, and your first sync, then saves instructions for next time. Set it up once; after that, ask your agent to "sync my MyChart records" to update the same local files. You can also follow the setup guide yourself.

## The tedious part

For MyChart, you need to create an [Epic developer account](https://fhir.epic.com/), register an app, and enable each hospital. It's laborious, especially choosing among the many APIs. An agent with computer-use capabilities can help work through the [registration walkthrough](docs/epic_registration.md).

After registering your app with Epic, each hospital you use needs its own activation first. Registration is a one-time setup. After that, downloads are programmatic, though a hospital may occasionally require you to sign in again.

## Your files

By default, records go into `output/`: readable clinical summaries and labs, raw JSON, and downloaded documents. Point your agent at that folder to analyze them. Cloud agents may send the files to their vendor; choose what you share.

## Optional: Tidepool

[Tidepool](https://www.tidepool.org/how-it-works) collects diabetes-device data, such as glucose readings and insulin delivery. Its importer adds that data to the same local folder; it is separate from MyChart. Skip it unless you use Tidepool. [Tidepool setup](docs/tidepool.md) covers API sync and a JSON-export fallback. It is less tested.

This is a small reference tool, not a guaranteed complete medical record. [MIT licensed](LICENSE). [Development notes](CLAUDE.md).
