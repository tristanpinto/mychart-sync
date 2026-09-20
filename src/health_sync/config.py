from __future__ import annotations

"""Configuration loading for mychart-sync."""

import json
import os
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ConfigError(ValueError):
    """A local configuration error that is safe to show without its contents."""


def validate_path_component(value: str) -> str:
    if not value.strip() or value in {".", ".."} or any(c in value for c in ("/", "\\", "\0")):
        raise ValueError("Identifiers must be nonempty names without path separators")
    return value


def load_json_object(path: Path) -> dict:
    """Read local settings without exposing their values in parse errors."""
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeError) as e:
        raise ConfigError(f"Invalid JSON in {path}. Check commas, quotes, and braces.") from e
    except OSError as e:
        raise ConfigError(f"Cannot read {path}. Check that it exists and is readable.") from e
    if not isinstance(data, dict):
        raise ConfigError(f"Invalid {path}: expected a JSON object.")
    return data


def _project_root() -> Path:
    """Return the mychart-sync project root (where pyproject.toml lives)."""
    return Path(__file__).resolve().parent.parent.parent


class OutputPaths(BaseModel):
    clinical_extract: str
    health_profile: str
    lab_results: str
    medication_log: Optional[str] = None
    recovery_snapshots: Optional[str] = None
    records_dir: str
    raw_fhir_dir: Optional[str] = None
    loop_raw_dir: Optional[str] = None
    loop_daily_dir: Optional[str] = None
    loop_telemetry: Optional[str] = None


BrainPaths = OutputPaths  # Compatibility with existing local configurations.


class Person(BaseModel):
    output_dir: Path = Field(validation_alias=AliasChoices("output_dir", "brain_dir"))
    paths: OutputPaths
    timezone: str = "America/Los_Angeles"

    @field_validator("output_dir", mode="before")
    @classmethod
    def expand_output_dir(cls, v: str | Path) -> Path:
        path = Path(os.path.expanduser(str(v)))
        return path if path.is_absolute() else _project_root() / path

    @property
    def brain_dir(self) -> Path:
        return self.output_dir

    def allowed_root(self) -> Path:
        path_values = [
            value
            for value in self.paths.model_dump().values()
            if value is not None
        ]
        try:
            common = os.path.commonpath(path_values)
        except ValueError as e:
            raise ConfigError("Output paths must all be relative to output_dir.") from e
        return (self.output_dir / common).resolve()


class AppConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    client_id: str
    sandbox_client_id: Optional[str] = None
    redirect_uri: str = "http://localhost:8080/callback"
    default_person: str
    persons: dict[str, Person]

    @field_validator("persons")
    @classmethod
    def validate_person_names(cls, v: dict[str, Person]) -> dict[str, Person]:
        if not v:
            raise ValueError("At least one person must be configured")
        for name in v:
            validate_path_component(name)
        return v

    @model_validator(mode="after")
    def validate_default_person(self) -> "AppConfig":
        if self.default_person not in self.persons:
            raise ValueError(
                f"default_person '{self.default_person}' is not in persons"
            )
        return self

    def person(self, person: str | None = None) -> Person:
        person_name = person or self.default_person
        if person_name not in self.persons:
            raise ValueError(f"Unknown person '{person_name}'")
        return self.persons[person_name]

    def output_path(self, key: str, person: str) -> Path:
        """Resolve an output-relative path to an absolute path."""
        person_config = self.person(person)
        rel = getattr(person_config.paths, key)
        if rel is None:
            raise ConfigError(f"Path '{key}' is not configured for this person")

        rel_path = Path(rel)
        if rel_path.is_absolute():
            raise ConfigError(f"Path '{key}' must be relative")

        result = (person_config.output_dir / rel_path).resolve()
        if not result.is_relative_to(person_config.output_dir.resolve()):
            raise ConfigError(f"Path '{key}' escapes allowed root (output_dir)")
        allowed_root = person_config.allowed_root()
        if not result.is_relative_to(allowed_root):
            raise ConfigError(f"Path '{key}' escapes allowed root for this person")
        return result

    brain_path = output_path  # Keep existing callers working.

    def fhir_cache_dir(self, person: str, slug: str) -> Path:
        validate_path_component(slug)
        if self.person(person).paths.raw_fhir_dir is not None:
            return self.output_path("raw_fhir_dir", person) / slug
        return self.cache_dir(person) / "fhir" / slug

    def secrets_dir(self) -> Path:
        return _project_root() / "secrets"

    def tokens_dir(self, person: str) -> Path:
        self.person(person)
        d = self.secrets_dir() / "tokens" / person
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cache_dir(self, person: str) -> Path:
        self.person(person)
        d = _project_root() / "cache" / person
        d.mkdir(parents=True, exist_ok=True)
        return d

    def sync_state_dir(self, person: str) -> Path:
        self.person(person)
        d = _project_root() / "cache" / "state" / person
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _provider_secret_data(self, slug: str) -> Optional[dict]:
        """Load per-provider OAuth settings, if available.

        Checks for a per-provider secret file (secrets/{slug}_secrets.json).
        """
        validate_path_component(slug)
        provider_secret_file = self.secrets_dir() / f"{slug.replace('-', '_')}_secrets.json"
        if provider_secret_file.exists():
            data = load_json_object(provider_secret_file)
            if any(not isinstance(data.get(env, {}), dict) for env in ("production", "non_production")):
                raise ConfigError(f"Invalid environment settings in {provider_secret_file}.")
            return data
        return None

    def _provider_env(self, slug: str) -> str:
        """Return the Epic environment bucket for a provider slug."""
        return "non_production" if slug == "epic-sandbox" else "production"

    def get_client_id(self, slug: str) -> str:
        """Return the client ID for a provider."""
        data = self._provider_secret_data(slug)
        env = self._provider_env(slug)
        if data:
            client_id = data.get(env, {}).get("client_id")
            if client_id:
                return client_id
        if env == "non_production" and self.sandbox_client_id:
            return self.sandbox_client_id
        return self.client_id

    def get_client_secret(self, slug: str) -> Optional[str]:
        """Load a client secret for a provider, if available."""
        data = self._provider_secret_data(slug)
        env = self._provider_env(slug)
        if data:
            secret = data.get(env, {}).get("client_secret")
            if secret:
                return secret
        return None


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load app config from JSON file."""
    if config_path is None:
        config_path = _project_root() / "config" / "app.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"Missing {config_path}. Copy config/app.example.json to config/app.json "
            "and set your Epic client ID."
        )
    data = load_json_object(config_path)
    try:
        return AppConfig(**data)
    except ValidationError as e:
        raise ConfigError(f"Invalid settings in {config_path}. Compare fields with config/app.example.json.") from e
