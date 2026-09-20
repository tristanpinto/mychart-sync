from __future__ import annotations

"""Loop telemetry sync engine.

Phase 1 entry point: `sync_from_export(export_file, person, config, ...)`.
Reads a Tidepool web-UI JSON export, parses records, groups by local date,
and writes per-day JSONL + daily MD into the person's output directory.

Phase 2 will add `sync_tidepool_api(provider, person, config, ...)` against
the legacy session-token API behind the same parser/rollup/writer pipeline.
"""

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
from health_sync.writers.loop_raw import write_raw_jsonl
from health_sync.writers.loop_telemetry import write_loop_telemetry

logger = logging.getLogger(__name__)

# Legacy bulk-import boundary; --start selects a different beginning.
DEFAULT_BULK_IMPORT_START = datetime(2022, 9, 1, tzinfo=timezone.utc)

# Rolling lookback window for incremental sync. Catches pump uploads of
# 2-4 week old buffered data (per Codex review finding #4 — Tidepool
# startDate filters record-time, not upload-time, so we re-fetch a
# 30-day rolling window each sync and dedup on write).
INCREMENTAL_LOOKBACK_DAYS = 30


class LoopSyncError(RuntimeError):
    """Raised when a Loop sync cannot complete."""


def sync_from_export(
    export_file: Path,
    person: str,
    config: AppConfig,
    dry_run: bool = False,
) -> dict[str, int]:
    """Ingest a Tidepool web-UI JSON export into the person's output directory.

    Args:
        export_file: Path to the JSON export.
        person: Configured person key (must have loop_raw_dir, loop_daily_dir).
        config: App configuration.
        dry_run: If True, parse and roll up but don't write to output.

    Returns:
        Dict with counts: {raw_records, parsed_records, daily_files_written, days}.
    """
    if not export_file.exists():
        raise LoopSyncError(f"Export file not found: {export_file}")

    # Validate output paths up-front (consistent with FHIR engine pattern).
    raw_dir = config.output_path("loop_raw_dir", person)
    daily_dir = config.output_path("loop_daily_dir", person)

    person_tz = config.person(person).timezone

    logger.info(f"Reading export: {export_file}")
    source = TidepoolExportSource(export_file)
    raw_records = list(source.fetch())
    logger.info(f"Loaded {len(raw_records)} raw records")

    normalized: list[NormalizedLoopRecord] = list(
        parse_records(raw_records, fallback_tz=person_tz)
    )
    logger.info(f"Parsed {len(normalized)} records into normalized form")

    # Group by local date
    by_date: dict = defaultdict(list)
    for r in normalized:
        by_date[r.local_date].append(r)

    days = sorted(by_date.keys())
    logger.info(
        f"Coverage: {len(days)} days from {days[0]} to {days[-1]}"
        if days
        else "No days covered"
    )

    counts = {
        "raw_records": len(raw_records),
        "parsed_records": len(normalized),
        "daily_files_written": 0,
        "days": len(days),
    }

    if dry_run:
        logger.info("Dry run — not writing to output")
        # Still compute one summary so we can preview
        if days:
            sample = compute_daily(normalized, days[0])
            logger.info(f"Sample day {days[0]}: TIR {sample.tir_70_180_pct}% "
                        f"GMI {sample.gmi_pct}% TDD {sample.tdd_units}U")
        return counts

    all_summaries = []
    for d in days:
        day_records = by_date[d]
        write_raw_jsonl(day_records, raw_dir, d)
        summary = compute_daily(normalized, d)
        write_daily_summary(summary, daily_dir, tz=person_tz)
        all_summaries.append(summary)
        counts["daily_files_written"] += 1

    logger.info(
        f"Wrote {counts['daily_files_written']} daily summaries to {daily_dir}"
    )
    logger.info(f"Raw JSONL files: {raw_dir}")

    # Rolling telemetry — last 7, last 30, all-time (if span > 30 days)
    _write_telemetry(all_summaries, config, person, person_tz)

    source.close()
    return counts


