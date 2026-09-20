from __future__ import annotations

import pytest
from pydantic import ValidationError

from health_sync.config import AppConfig
from health_sync.providers.registry import Provider


def test_persons_map_loads(app_config, fake_provider_factory) -> None:
    provider = fake_provider_factory()

    assert sorted(app_config.persons) == ["person_a", "person_b"]
    assert app_config.default_person == "person_a"
    assert app_config.brain_path("clinical_extract", "person_a").name == "clinical_extract.md"
    assert provider.slug == "mount-sinai"


def test_missing_paths_block_fails(app_config_data) -> None:
    del app_config_data["persons"]["person_b"]["paths"]

    with pytest.raises(ValidationError):
        AppConfig(**app_config_data)


def test_absolute_path_escape_is_rejected(app_config_data) -> None:
    app_config_data["persons"]["person_b"]["paths"]["clinical_extract"] = "/tmp/clinical_extract.md"
    config = AppConfig(**app_config_data)

    with pytest.raises(ValueError, match="must be relative"):
        config.brain_path("clinical_extract", "person_b")


def test_dotdot_path_escape_is_rejected(app_config_data) -> None:
    app_config_data["persons"]["person_b"]["paths"]["clinical_extract"] = (
        "person_b/../../profile.md"
    )
    config = AppConfig(**app_config_data)

    with pytest.raises(ValueError, match="escapes allowed root"):
        config.brain_path("clinical_extract", "person_b")


def test_default_person_must_be_configured(app_config_data) -> None:
    app_config_data["default_person"] = "nobody"

    with pytest.raises(ValidationError, match="default_person"):
        AppConfig(**app_config_data)


def test_brain_dir_env_var_does_not_override(monkeypatch, app_config_data, tmp_path) -> None:
    monkeypatch.setenv("BRAIN_DIR", str(tmp_path / "other-brain"))
    config = AppConfig(**app_config_data)

    assert config.brain_path("clinical_extract", "person_a").is_relative_to(
        app_config_data["persons"]["person_a"]["brain_dir"]
    )


@pytest.mark.parametrize("identifier", ["", " ", ".", "..", "../other", "/absolute", "a/b", "a\\b", "a\0b"])
def test_person_keys_cannot_be_paths(app_config_data, identifier):
    person = app_config_data["persons"].pop("person_a")
    app_config_data["persons"][identifier] = person
    app_config_data["default_person"] = identifier
    with pytest.raises(ValidationError):
        AppConfig(**app_config_data)


@pytest.mark.parametrize("identifier", ["", " ", ".", "..", "../other", "/absolute", "a/b", "a\\b", "a\0b"])
@pytest.mark.parametrize("field", ["slug", "patient"])
def test_provider_identifiers_cannot_be_paths(identifier, field):
    data = {"slug": "hospital", "patient": "me", "name": "Hospital", "kind": "tidepool"}
    data[field] = identifier
    with pytest.raises(ValidationError):
        Provider(**data)


def test_common_parent_escape_is_rejected(app_config_data):
    paths = app_config_data["persons"]["person_a"]["paths"]
    for key in paths:
        paths[key] = f"../outside/{key}"
    config = AppConfig(**app_config_data)
    with pytest.raises(ValueError, match="escapes allowed root"):
        config.output_path("clinical_extract", "person_a")


def test_symlink_escape_is_rejected(app_config_data, tmp_path):
    from pathlib import Path

    person = app_config_data["persons"]["person_a"]
    (Path(person["brain_dir"]) / "outside").symlink_to(tmp_path, target_is_directory=True)
    for key in person["paths"]:
        person["paths"][key] = f"outside/{key}"
    config = AppConfig(**app_config_data)
    with pytest.raises(ValueError, match="escapes allowed root"):
        config.output_path("clinical_extract", "person_a")
