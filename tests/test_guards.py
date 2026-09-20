from __future__ import annotations

"""Tests for sync/guards.py — extracted person-guard helper."""

import pytest

from health_sync.providers.registry import Provider
from health_sync.sync.guards import (
    PersonGuardError,
    check_person_guard,
    provider_patient,
)


def _provider(slug: str = "ucsf", patient: str = "person_b", kind: str = "fhir") -> Provider:
    if kind == "fhir":
        return Provider(
            slug=slug,
            name=slug,
            fhir_base_url=f"https://{slug}.example/FHIR/R4",
            patient=patient,
            kind="fhir",
        )
    return Provider(slug=slug, name=slug, patient=patient, kind="tidepool")


def test_provider_patient_returns_assigned_patient() -> None:
    assert provider_patient(_provider(patient="person_b")) == "person_b"


def test_provider_patient_raises_when_unassigned() -> None:
    # We can't make patient empty via the model, but we can simulate via
    # a non-Provider with missing attribute
    class FakeProvider:
        slug = "x"
        patient = None

    with pytest.raises(PersonGuardError):
        provider_patient(FakeProvider())  # type: ignore[arg-type]


def test_check_person_guard_passes_when_person_matches() -> None:
    p = _provider(patient="person_b")
    assert check_person_guard(p, "person_b", False) == "person_b"


def test_check_person_guard_raises_when_person_mismatched() -> None:
    p = _provider(patient="person_b")
    with pytest.raises(PersonGuardError) as excinfo:
        check_person_guard(p, "person_a", False)
    assert "person_b" in str(excinfo.value)
    assert "person_a" in str(excinfo.value)
    assert "--override-patient" in str(excinfo.value)


def test_check_person_guard_allows_mismatch_with_override() -> None:
    p = _provider(patient="person_b")
    assert check_person_guard(p, "person_a", override_patient=True) == "person_a"


def test_guard_works_for_tidepool_kind() -> None:
    p = _provider(kind="tidepool", patient="person_b")
    assert check_person_guard(p, "person_b", False) == "person_b"
    with pytest.raises(PersonGuardError):
        check_person_guard(p, "person_a", False)