def _write_telemetry(
    summaries: list, config: AppConfig, person: str, person_tz: str
) -> None:
    """Compute and write loop_telemetry.md if the path is configured."""
    if not summaries:
        return
    try:
        telemetry_path = config.output_path("loop_telemetry", person)
    except ValueError:
        # loop_telemetry path not configured for this person — skip silently
        return
    anchor = max(s.local_date for s in summaries)
    write_loop_telemetry(summaries, telemetry_path, today=anchor, person_tz=person_tz)
    logger.info(f"Wrote loop_telemetry.md to {telemetry_path}")


# --- Phase 2: Tidepool legacy-API sync ---


def _month_chunks(
    start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    """Break a [start, end) window into ≤31-day chunks.

    Tidepool's /data/{userId} endpoint accepts arbitrary windows, but very
    large windows can time out at the 60-second client timeout. Chunking
    by month keeps per-request response sizes bounded.
    """
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
    """Compute the fetch window for a Tidepool sync.

    Order of precedence:
    1. start_override → uses provided start, end = now
    2. full → uses DEFAULT_BULK_IMPORT_START, end = now
    3. otherwise (incremental) → max(stored_last_sync - rolling_lookback,
       DEFAULT_BULK_IMPORT_START); if no stored state, treat as full

    Returns (start, end) — both tz-aware UTC.
    """
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
    """Sync diabetes data from Tidepool's legacy API into the person's output directory.

    Same downstream pipeline as the export source (parser → rollup → writers),
    so daily MDs and raw JSONL have identical schema regardless of source.

    Args:
        provider: Tidepool provider (must have kind="tidepool").
        person: Target person.
        config: App config.
        dry_run: Parse + roll up but skip writes and state recording.
        full: Ignore last_sync, fetch from DEFAULT_BULK_IMPORT_START.
        override_patient: Skip the person-guard check.
        start_override: Explicit window start (overrides full and incremental).

    Returns:
        Counts: raw_records, parsed_records, days, daily_files_written, chunks.
    """
    check_person_guard(provider, person, override_patient)

    # Validate output paths up-front
    raw_dir = config.output_path("loop_raw_dir", person)
    daily_dir = config.output_path("loop_daily_dir", person)

    person_tz = config.person(person).timezone
    api_base = provider.tidepool_api_base or "https://api.tidepool.org"

    # Load credentials (will raise with a clear "Run chartstash auth" message)
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

    counts = {
        "raw_records": 0,
        "parsed_records": 0,
        "days": 0,
        "daily_files_written": 0,
        "chunks": len(chunks),
    }

    raw_records: list[dict] = []
    client = TidepoolClient(credentials, api_base=api_base)
    try:
        for chunk_start, chunk_end in chunks:
            chunk_records = list(client.fetch(chunk_start, chunk_end))
            raw_records.extend(chunk_records)
            counts["raw_records"] += len(chunk_records)
    except (TidepoolAuthError, TidepoolFetchError) as e:
        client.close()
        raise LoopSyncError(str(e)) from e
    finally:
        client.close()

    logger.info(f"Fetched {len(raw_records)} total records across {len(chunks)} chunks")

    normalized: list[NormalizedLoopRecord] = list(
        parse_records(raw_records, fallback_tz=person_tz)
    )
    counts["parsed_records"] = len(normalized)

    by_date: dict[date, list[NormalizedLoopRecord]] = defaultdict(list)
    for r in normalized:
        by_date[r.local_date].append(r)
    days = sorted(by_date.keys())
    counts["days"] = len(days)

    if dry_run:
        logger.info("Dry run — not writing to output or state")
        return counts

    all_summaries = []
    for d in days:
        write_raw_jsonl(by_date[d], raw_dir, d)
        summary = compute_daily(normalized, d)
        write_daily_summary(summary, daily_dir, tz=person_tz)
        all_summaries.append(summary)
        counts["daily_files_written"] += 1

    # Rolling telemetry summary
    _write_telemetry(all_summaries, config, person, person_tz)

    # Record the right edge of the window we just fetched; next incremental
    # sync uses (right_edge - INCREMENTAL_LOOKBACK_DAYS) as its left edge.
    sync_state.record_sync(provider.slug, {"records": counts["raw_records"]})

    logger.info(
        f"Wrote {counts['daily_files_written']} daily summaries to {daily_dir}"
    )
    return counts
