from __future__ import annotations

"""Sync engine: orchestrates fetch → parse → write pipeline."""

import logging
from typing import Any

from health_sync.auth.token_store import TokenStore
from health_sync.config import AppConfig
from health_sync.fhir.client import FHIRClient, TokenExpiredError
from health_sync.providers.registry import Provider
from health_sync.sync.guards import PersonGuardError, check_person_guard, provider_patient
from health_sync.sync.state import SyncState, hash_resources
from health_sync.writers.clinical_extract import update_clinical_extract
from health_sync.writers.documents import download_documents
from health_sync.writers.lab_results import update_lab_results

logger = logging.getLogger(__name__)


class SyncTokenError(RuntimeError):
    """Raised when a provider cannot sync because auth is missing or expired."""


# Backward-compat: re-export for existing imports
_provider_patient = provider_patient


def sync_provider(
    provider: Provider,
    person: str,
    config: AppConfig,
    dry_run: bool = False,
    full: bool = False,
    override_patient: bool = False,
) -> dict[str, int]:
    """Sync all data from a single provider into output files.

    Dispatches on provider.kind:
    - "fhir" → FHIR-shaped sync (this function below)
    - "tidepool" → delegated to sync/loop_engine.sync_tidepool_api

    Args:
        provider: The provider to sync.
        person: Person whose scoped tokens, state, cache, and output paths are used.
        config: App configuration with output paths.
        dry_run: If True, fetch and parse but don't write to output files.
        full: If True, ignore last sync timestamp and fetch everything.
        override_patient: If True, allow syncing a provider for a non-default person.

    Returns:
        Dict of resource type → count of resources fetched.
    """
    # Kind dispatch (Phase 2). Defaults to fhir for backward compat with rows
    # in providers.json (and test fixtures) that omit `kind`.
    if getattr(provider, "kind", "fhir") == "tidepool":
        from health_sync.sync.loop_engine import sync_tidepool_api

        return sync_tidepool_api(
            provider,
            person,
            config,
            dry_run=dry_run,
            full=full,
            override_patient=override_patient,
        )

    check_person_guard(provider, person, override_patient)

    # Validate person-scoped output paths before constructing stores that create dirs.
    config.output_path("clinical_extract", person)
    config.output_path("lab_results", person)
    config.output_path("records_dir", person)

    token_store = TokenStore(config.tokens_dir(person))
    sync_state = SyncState(config.sync_state_dir(person))

    # Get valid token
    try:
        token = token_store.get_valid_token(
            provider.slug,
            config.get_client_id(provider.slug),
            config.get_client_secret(provider.slug),
        )
    except RuntimeError as e:
        logger.error(f"{provider.name}: {e}")
        raise SyncTokenError(str(e)) from e

    # Determine incremental fetch window
    since = None
    if not full:
        since = sync_state.get_last_sync(provider.slug)
        if since:
            logger.info(f"Incremental sync since {since}")

    # Fetch all resources
    cache_dir = config.fhir_cache_dir(person, provider.slug)
    cache_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Fetching data from {provider.name}...")

    try:
        client = FHIRClient(
            base_url=token.fhir_base_url,
            access_token=token.access_token,
            patient_id=token.patient_id,
            cache_dir=cache_dir,
        )
        fhir_data = client.fetch_all(since=since)
    except TokenExpiredError:
        logger.error(
            f"Token expired for {provider.name}. "
            f"Run: chartstash auth {provider.slug}"
        )
        raise SyncTokenError(f"Token expired for {provider.slug}")

    # Count resources (excluding OperationOutcome)
    counts = {}
    resource_hashes = {}
    for key, resources in fhir_data.items():
        real = [r for r in resources if r.get("resourceType") != "OperationOutcome"]
        counts[key] = len(real)
        if real:
            resource_hashes[key] = hash_resources(real)

    total = sum(counts.values())
    logger.info(f"Fetched {total} resources from {provider.name}")
    for k, v in counts.items():
        if v > 0:
            logger.info(f"  {k}: {v}")

    # Check for changes
    if not full and resource_hashes:
        has_changes, changed = sync_state.has_changes(provider.slug, resource_hashes)
        if not has_changes:
            logger.info("No changes detected — skipping write")
            sync_state.record_sync(provider.slug, counts, resource_hashes)
            client.close()
            return counts
        if changed:
            logger.info(f"Changes detected in: {', '.join(changed)}")

    if dry_run:
        logger.info("Dry run — not writing to output files")
        _print_preview(fhir_data, config, provider.name, person)
        client.close()
        return counts

    # Write to output files
    logger.info("Writing to output files...")

    # clinical_extract.md
    clinical_path = config.output_path("clinical_extract", person)
    clinical_path.parent.mkdir(parents=True, exist_ok=True)
    update_clinical_extract(clinical_path, fhir_data, provider.name)
    logger.info(f"  Updated {clinical_path}")

    # lab_results.md
    lab_path = config.output_path("lab_results", person)
    lab_path.parent.mkdir(parents=True, exist_ok=True)
    update_lab_results(lab_path, fhir_data, provider.name)
    logger.info(f"  Updated {lab_path}")

    # Download clinical documents
    records_dir = config.output_path("records_dir", person)
    try:
        doc_count = download_documents(fhir_data, client, records_dir)
        if doc_count:
            logger.info(f"  Downloaded {doc_count} clinical documents to {records_dir}")
    except Exception as e:
        logger.warning(f"  Document download failed: {e}")

    client.close()

    # Record sync state
    sync_state.record_sync(provider.slug, counts, resource_hashes)
    logger.info("Sync state saved")

    return counts


def sync_all(
    config: AppConfig,
    providers: list[Provider],
    dry_run: bool = False,
    full: bool = False,
    person: str | None = None,
    override_patient: bool = False,
) -> dict[str, dict[str, int]]:
    """Sync all enabled providers.

    Args:
        config: App configuration.
        providers: List of providers to sync.
        dry_run: If True, fetch and parse but don't write.
        full: If True, ignore incremental state.
        person: Optional person override. Defaults to each provider's patient.
        override_patient: If True, allow syncing a provider for a non-default person.

    Returns:
        Dict of provider slug → resource counts.
    """
    results = {}
    for provider in providers:
        target_person = person or _provider_patient(provider)
        logger.info(f"\n{'='*50}")
        logger.info(f"Syncing {provider.name} for {target_person}...")
        logger.info(f"{'='*50}")
        results[provider.slug] = sync_provider(
            provider,
            target_person,
            config,
            dry_run=dry_run,
            full=full,
            override_patient=override_patient,
        )

    return results


def _print_preview(
    fhir_data: dict[str, list[dict[str, Any]]],
    config: AppConfig,
    provider_name: str,
    person: str,
) -> None:
    """Print a preview of what would be written (for dry-run mode)."""
    # Write to a temp path to see the output
    tmp = config.cache_dir(person) / "dry_run_preview.md"
    result = update_clinical_extract(tmp, fhir_data, provider_name)

    print("\n--- DRY RUN PREVIEW: clinical_extract.md ---")
    print(result[:3000])
    if len(result) > 3000:
        print(f"\n... ({len(result)} chars total, truncated)")

    # Clean up
    if tmp.exists():
        tmp.unlink()
