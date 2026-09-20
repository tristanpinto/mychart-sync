from __future__ import annotations

"""MyChart Sync commands. Run mychart-sync --help for usage."""

import json
import logging
import re
import time
from pathlib import Path

import click
from pydantic import ValidationError

from health_sync.auth.smart_auth import authenticate
from health_sync.auth.token_store import StoredToken, TokenStore
from health_sync.config import ConfigError, load_config
from health_sync.fhir.client import RESOURCE_TYPES, FHIRClient
from health_sync.fhir.endpoints import SANDBOX_ENDPOINT, search_endpoints, validate_endpoint
from health_sync.providers.registry import Provider, ProviderRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)


class ConfigErrorGroup(click.Group):
    """Keep malformed local settings and credentials out of tracebacks."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except ConfigError as e:
            raise click.ClickException(str(e)) from e
        except (ValidationError, json.JSONDecodeError) as e:
            raise click.ClickException(
                "Invalid local settings or credentials. Check the file format in SETUP.md."
            ) from e


def _get_config():
    try:
        return load_config()
    except FileNotFoundError as e:
        raise click.ClickException(str(e)) from e


def _get_registry(config=None):
    if config is None:
        config = _get_config()
    from health_sync.config import _project_root

    return ProviderRegistry(_project_root() / "config" / "providers.json")


def _provider_patient(provider: Provider) -> str:
    patient = getattr(provider, "patient", None)
    if not patient:
        raise click.ClickException(
            f"Provider '{provider.slug}' has no assigned patient in config/providers.json"
        )
    return patient


def _resolve_person(
    config,
    provider: Provider,
    person: str | None,
    override_patient: bool,
) -> str:
    provider_patient = _provider_patient(provider)
    target_person = person or provider_patient

    try:
        config.person(target_person)
    except ValueError as e:
        raise click.ClickException(str(e)) from e

    if target_person != provider_patient and not override_patient:
        raise click.ClickException(
            f"Provider '{provider.slug}' is assigned to '{provider_patient}', "
            f"not '{target_person}'. Pass --override-patient to override."
        )

    return target_person


def _project_cache_dir():
    from health_sync.config import _project_root

    cache_dir = _project_root() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


@click.group(cls=ConfigErrorGroup)
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def main(verbose: bool) -> None:
    """mychart-sync: Sync medical records from Epic MyChart via SMART on FHIR."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)


# --- Auth ---


@main.command()
@click.argument("slug")
@click.option("--person", help="Authenticate as this configured person")
@click.option("--timeout", default=600, show_default=True, help="Seconds to wait for browser login")
@click.option(
    "--override-patient",
    is_flag=True,
    help="Allow authenticating a provider for a person other than provider.patient",
)
def auth(slug: str, person: str | None, timeout: int, override_patient: bool) -> None:
    """Authenticate with a health system or data source.

    SLUG is the provider identifier (e.g., 'mount-sinai', 'tidepool').
    For FHIR providers, opens a browser for SMART on FHIR OAuth.
    For Tidepool, prompts for email + password (legacy auth).
    """
    config = _get_config()
    registry = _get_registry(config)

    provider = registry.get(slug)
    if provider is None:
        raise click.ClickException(
            f"Provider '{slug}' not registered. Run: mychart-sync providers add"
        )

    target_person = _resolve_person(config, provider, person, override_patient)

    # Tidepool legacy auth: prompt for email + password, save credentials
    if provider.kind == "tidepool":
        _auth_tidepool(provider, target_person, config)
        return

    # FHIR OAuth flow
    token_store = TokenStore(config.tokens_dir(target_person))

    client_id = config.get_client_id(slug)
    if not client_id:
        raise click.ClickException("client_id not set in config/app.json")

    client_secret = config.get_client_secret(slug)

    click.echo(f"Authenticating with {provider.name} for {target_person}...")
    click.echo(f"FHIR endpoint: {provider.fhir_base_url}")
    click.echo(f"Client ID: {client_id}")
    click.echo(f"Client type: {'confidential' if client_secret else 'public'}")

    result = authenticate(
        fhir_base_url=provider.fhir_base_url,
        client_id=client_id,
        redirect_uri=config.redirect_uri,
        secrets_dir=config.secrets_dir(),
        client_secret=client_secret,
        timeout=timeout,
    )

    token = StoredToken(
        provider_slug=slug,
        person=target_person,
        fhir_base_url=provider.fhir_base_url,
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_at=time.time() + result.expires_in,
        patient_id=result.patient_id,
        token_endpoint=result.token_endpoint,
        scope=result.scope,
    )
    token_store.save(token)

    click.echo(f"\nAuthenticated successfully!")
    click.echo(f"  Patient ID: {result.patient_id}")
    click.echo(f"  Scopes: {result.scope}")
    click.echo(f"  Refresh token: {'yes' if result.refresh_token else 'no'}")


