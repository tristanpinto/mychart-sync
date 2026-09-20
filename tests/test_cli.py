from __future__ import annotations

import json
import time
from types import SimpleNamespace

from health_sync.auth.token_store import StoredToken, TokenStore
from health_sync import cli


def test_persons_list_prints_configured_people(cli_runner, config_file) -> None:
    result = cli_runner.invoke(cli.main, ["persons", "list"])

    assert result.exit_code == 0
    assert "person_a (default)" in result.output
    assert "person_b" in result.output


def test_providers_list_shows_patient_and_authenticated_for(
    cli_runner,
    config_file,
    providers_file,
    app_config,
    stored_token_factory,
) -> None:
    TokenStore(app_config.tokens_dir("person_b")).save(
        stored_token_factory(slug="ucsf", person="person_b")
    )

    result = cli_runner.invoke(cli.main, ["providers", "list"])

    assert result.exit_code == 0
    assert "Patient" in result.output
    assert "mount-sinai      person_a" in result.output
    assert "ucsf             person_b" in result.output
    assert "person_b" in result.output


def test_sync_default_routing_uses_provider_patient(
    monkeypatch,
    cli_runner,
    config_file,
    providers_file,
) -> None:
    calls = {}

    def fake_sync_all(config, providers, **kwargs):
        calls["provider_patient"] = providers[0].patient
        calls["person"] = kwargs["person"]
        return {providers[0].slug: {"Patient": 1}}

    monkeypatch.setattr("health_sync.sync.engine.sync_all", fake_sync_all)

    result = cli_runner.invoke(cli.main, ["sync", "--provider", "ucsf", "--dry-run"])

    assert result.exit_code == 0
    assert calls == {"provider_patient": "person_b", "person": None}


def test_sync_person_override_requires_flag(
    monkeypatch,
    cli_runner,
    config_file,
    providers_file,
) -> None:
    blocked = cli_runner.invoke(
        cli.main,
        ["sync", "--provider", "mount-sinai", "--person", "person_b", "--dry-run"],
    )
    assert blocked.exit_code != 0
    assert "Pass --override-patient" in blocked.output

    monkeypatch.setattr(
        "health_sync.sync.engine.sync_all",
        lambda config, providers, **kwargs: {providers[0].slug: {"Patient": 1}},
    )
    allowed = cli_runner.invoke(
        cli.main,
        [
            "sync",
            "--provider",
            "mount-sinai",
            "--person",
            "person_b",
            "--override-patient",
            "--dry-run",
        ],
    )
    assert allowed.exit_code == 0


def test_auth_writes_person_scoped_token(
    monkeypatch,
    cli_runner,
    config_file,
    providers_file,
    app_config,
) -> None:
    monkeypatch.setattr(
        cli,
        "authenticate",
        lambda **kwargs: SimpleNamespace(
            access_token="access",
            refresh_token="refresh",
            expires_in=3600,
            patient_id="person_b-patient",
            token_endpoint="https://ucsf.example/token",
            scope="patient/*.read",
        ),
    )

    result = cli_runner.invoke(cli.main, ["auth", "ucsf"])

    assert result.exit_code == 0
    token_path = app_config.tokens_dir("person_b") / "ucsf_token.json"
    assert token_path.exists()
    assert json.loads(token_path.read_text())["person"] == "person_b"


def test_fetch_supports_person_and_reports_missing_token(
    cli_runner,
    config_file,
    providers_file,
) -> None:
    result = cli_runner.invoke(cli.main, ["fetch", "Condition", "--provider", "ucsf"])

    assert result.exit_code != 0
    assert "No token found for 'ucsf'" in result.output


def test_providers_add_requires_valid_patient(
    cli_runner,
    config_file,
    providers_file,
) -> None:
    result = cli_runner.invoke(
        cli.main,
        [
            "providers",
            "add",
            "--slug",
            "new-system",
            "--name",
            "New System",
            "--url",
            "https://new.example/FHIR/R4",
            "--patient",
            "person_b",
            "--no-validate",
        ],
    )

    assert result.exit_code == 0
    assert "Added 'new-system'" in result.output


def test_providers_remove_deletes_only_assigned_patient_token(
    cli_runner,
    config_file,
    providers_file,
    app_config,
) -> None:
    person_a_store = TokenStore(app_config.tokens_dir("person_a"))
    person_b_store = TokenStore(app_config.tokens_dir("person_b"))
    person_a_store.save(
        StoredToken(
            provider_slug="mount-sinai",
            person="person_a",
            fhir_base_url="https://mount.example/FHIR/R4",
            access_token="access",
            refresh_token="refresh",
            expires_at=time.time() + 3600,
            patient_id="person_a",
            token_endpoint="https://mount.example/token",
        )
    )
    person_b_store.save(
        StoredToken(
            provider_slug="mount-sinai",
            person="person_b",
            fhir_base_url="https://mount.example/FHIR/R4",
            access_token="access",
            refresh_token="refresh",
            expires_at=time.time() + 3600,
            patient_id="person_b",
            token_endpoint="https://mount.example/token",
        )
    )

    result = cli_runner.invoke(cli.main, ["providers", "remove", "mount-sinai", "--yes"])

    assert result.exit_code == 0
    assert not (app_config.tokens_dir("person_a") / "mount-sinai_token.json").exists()
    assert (app_config.tokens_dir("person_b") / "mount-sinai_token.json").exists()
