from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import health_sync.config as config_module
from health_sync.auth.token_store import StoredToken
from health_sync.config import AppConfig
from health_sync.providers.registry import Provider


@pytest.fixture
def tmp_health_sync_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Project-shaped tmpdir that keeps tests away from real secrets and brain files."""
    root = tmp_path / "health-sync"
    for rel in (
        "cache/fhir",
        "cache/state",
        "config",
        "secrets/tokens",
        "brain/person_a/records/labs",
        "brain/person_b/records",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("BRAIN_DIR", str(root / "brain"))
    monkeypatch.setattr(config_module, "_project_root", lambda: root)
    return root


@pytest.fixture
def app_config_data(tmp_health_sync_root: Path) -> dict[str, Any]:
    brain_dir = str(tmp_health_sync_root / "brain")
    return {
        "client_id": "prod-client-id",
        "sandbox_client_id": "sandbox-client-id",
        "redirect_uri": "https://localhost:8080/callback",
        "default_person": "person_a",
        "persons": {
            "person_a": {
                "brain_dir": brain_dir,
                "paths": {
                    "clinical_extract": "person_a/records/clinical_extract.md",
                    "health_profile": "person_a/health_profile.md",
                    "lab_results": "person_a/records/labs/lab_results.md",
                    "medication_log": "person_a/medication_log.md",
                    "recovery_snapshots": "person_a/recovery_snapshots_and_progress.md",
                    "records_dir": "person_a/records",
                },
            },
            "person_b": {
                "brain_dir": brain_dir,
                "paths": {
                    "clinical_extract": "person_b/clinical_extract.md",
                    "health_profile": "person_b/health_profile.md",
                    "lab_results": "person_b/lab_results.md",
                    "records_dir": "person_b/records",
                },
            },
        },
    }


@pytest.fixture
def app_config(app_config_data: dict[str, Any]) -> AppConfig:
    return AppConfig(**app_config_data)


@pytest.fixture
def config_file(tmp_health_sync_root: Path, app_config_data: dict[str, Any]) -> Path:
    path = tmp_health_sync_root / "config" / "app.json"
    path.write_text(json.dumps(app_config_data, indent=2) + "\n")
    return path


@pytest.fixture
def providers_file(tmp_health_sync_root: Path) -> Path:
    data = {
        "providers": [
            {
                "slug": "mount-sinai",
                "name": "Mount Sinai Health System",
                "fhir_base_url": "https://mount.example/FHIR/R4",
                "patient": "person_a",
                "enabled": True,
            },
            {
                "slug": "ucsf",
                "name": "UCSF Health",
                "fhir_base_url": "https://ucsf.example/FHIR/R4",
                "patient": "person_b",
                "enabled": False,
            },
        ]
    }
    path = tmp_health_sync_root / "config" / "providers.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_provider_factory() -> Callable[..., Provider]:
    def factory(
        *,
        slug: str = "mount-sinai",
        name: str = "Mount Sinai Health System",
        patient: str = "person_a",
        enabled: bool = True,
    ) -> Provider:
        data = {
            "slug": slug,
            "name": name,
            "fhir_base_url": f"https://{slug}.example/FHIR/R4",
            "patient": patient,
            "enabled": enabled,
        }
        return Provider(**data)

    return factory


@pytest.fixture
def stored_token_factory() -> Callable[..., StoredToken]:
    def factory(
        *,
        slug: str = "mount-sinai",
        person: str = "person_a",
        access_token: str = "access-token",
        refresh_token: str = "refresh-token",
    ) -> StoredToken:
        data = {
            "provider_slug": slug,
            "fhir_base_url": f"https://{slug}.example/FHIR/R4",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": time.time() + 3600,
            "patient_id": f"{person}-patient-id",
            "token_endpoint": f"https://{slug}.example/oauth2/token",
            "scope": "patient/*.read",
        }
        data["person"] = person
        return StoredToken(**data)

    return factory


@pytest.fixture
def mock_fhir_data() -> dict[str, list[dict[str, Any]]]:
    return {
        "Patient": [{"resourceType": "Patient", "id": "patient-1"}],
        "Condition": [],
        "MedicationRequest": [],
        "AllergyIntolerance": [],
        "Immunization": [],
        "Encounter": [],
        "Procedure": [],
        "CareTeam": [],
        "Observation": [],
        "DocumentReference": [],
    }


@pytest.fixture
def mock_fhir_client(mock_fhir_data: dict[str, list[dict[str, Any]]]) -> type:
    class MockFHIRClient:
        instances: list["MockFHIRClient"] = []

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs
            self.closed = False
            self.__class__.instances.append(self)

        def __enter__(self) -> "MockFHIRClient":
            return self

        def __exit__(self, *args: Any) -> None:
            self.close()

        def fetch_all(self, since: str | None = None) -> dict[str, list[dict[str, Any]]]:
            return {**mock_fhir_data, "Patient": [{"resourceType": "Patient", "id": self.kwargs["patient_id"]}]}

        def fetch_patient(self) -> dict[str, Any]:
            return mock_fhir_data["Patient"][0]

        def fetch_resource(
            self, resource_type: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            return mock_fhir_data.get(resource_type, [])

        def close(self) -> None:
            self.closed = True

    return MockFHIRClient


@pytest.fixture
def person_a_legacy_state(
    tmp_health_sync_root: Path,
    stored_token_factory: Callable[..., StoredToken],
) -> Path:
    token = stored_token_factory(slug="mount-sinai", person="person_a")
    sandbox = stored_token_factory(slug="epic-sandbox", person="person_a")

    tokens_dir = tmp_health_sync_root / "secrets" / "tokens"
    (tokens_dir / "mount-sinai_token.json").write_text(token.model_dump_json(indent=2))
    (tokens_dir / "epic-sandbox_token.json").write_text(sandbox.model_dump_json(indent=2))

    cache_dir = tmp_health_sync_root / "cache"
    (cache_dir / "mount-sinai_state.json").write_text('{"last_sync": "2026-04-09T00:00:00Z"}')
    (cache_dir / "epic-sandbox_state.json").write_text('{"last_sync": "2026-04-01T00:00:00Z"}')
    (cache_dir / "fhir" / "mount-sinai").mkdir(parents=True)
    (cache_dir / "fhir" / "mount-sinai" / "Patient.json").write_text("[]")

    snapshots = (
        tmp_health_sync_root
        / "brain"
        / "local_snapshots"
        / "outputs"
        / ".pre_sync_snapshots"
        / "2026-04-09"
    )
    snapshots.mkdir(parents=True)
    (snapshots / "clinical_extract.md").write_text("# Snapshot\n")
    return tmp_health_sync_root