def _auth_tidepool(provider, target_person: str, config) -> None:
    """Prompt for Tidepool email + password, verify, save with chmod 0600."""
    from health_sync.auth.tidepool_credentials import (
        TidepoolCredentials,
        save_credentials,
    )
    from health_sync.sources.tidepool_client import TidepoolAuthError, TidepoolClient

    click.echo(f"Authenticating with {provider.name} for {target_person}...")
    click.echo(f"API base: {provider.tidepool_api_base}")
    click.echo("")
    click.echo(
        "Note: Tidepool uses legacy auth — your password is stored locally at "
        f"secrets/tokens/{target_person}/tidepool_credentials.json (mode 0600)."
    )
    email = click.prompt("Tidepool email", type=str)
    password = click.prompt("Tidepool password", type=str, hide_input=True)

    credentials = TidepoolCredentials(email=email, password=password)

    # Verify roundtrip — catches bad creds before we save them
    click.echo("Verifying credentials with Tidepool...")
    client = TidepoolClient(credentials, api_base=provider.tidepool_api_base)
    try:
        client._login()
        credentials.userid = client._userid
        client.close()
    except TidepoolAuthError as e:
        raise click.ClickException(f"Authentication failed: {e}") from e

    path = save_credentials(target_person, credentials, config.secrets_dir())
    click.echo(f"\nAuthenticated successfully!")
    click.echo(f"  User ID: {credentials.userid}")
    click.echo(f"  Credentials saved: {path}")


# --- Sync ---


@main.command()
@click.option("--provider", "-p", help="Sync only this provider (default: all enabled)")
@click.option("--person", help="Sync as this configured person")
@click.option(
    "--override-patient",
    is_flag=True,
    help="Allow syncing a provider for a person other than provider.patient",
)
@click.option("--dry-run", is_flag=True, help="Preview changes; still downloads raw FHIR data and may refresh tokens")
@click.option("--full", is_flag=True, help="Full re-sync (ignore incremental state)")
@click.option(
    "--bulk-import",
    is_flag=True,
    help="(Tidepool only) Full sync from default bulk-import start. Equivalent to --full.",
)
@click.option(
    "--start",
    "start_str",
    default=None,
    help="(Tidepool only) Explicit window start, e.g. 2024-01-01",
)
def sync(
    provider: str | None,
    person: str | None,
    override_patient: bool,
    dry_run: bool,
    full: bool,
    bulk_import: bool,
    start_str: str | None,
) -> None:
    """Sync data from configured providers into output files.

    For FHIR providers: fetches resources, parses, writes to clinical_extract.md
    and lab_results.md.
    For Tidepool: fetches Loop data via legacy API, writes per-day JSONL +
    daily summary MD into loop/raw/ and loop/daily/.

    Uses incremental sync by default. `--full` and `--bulk-import` are
    equivalent for Tidepool (both fetch from 2022-09-01).
    """
    from datetime import datetime, timezone

    from health_sync.sync.engine import SyncTokenError, sync_all
    from health_sync.sync.guards import PersonGuardError
    from health_sync.sync.loop_engine import LoopSyncError

    config = _get_config()
    registry = _get_registry(config)

    # Parse Tidepool-only flags
    start_override = None
    if start_str:
        try:
            start_override = datetime.fromisoformat(start_str)
            if start_override.tzinfo is None:
                start_override = start_override.replace(tzinfo=timezone.utc)
            else:
                start_override = start_override.astimezone(timezone.utc)
        except ValueError as e:
            raise click.ClickException(f"Invalid --start date: {e}") from e

    # bulk-import is sugar for --full
    if bulk_import:
        full = True

    if provider:
        p = registry.get(provider)
        if not p:
            raise click.ClickException(f"Provider '{provider}' not found.")
        _resolve_person(config, p, person, override_patient)
        providers = [p]
    else:
        providers = registry.get_enabled()
        if person is not None:
            try:
                config.person(person)
            except ValueError as e:
                raise click.ClickException(str(e)) from e

    if not providers:
        raise click.ClickException("No providers to sync. Run: mychart-sync providers add")

    mode = " (dry run)" if dry_run else ""
    mode += " (full)" if full else ""
    click.echo(f"Syncing {len(providers)} provider(s){mode}...\n")

    try:
        # Preserve the existing Tidepool-first order for mixed-source runs.
        results = sync_all(
            config,
            sorted(providers, key=lambda p: p.kind != "tidepool"),
            dry_run=dry_run,
            full=full,
            person=person,
            override_patient=override_patient,
            start_override=start_override,
        )
    except (PersonGuardError, SyncTokenError, LoopSyncError) as e:
        raise click.ClickException(str(e)) from e

    # Summary
    click.echo(f"\n{'='*50}")
    click.echo("Sync complete")
    click.echo(f"{'='*50}")
    for slug, counts in results.items():
        total = sum(counts.values()) if isinstance(counts, dict) else 0
        click.echo(f"  {slug}: {total} items")


