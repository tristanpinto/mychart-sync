from __future__ import annotations

"""Read and update the hospital and Tidepool entries in config/providers.json."""

import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from health_sync.config import ConfigError, load_json_object, validate_path_component


class Provider(BaseModel):
    """A hospital connection (the default) or a Tidepool account."""

    slug: str
    name: str
    patient: str
    enabled: bool = True
    kind: Literal["fhir", "tidepool"] = "fhir"
    fhir_base_url: Optional[str] = None
    tidepool_api_base: Optional[str] = "https://api.tidepool.org"

    @field_validator("slug", "patient")
    @classmethod
    def _validate_identifiers(cls, value: str) -> str:
        return validate_path_component(value)

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
        data = load_json_object(self.config_path)
        rows = data.get("providers", [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ConfigError(f"Invalid {self.config_path}: providers must be a list of objects.")
        try:
            providers = [Provider(**row) for row in rows]
        except ValidationError as e:
            raise ConfigError(f"Invalid provider settings in {self.config_path}. Check SETUP.md.") from e
        if len({provider.slug for provider in providers}) != len(providers):
            raise ConfigError(f"Duplicate provider slugs in {self.config_path}.")
        return providers

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
