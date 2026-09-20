from __future__ import annotations

"""Import Tidepool JSON exports or API data into daily JSONL and Markdown."""

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from health_sync.auth.tidepool_credentials import (
    TidepoolCredentialsError,
    load_credentials,
)
from health_sync.config import AppConfig
from health_sync.parsers.loop import NormalizedLoopRecord, parse_records
from health_sync.parsers.loop_rollup import compute_daily
from health_sync.providers.registry import Provider
from health_sync.sources.tidepool_client import (
    TidepoolAuthError,
    TidepoolClient,
    TidepoolFetchError,
)
from health_sync.sources.tidepool_export import TidepoolExportSource
from health_sync.sync.guards import check_person_guard
from health_sync.sync.state import SyncState
from health_sync.writers.loop_daily import write_daily_summary
from health_sync.writers.loop_raw import deduplicate_records, write_raw_jsonl
from health_sync.writers.loop_telemetry import write_loop_telemetry

logger = logging.getLogger(__name__)

# Legacy bulk-import boundary; --start selects a different beginning.
DEFAULT_BULK_IMPORT_START = datetime(2022, 9, 1, tzinfo=timezone.utc)

# Rolling lookback window for incremental sync. Catches pump uploads of
# 2-4 week old buffered data. Tidepool startDate filters record-time,
# not upload-time, so we re-fetch a 30-day window and merge by record ID.
INCREMENTAL_LOOKBACK_DAYS = 30


class LoopSyncError(RuntimeError):
    """Raised when a Loop sync cannot complete."""


def sync_from_export(
    export_file: Path,
    person: str,
    config: AppConfig,
    dry_run: bool = False,
) -> dict[str, int]:
    """Import JSON records, returning raw/parsed record and daily-file counts."""
    if not export_file.exists():
        raise LoopSyncError(f"Export file not found: {export_file}")

    logger.info(f"Reading export: {export_file}")
    source = TidepoolExportSource(export_file)
    try:
        raw_records = list(source.fetch())
    except (OSError, ValueError) as e:
        raise LoopSyncError("Could not read Tidepool export; expected a JSON array of records") from e
    finally:
        source.close()
    return _merge_and_write(raw_records, person, config, dry_run=dry_run)


def _merge_and_write(
    raw_records: list[dict], person: str, config: AppConfig, *, dry_run: bool
) -> dict[str, int]:
    """Merge fetched records into retained history before rebuilding summaries.

    New records replace matching IDs; records absent from a fetch are retained.
    Saved JSONL already uses the parser's units, including basal minutes.
    """
    raw_dir = config.output_path("loop_raw_dir", person)
    daily_dir = config.output_path("loop_daily_dir", person)
    person_tz = config.person(person).timezone

    normalized = deduplicate_records(
        list(parse_records(raw_records, fallback_tz=person_tz))
    )
    incoming_count = len(normalized)
    existing: list[NormalizedLoopRecord] = []
    try:
        for path in sorted(raw_dir.glob("????-??-??.jsonl")):
            records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            if not all(isinstance(record, dict) for record in records):
                raise ValueError("Expected record objects")
            parsed = list(parse_records(records, fallback_tz=person_tz))
            if len(parsed) != len(records):
                raise ValueError("Stored records could not be parsed")
            existing.extend(parsed)
    except (OSError, ValueError) as e:
        raise LoopSyncError("Could not read stored Tidepool history; no files were changed") from e

    old_days = {record.local_date for record in existing}
    # Prevent repeated imports from accumulating identical records without IDs.
    incoming_without_ids = {
        json.dumps(record.raw, sort_keys=True)
        for record in normalized if record.raw.get("id") is None
    }
    existing = [record for record in existing if record.raw.get("id") is not None
                or json.dumps(record.raw, sort_keys=True) not in incoming_without_ids]
    normalized = deduplicate_records(normalized + existing)

    by_date: dict[date, list[NormalizedLoopRecord]] = defaultdict(list)
    for r in normalized:
        by_date[r.local_date].append(r)

    days = sorted(by_date.keys())
    counts = {
        "raw_records": len(raw_records),
        "parsed_records": incoming_count,
        "daily_files_written": 0,
        "days": len(days),
    }

    if dry_run:
        logger.info("Dry run — not writing to output")
        return counts

    all_summaries = []
    basals = sorted((record for record in normalized if record.kind == "basal"),
                    key=lambda record: record.time_utc)
    basal_successors: dict[date, list[NormalizedLoopRecord]] = defaultdict(list)
    for basal, successor in zip(basals, basals[1:]):
        if basal.local_date != successor.local_date:
            basal_successors[basal.local_date].append(successor)
    # Include emptied dates when an updated record moves to a different day.
    for d in sorted(set(days) | old_days):
        day_records = by_date[d]
        write_raw_jsonl(day_records, raw_dir, d)
        # Keep cross-day basal forward-fill without scanning all history per day.
        summary = compute_daily(day_records + basal_successors[d], d)
        write_daily_summary(summary, daily_dir, tz=person_tz)
        if day_records:
            all_summaries.append(summary)
        counts["daily_files_written"] += 1

    logger.info(
        f"Wrote {counts['daily_files_written']} daily summaries to {daily_dir}"
    )
    logger.info(f"Raw JSONL files: {raw_dir}")

    # Rolling telemetry — last 7, last 30, all-time (if span > 30 days)
    _write_telemetry(all_summaries, config, person, person_tz)

    return counts