# --- Fetch ---


@main.command()
@click.argument("resource_type")
@click.option("--provider", "-p", required=True, help="Provider slug")
@click.option("--person", help="Fetch as this configured person")
@click.option(
    "--override-patient",
    is_flag=True,
    help="Allow fetching a provider for a person other than provider.patient",
)
@click.option("--category", "-c", help="Observation category (laboratory, vital-signs, social-history)")
@click.option("--pretty/--no-pretty", default=True, help="Pretty-print JSON output")
def fetch(
    resource_type: str,
    provider: str,
    person: str | None,
    override_patient: bool,
    category: str | None,
    pretty: bool,
) -> None:
    """Fetch FHIR resources and print raw JSON.

    RESOURCE_TYPE is a FHIR resource name (e.g., Patient, Condition, Observation).
    Tidepool providers are not supported by this command — use `sync` instead.
    """
    config = _get_config()
    registry = _get_registry(config)
    provider_config = registry.get(provider)
    if provider_config is None:
        raise click.ClickException(f"Provider '{provider}' not found.")

    if provider_config.kind != "fhir":
        raise click.ClickException(
            f"'{provider}' is a {provider_config.kind} provider; "
            f"the fetch command only supports FHIR. Use: mychart-sync sync "
            f"--provider {provider}"
        )

    target_person = _resolve_person(config, provider_config, person, override_patient)
    token_store = TokenStore(config.tokens_dir(target_person))

    try:
        token = token_store.get_valid_token(
            provider, config.get_client_id(provider), config.get_client_secret(provider)
        )
    except RuntimeError as e:
        raise click.ClickException(str(e)) from e

    cache_dir = config.fhir_cache_dir(target_person, provider)
    cache_dir.mkdir(parents=True, exist_ok=True)

    with FHIRClient(
        base_url=token.fhir_base_url,
        access_token=token.access_token,
        patient_id=token.patient_id,
        cache_dir=cache_dir,
    ) as client:
        if resource_type == "Patient":
            resources = [client.fetch_patient()]
        else:
            params = {}
            if category:
                params["category"] = category
            resources = client.fetch_resource(resource_type, params=params)

    indent = 2 if pretty else None
    click.echo(json.dumps(resources, indent=indent))
    click.echo(f"\n({len(resources)} resources)", err=True)


# --- Providers ---


@main.group()
def providers() -> None:
    """Manage health system connections."""
    pass


@providers.command("list")
def providers_list() -> None:
    """List all registered health systems."""
    config = _get_config()
    registry = _get_registry(config)

    all_providers = registry.list_all()
    if not all_providers:
        click.echo("No providers registered. Run: mychart-sync providers add")
        return

    click.echo(f"{'Slug':<16} {'Patient':<10} {'Kind':<10} {'Status':<10} {'Authenticated-for':<18} Name")
    click.echo("-" * 90)
    for p in all_providers:
        authenticated_for = []
        for person in config.persons:
            token_store = TokenStore(config.tokens_dir(person))
            if p.slug in token_store.list_authenticated():
                authenticated_for.append(person)
        # Tidepool credentials check
        if p.kind == "tidepool":
            from health_sync.auth.tidepool_credentials import _path_for

            for person in config.persons:
                cred_path = _path_for(person, config.secrets_dir())
                if cred_path.exists() and person not in authenticated_for:
                    authenticated_for.append(person)
        auth_text = ", ".join(authenticated_for) if authenticated_for else "none"
        patient = getattr(p, "patient", "unassigned")
        status = "enabled" if p.enabled else "disabled"
        click.echo(
            f"{p.slug:<16} {patient:<10} {p.kind:<10} {status:<10} "
            f"{auth_text:<18} {p.name}"
        )
        base_url = p.fhir_base_url if p.kind == "fhir" else p.tidepool_api_base
        if base_url:
            click.echo(f"  {base_url}")


