from __future__ import annotations

"""Tests for sync engine dispatch on provider.kind.

Critical regression coverage: FHIR providers must continue routing through
the existing sync_provider FHIR path after the discriminator is added. Without
this test, a bug in the kind-branch could silently break Person A's daily cron.
"""

import pytest

from health_sync.providers.registry import Provider
from health_sync.sync import engine, loop_engine


def _fhir_provider() -> Provider:
    return Provider(
        slug="mount-sinai",
        name="Mount Sinai",
        fhir_base_url="https://example/FHIR/R4",
        patient="person_a",
        kind="fhir",
    )


def _tidepool_provider() -> Provider:
    return Provider(
        slug="tidepool",
        name="Tidepool",
        patient="person_b",
        kind="tidepool",
        tidepool_api_base="https://api.tidepool.org",
    )


def test_fhir_provider_routes_through_fhir_sync(monkeypatch, app_config):
    """REGRESSION: kind=fhir must NOT call sync_tidepool_api."""
    tidepool_called = False
    fhir_called = False

    def fake_tidepool(*args, **kwargs):
        nonlocal tidepool_called
        tidepool_called = True
        return {"records": 0}

    def fake_fhir_internals(*args, **kwargs):
        nonlocal fhir_called
        fhir_called = True
        raise engine.SyncTokenError("expected — short-circuit")

    monkeypatch.setattr(loop_engine, "sync_tidepool_api", fake_tidepool)
    monkeypatch.setattr(engine, "TokenStore", lambda *a, **k: fake_fhir_internals())

    with pytest.raises(engine.SyncTokenError):
        engine.sync_provider(_fhir_provider(), "person_a", app_config)

    assert tidepool_called is False, "FHIR provider was incorrectly routed to Tidepool path"
    assert fhir_called is True


def test_tidepool_provider_routes_through_loop_engine(monkeypatch, app_config):
    """kind=tidepool must dispatch to loop_engine.sync_tidepool_api."""
    called_with = {}

    def fake_tidepool(provider, person, config, **kwargs):
        called_with["slug"] = provider.slug
        called_with["person"] = person
        called_with["kind"] = provider.kind
        return {"raw_records": 0}

    monkeypatch.setattr(loop_engine, "sync_tidepool_api", fake_tidepool)
    # also monkeypatch the import inside sync_provider
    monkeypatch.setattr(
        "health_sync.sync.engine.sync_tidepool_api", fake_tidepool, raising=False
    )

    result = engine.sync_provider(_tidepool_provider(), "person_b", app_config)

    assert called_with["slug"] == "tidepool"
    assert called_with["kind"] == "tidepool"
    assert called_with["person"] == "person_b"


def test_provider_without_kind_field_defaults_to_fhir(app_config):
    """Backward compat: a Provider built without kind defaults to kind='fhir'."""
    p = Provider(
        slug="legacy",
        name="Legacy",
        fhir_base_url="https://example/FHIR/R4",
        patient="person_a",
    )
    assert p.kind == "fhir"


def test_provider_kind_fhir_requires_fhir_base_url():
    """Validator: a fhir-kind provider must have fhir_base_url."""
    with pytest.raises(ValueError):
        Provider(slug="bad", name="Bad", patient="person_a", kind="fhir")


def test_provider_kind_tidepool_does_not_require_fhir_base_url():
    """Tidepool providers can (and should) omit fhir_base_url."""
    p = Provider(slug="tidepool", name="Tidepool", patient="person_b", kind="tidepool")
    assert p.kind == "tidepool"
    assert p.fhir_base_url is None
