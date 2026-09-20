from __future__ import annotations

from types import SimpleNamespace

import pytest

from health_sync.auth.token_store import TokenStore
from health_sync.sync import engine


def provider(slug: str = "mount-sinai", patient: str = "person_a") -> SimpleNamespace:
    return SimpleNamespace(
        slug=slug,
        name=slug,
        fhir_base_url=f"https://{slug}.example/FHIR/R4",
        patient=patient,
        enabled=True,
    )


def test_person_guard_fires_before_store_constructors(monkeypatch, app_config) -> None:
    token_store_called = False
    sync_state_called = False

    def token_store_ctor(*args, **kwargs):
        nonlocal token_store_called
        token_store_called = True
        raise AssertionError("TokenStore should not be constructed")

    def sync_state_ctor(*args, **kwargs):
        nonlocal sync_state_called
        sync_state_called = True
        raise AssertionError("SyncState should not be constructed")

    monkeypatch.setattr(engine, "TokenStore", token_store_ctor)
    monkeypatch.setattr(engine, "SyncState", sync_state_ctor)

    with pytest.raises(engine.PersonGuardError):
        engine.sync_provider(provider(slug="ucsf", patient="person_b"), "person_a", app_config)

    assert token_store_called is False
    assert sync_state_called is False


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
    assert mock_fhir_client.instances[0].kwargs["cache_dir"] == (
        app_config.cache_dir("person_a") / "fhir" / "mount-sinai"
    )


def test_missing_token_is_non_silent(app_config) -> None:
    with pytest.raises(engine.SyncTokenError, match="No token found"):
        engine.sync_provider(provider(), "person_a", app_config, dry_run=True)