@providers.command("add")
@click.option("--slug", prompt="Provider slug (e.g., mount-sinai)", help="Short identifier")
@click.option("--name", prompt="Display name (e.g., Mount Sinai Health System)", help="Full name")
@click.option("--url", "fhir_url", prompt="FHIR base URL", help="FHIR R4 endpoint URL")
@click.option("--patient", prompt="Patient/person key", help="Configured person key")
@click.option("--validate/--no-validate", default=True, help="Validate the endpoint")
def providers_add(
    slug: str,
    name: str,
    fhir_url: str,
    patient: str,
    validate: bool,
) -> None:
    """Register a new health system."""
    # Normalize slug
    slug = re.sub(r"[^a-z0-9-]", "-", slug.lower()).strip("-")
    if not slug:
        raise click.ClickException("Provider slug must contain a letter or number.")

    if validate:
        click.echo(f"Validating endpoint... ", nl=False)
        if validate_endpoint(fhir_url):
            click.echo("OK")
        else:
            click.echo("FAILED")
            if not click.confirm("Endpoint validation failed. Add anyway?"):
                return

    config = _get_config()
    try:
        config.person(patient)
    except ValueError as e:
        raise click.ClickException(str(e)) from e

    registry = _get_registry(config)
    provider = Provider(
        slug=slug,
        name=name,
        fhir_base_url=fhir_url.rstrip("/"),
        patient=patient,
    )

    try:
        registry.add(provider)
        click.echo(f"Added '{slug}'. Next: mychart-sync auth {slug}")
    except ValueError as e:
        raise click.ClickException(str(e)) from e


@providers.command("search")
@click.argument("query")
@click.option("--limit", "-n", default=10, help="Max results")
def providers_search(query: str, limit: int) -> None:
    """Search Epic's endpoint directory for a health system."""
    cache_dir = _project_cache_dir()

    click.echo(f"Searching for '{query}'...")
    matches = search_endpoints(query, cache_dir, limit=limit)

    if not matches:
        click.echo("No matches found.")
        return

    for i, m in enumerate(matches, 1):
        click.echo(f"  {i}. {m['name']}")
        click.echo(f"     {m['fhir_base_url']}")


@providers.command("remove")
@click.argument("slug")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def providers_remove(slug: str, yes: bool) -> None:
    """Remove a registered health system."""
    registry = _get_registry()
    config = _get_config()

    provider = registry.get(slug)
    if not provider:
        click.echo(f"Provider '{slug}' not found.")
        return

    if not yes:
        if not click.confirm(f"Remove '{provider.name}' ({slug})?"):
            return

    registry.remove(slug)
    token_store = TokenStore(config.tokens_dir(_provider_patient(provider)))
    token_store.delete(slug)
    click.echo(f"Removed '{slug}'.")


@providers.command("add-sandbox")
def providers_add_sandbox() -> None:
    """Add Epic's sandbox environment for testing."""
    config = _get_config()
    registry = _get_registry(config)

    provider = Provider(**SANDBOX_ENDPOINT, patient=config.default_person)
    try:
        registry.add(provider)
        click.echo(f"Added Epic sandbox. Next: mychart-sync auth epic-sandbox")
    except ValueError:
        click.echo("Epic sandbox already registered.")


# --- Persons ---


@main.group()
def persons() -> None:
    """Manage configured people."""
    pass


@persons.command("list")
def persons_list() -> None:
    """List configured people."""
    config = _get_config()
    for name, person_config in config.persons.items():
        default = " (default)" if name == config.default_person else ""
        click.echo(f"  {name}{default}: {person_config.output_dir}")


# --- Tidepool ---


@main.group()
def tidepool() -> None:
    """Manage Tidepool / Loop telemetry ingestion."""
    pass


@tidepool.command("import-export")
@click.option("--person", required=True, help="Configured person receiving the data")
@click.option(
    "--file",
    "export_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a Tidepool web-UI JSON export file",
)
@click.option("--dry-run", is_flag=True, help="Parse + roll up but do not write output files")
def tidepool_import_export(person: str, export_file, dry_run: bool) -> None:
    """Import a Tidepool web-UI JSON export into the output directory.

    Use Export Data at app.tidepool.org and choose JSON; see docs/tidepool.md.
    Writes per-day JSONL and Markdown to the configured output directory.
    """
    from health_sync.sync.loop_engine import LoopSyncError, sync_from_export

    config = _get_config()
    try:
        config.person(person)
    except ValueError as e:
        raise click.ClickException(str(e)) from e

    click.echo(f"Importing Tidepool export for {person}: {export_file}")
    if dry_run:
        click.echo("  (dry run)")

    try:
        counts = sync_from_export(export_file, person, config, dry_run=dry_run)
    except LoopSyncError as e:
        raise click.ClickException(str(e)) from e

    click.echo("")
    click.echo(f"Raw records:       {counts['raw_records']}")
    click.echo(f"Parsed records:    {counts['parsed_records']}")
    click.echo(f"Days covered:      {counts['days']}")
    click.echo(f"Daily files:       {counts['daily_files_written']}")
    if dry_run:
        click.echo("\n(nothing written — dry run)")


if __name__ == "__main__":
    main()
