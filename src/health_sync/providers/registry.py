from __future__ import annotations

"""Multi-provider registry management.

Manages the list of registered health systems in config/providers.json.
Each provider has a slug, display name, FHIR base URL, assigned patient, and
enabled flag.
"""

import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, model_validator


class Provider(BaseModel):
    """A registered health system or non-FHIR data source.

    The `kind` discriminator distinguishes FHIR providers from non-FHIR
    sources (currently: Tidepool legacy session-token API). Existing rows
    in providers.json that omit `kind` default to "fhir" so the migration
    is backward-compatible.
    """

    slug: str
    name: str
    patient: str
    enabled: bool = True
    kind: Literal["fhir", "tidepool"] = "fhir"
    fhir_base_url: Optional[str] = None
    tidepool_api_base: Optional[str] = "https://api.tidepool.org"

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> "Provider":
        if self.kind == "fhir" and not self.fhir_base_url:
            raise ValueError(
                f"Provider '{self.slug}' has kind=fhir but no fhir_base_url"
            )
        return self


class ProviderRegistry:
    """Manages the provider configuration file."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path

    def _load(self) -> list[Provider]:
        if not self.config_path.exists():
            return []
        data = json.loads(self.config_path.read_text())
        return [Provider(**p) for p in data.get("providers", [])]

    def _save(self, providers: list[Provider]) -> None:
        data = {"providers": [p.model_dump() for p in providers]}
        self.config_path.write_text(json.dumps(data, indent=2) + "\n")

    def list_all(self) -> list[Provider]:
        return self._load()

    def get_enabled(self) -> list[Provider]:
        return [p for p in self._load() if p.enabled]

    def get(self, slug: str) -> Provider | None:
        for p in self._load():
            if p.slug == slug:
                return p
        return None

    def add(self, provider: Provider) -> None:
        """Add a new provider. Raises if slug already exists."""
        providers = self._load()
        if any(p.slug == provider.slug for p in providers):
            raise ValueError(f"Provider '{provider.slug}' already registered")
        providers.append(provider)
        self._save(providers)

    def remove(self, slug: str) -> bool:
        """Remove a provider by slug. Returns True if found."""
        providers = self._load()
        filtered = [p for p in providers if p.slug != slug]
        if len(filtered) == len(providers):
            return False
        self._save(filtered)
        return True

    def set_enabled(self, slug: str, enabled: bool) -> bool:
        """Enable or disable a provider. Returns True if found."""
        providers = self._load()
        for p in providers:
            if p.slug == slug:
                p.enabled = enabled
                self._save(providers)
                return True
        return False