def _write_telemetry(
    summaries: list, config: AppConfig, person: str, person_tz: str
) -> None:
    """Compute and write loop_telemetry.md if the path is configured."""
    try:
        telemetry_path = config.output_path("loop_telemetry", person)
    except ValueError:
        # loop_telemetry path not configured for this person — skip silently
        return
    anchor = max((s.local_date for s in summaries), default=datetime.now(timezone.utc).date())
    write_loop_telemetry(summaries, telemetry_path, today=anchor, person_tz=person_tz)
    logger.info(f"Wrote loop_telemetry.md to {telemetry_path}")


# --- Tidepool API sync ---


def _month_chunks(
    start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    """Limit requests to 31 days to keep response sizes and timeouts manageable."""
    chunks: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        next_cursor = min(cursor + timedelta(days=31), end)
        chunks.append((cursor, next_cursor))
        cursor = next_cursor
    return chunks


def _determine_fetch_window(
    *,
    full: bool,
    start_override: Optional[datetime],
    sync_state: SyncState,
    slug: str,
) -> tuple[datetime, datetime]:
    """Use an explicit start, bulk default, or rolling lookback; end at now (UTC)."""
    end = datetime.now(timezone.utc)

    if start_override is not None:
        return start_override.astimezone(timezone.utc), end

    if full:
        return DEFAULT_BULK_IMPORT_START, end

    last_sync_str = sync_state.get_last_sync(slug)
    if last_sync_str is None:
        # First sync — treat as full
        return DEFAULT_BULK_IMPORT_START, end

    try:
        last_sync = datetime.fromisoformat(last_sync_str.replace("Z", "+00:00"))
    except ValueError:
        logger.warning(
            f"Could not parse last_sync='{last_sync_str}', treating as full"
        )
        return DEFAULT_BULK_IMPORT_START, end

    start = max(
        last_sync - timedelta(days=INCREMENTAL_LOOKBACK_DAYS),
        DEFAULT_BULK_IMPORT_START,
    )
    return start.astimezone(timezone.utc), end


def sync_tidepool_api(
    provider: Provider,
    person: str,
    config: AppConfig,
    *,
    dry_run: bool = False,
    full: bool = False,
    override_patient: bool = False,
    start_override: Optional[datetime] = None,
) -> dict[str, int]:
    """Fetch monthly API windows and merge through the same pipeline as exports."""
    check_person_guard(provider, person, override_patient)

    # Validate the destination before making network requests.
    config.output_path("loop_raw_dir", person)
    config.output_path("loop_daily_dir", person)
    api_base = provider.tidepool_api_base or "https://api.tidepool.org"

    # Load credentials (will raise with a clear "Run mychart-sync auth" message)
    try:
        credentials = load_credentials(person, config.secrets_dir())
    except TidepoolCredentialsError as e:
        raise LoopSyncError(str(e)) from e

    sync_state = SyncState(config.sync_state_dir(person))
    start, end = _determine_fetch_window(
        full=full, start_override=start_override, sync_state=sync_state,
        slug=provider.slug,
    )
    chunks = _month_chunks(start, end)
    logger.info(
        f"Tidepool fetch: {start.date()} → {end.date()} "
        f"({len(chunks)} monthly chunks)"
    )

    raw_records: list[dict] = []
    client = TidepoolClient(credentials, api_base=api_base)
    try:
        for chunk_start, chunk_end in chunks:
            raw_records.extend(client.fetch(chunk_start, chunk_end))
    except (TidepoolAuthError, TidepoolFetchError) as e:
        raise LoopSyncError(str(e)) from e
    finally:
        client.close()

    logger.info(f"Fetched {len(raw_records)} total records across {len(chunks)} chunks")

    counts = _merge_and_write(raw_records, person, config, dry_run=dry_run)
    counts["chunks"] = len(chunks)

    if dry_run:
        logger.info("Dry run — not writing to output or state")
        return counts

    # Record the right edge of the window we just fetched; next incremental
    # sync uses (right_edge - INCREMENTAL_LOOKBACK_DAYS) as its left edge.
    sync_state.record_sync(provider.slug, {"records": counts["raw_records"]})

    return counts
