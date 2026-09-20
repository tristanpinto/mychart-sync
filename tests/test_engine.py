from __future__ import annotations

from types import SimpleNamespace

import pytest

from health_sync.auth.token_store import TokenStore
from health_sync.sync import engine
from health_sync.sync.state import SyncState


def provider(slug: str = "mount-sinai", patient: str = "person_a") -> SimpleNamespace:
    return SimpleNamespace(
        slug=slug,
        name=slug,
        fhir_base_url=f"https://{slug}.example/FHIR/R4",
        patient=patient,
        enabled=True,
    )


def test_person_guard_fires_before_store_constructors(monkeypatch, app_config) -> None:
    def unexpected_store(*args, **kwargs):
        pytest.fail("Person guard must run before constructing stores")

    monkeypatch.setattr(engine, "TokenStore", unexpected_store)
    monkeypatch.setattr(engine, "SyncState", unexpected_store)

    with pytest.raises(engine.PersonGuardError):
        engine.sync_provider(provider(slug="ucsf", patient="person_b"), "person_a", app_config)

def test_sync_provider_uses_person_scoped_cache(
    monkeypatch,
    app_config,
    mock_fhir_client,
    stored_token_factory,
) -> None:
    token_store = TokenStore(app_config.tokens_dir("person_a"))
    token_store.save(stored_token_factory(slug="mount-sinai", person="person_a"))

    monkeypatch.setattr(engine, "FHIRClient", mock_fhir_client)
    counts = engine.sync_provider(
        provider(),
        "person_a",
        app_config,
        dry_run=True,
    )

    assert sum(counts.values()) == 1
    assert mock_fhir_client.instances[0].kwargs["cache_dir"] is None
    assert (app_config.cache_dir("person_a") / "fhir/mount-sinai/records.json").exists()
    assert not (app_config.cache_dir("person_b") / "fhir/mount-sinai/records.json").exists()


def test_missing_token_is_non_silent(app_config) -> None:
    with pytest.raises(engine.SyncTokenError, match="No token found"):
        engine.sync_provider(provider(), "person_a", app_config, dry_run=True)


def test_dry_run_does_not_advance_state(
    monkeypatch, app_config, mock_fhir_client, stored_token_factory,
):
    TokenStore(app_config.tokens_dir("person_a")).save(stored_token_factory())
    state = SyncState(app_config.sync_state_dir("person_a"))
    state.record_sync("mount-sinai", {}, synced_at="2020-01-01T00:00:00Z")
    before = state.load("mount-sinai")
    monkeypatch.setattr(engine, "FHIRClient", mock_fhir_client)
    engine.sync_provider(provider(), "person_a", app_config, dry_run=True)
    assert state.load("mount-sinai") == before
    assert mock_fhir_client.instances[-1].closed


def test_document_failure_preserves_checkpoint_and_closes_client(
    monkeypatch, app_config, mock_fhir_client, stored_token_factory,
):
    TokenStore(app_config.tokens_dir("person_a")).save(stored_token_factory())
    state = SyncState(app_config.sync_state_dir("person_a"))
    state.record_sync("mount-sinai", {}, synced_at="2020-01-01T00:00:00Z")
    before = state.load("mount-sinai")
    monkeypatch.setattr(engine, "FHIRClient", mock_fhir_client)

    def fail(*args):
        raise RuntimeError("Document download incomplete")

    monkeypatch.setattr(engine, "download_documents", fail)
    with pytest.raises(RuntimeError, match="incomplete"):
        engine.sync_provider(provider(), "person_a", app_config)
    assert state.load("mount-sinai") == before
    assert mock_fhir_client.instances[-1].closed


def test_checkpoint_uses_fetch_start_not_finish(
    monkeypatch, app_config, mock_fhir_client, stored_token_factory,
):
    from datetime import datetime, timezone

    TokenStore(app_config.tokens_dir("person_a")).save(stored_token_factory())
    before = datetime.now(timezone.utc)
    monkeypatch.setattr(engine, "FHIRClient", mock_fhir_client)
    observed = []

    def documents(*args):
        observed.append(datetime.now(timezone.utc))
        return 0

    monkeypatch.setattr(engine, "download_documents", documents)
    engine.sync_provider(provider(), "person_a", app_config)
    state = SyncState(app_config.sync_state_dir("person_a"))
    checkpoint = datetime.fromisoformat(state.get_last_sync("mount-sinai"))
    assert before <= checkpoint <= observed[0]
    assert mock_fhir_client.instances[-1].closed
