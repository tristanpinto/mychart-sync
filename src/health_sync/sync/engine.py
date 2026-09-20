from __future__ import annotations

"""Sync engine: orchestrates fetch → parse → write pipeline."""

import logging
from datetime import datetime, timezone
from pathlib import Path

from health_sync.auth.token_store import TokenStore
from health_sync.config import AppConfig
from health_sync.fhir.client import FHIRClient, TokenExpiredError
from health_sync.providers.registry import Provider
from health_sync.sync.guards import PersonGuardError, check_person_guard, provider_patient
from health_sync.sync.state import SyncState
from health_sync.sync.records import load_sources, records_lock, merge_source, save_source
from health_sync.writers.clinical_extract import render_clinical_extract
from health_sync.writers.documents import download_documents
from health_sync.writers.lab_results import render_lab_results
from health_sync.writers.generated import write_generated

logger = logging.getLogger(__name__)


class SyncTokenError(RuntimeError):
    """Raised when a provider cannot sync because auth is missing or expired."""


def sync_provider(
    provider: Provider,
    person: str,
    config: AppConfig,
    dry_run: bool = False,
    full: bool = False,
    override_patient: bool = False,
    start_override: datetime | None = None,
) -> dict[str, int]:
    """Sync one provider and return fetched counts.

    A dry run saves FHIR JSON and previews Markdown without advancing state.
    start_override applies only to Tidepool.
    """
    if getattr(provider, "kind", "fhir") == "tidepool":
        from health_sync.sync.loop_engine import sync_tidepool_api

        return sync_tidepool_api(
            provider,
            person,
            config,
            dry_run=dry_run,
            full=full,
            override_patient=override_patient,
            start_override=start_override,
        )

    check_person_guard(provider, person, override_patient)

    # Validate person-scoped output paths before constructing stores that create dirs.
    summary_paths = {config.output_path(key, person) for key in ("clinical_extract", "lab_results")}
    if len(summary_paths) != 2 or config.output_path("health_profile", person) in summary_paths:
        raise ValueError("Clinical extract, labs, and manual health profile must use different files.")
    config.output_path("records_dir", person)

    cache_dir = config.fhir_cache_dir(person, provider.slug)
    with records_lock(cache_dir.parent):
        return _sync_fhir(provider, person, config, cache_dir, dry_run, full)


def _sync_fhir(
    provider: Provider, person: str, config: AppConfig,
    cache_dir: Path, dry_run: bool, full: bool,
) -> dict[str, int]:
    """Run under the person's lock, including any refresh-token rotation."""
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

    logger.info(f"Fetching data from {provider.name}...")
    try:
        with FHIRClient(
            base_url=token.fhir_base_url,
            access_token=token.access_token,
            patient_id=token.patient_id,
            cache_dir=None,  # The complete, validated fetch is retained atomically below.
        ) as client:
            sources = load_sources(cache_dir.parent, person)
            previous = sources.get(provider.slug, {})
            since = None
            if not full and previous.get("full_fetch_complete"):
                since = sync_state.get_last_sync(provider.slug)
            if since:
                logger.info(f"Incremental sync since {since}")
            # Persist the start, not the finish, so updates during the fetch aren't missed.
            started_at = datetime.now(timezone.utc).isoformat()
            fhir_data = client.fetch_all(since=since)
            sources[provider.slug] = merge_source(
                person, provider.name, token.patient_id,
                token.fhir_base_url, previous, fhir_data,
            )
            # Parse all retained sources before changing JSON or either summary.
            clinical = render_clinical_extract(sources)
            labs = render_lab_results(sources)
            for slug, source in sources.items():
                if slug != provider.slug and not (cache_dir.parent / slug / "records.json").exists():
                    save_source(cache_dir.parent, slug, source)
            save_source(cache_dir.parent, provider.slug, sources[provider.slug])
            counts = {
                key: sum(r.get("resourceType") != "OperationOutcome" for r in resources)
                for key, resources in fhir_data.items()
            }

            logger.info(f"Fetched {sum(counts.values())} resources from {provider.name}")

            if dry_run:
                _print_preview(clinical)
                return counts

            # Retry missing documents from retained metadata even after an empty fetch.
            records_dir = config.output_path("records_dir", person)
            doc_count = download_documents(sources[provider.slug]["resources"], client, records_dir)
            logger.info(f"Downloaded {doc_count} clinical documents")

            write_generated(config.output_path("clinical_extract", person), clinical)
            write_generated(config.output_path("lab_results", person), labs)
            if since is None:
                sources[provider.slug]["full_fetch_complete"] = True
                save_source(cache_dir.parent, provider.slug, sources[provider.slug])
            sync_state.record_sync(
                provider.slug, counts, synced_at=started_at
            )
            return counts
    except TokenExpiredError:
        logger.error(
            f"Token expired for {provider.name}. "
            f"Run: mychart-sync auth {provider.slug}"
        )
        raise SyncTokenError(f"Token expired for {provider.slug}")

def sync_all(
    config: AppConfig,
    providers: list[Provider],
    dry_run: bool = False,
    full: bool = False,
    person: str | None = None,
    override_patient: bool = False,
    start_override: datetime | None = None,
) -> dict[str, dict[str, int]]:
    """Sync providers in order, using each assigned person unless overridden."""
    results = {}
    for provider in providers:
        target_person = person or provider_patient(provider)
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
            start_override=start_override,
        )

    return results


def _print_preview(result: str) -> None:
    """Print a preview of what would be written (for dry-run mode)."""
    print("\n--- DRY RUN PREVIEW: clinical_extract.md ---")
    print(result[:3000])
    if len(result) > 3000:
        print(f"\n... ({len(result)} chars total, truncated)")
