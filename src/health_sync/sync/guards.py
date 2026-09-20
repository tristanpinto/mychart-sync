from __future__ import annotations

"""Person-guard helpers shared across sync engines.

Extracted from sync/engine.py so the FHIR engine (sync_provider) and the
Tidepool engine (sync_tidepool_api) use the same logic. If the person-guard
rule changes (e.g., a new override flag), both engines update together.
"""

from health_sync.providers.registry import Provider


class PersonGuardError(RuntimeError):
    """Raised when a provider is being synced for the wrong configured person."""


def provider_patient(provider: Provider) -> str:
    """Return the provider's assigned patient, raising if unset."""
    patient = getattr(provider, "patient", None)
    if not patient:
        raise PersonGuardError(f"Provider '{provider.slug}' has no assigned patient")
    return patient


def check_person_guard(
    provider: Provider, person: str, override_patient: bool
) -> str:
    """Validate that this provider may sync for `person`.

    Returns the target person on success. Raises PersonGuardError if the
    provider is assigned to someone else and override_patient is False.
    """
    assigned = provider_patient(provider)
    if assigned != person and not override_patient:
        raise PersonGuardError(
            f"Provider '{provider.slug}' is assigned to '{assigned}', "
            f"not '{person}'. Pass --override-patient to override."
        )
    return person
