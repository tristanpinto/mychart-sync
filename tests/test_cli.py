from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize("contents", ['{"client_id": "private-marker",', '[]', '{"client_id":"private-marker"}'])
def test_invalid_app_config_is_readable_and_redacted(cli_runner, config_file, contents):
    config_file.write_text(contents)
    result = cli_runner.invoke(cli.main, ["persons", "list"])
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "app.json" in result.output
    assert "private-marker" not in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("contents", [
    '{"private-marker":',
    '{"providers":"private-marker"}',
    '{"providers":[{"slug":"private-marker"}]}',
])
def test_invalid_provider_config_is_readable_and_redacted(cli_runner, config_file, providers_file, contents):
    providers_file.write_text(contents)
    result = cli_runner.invoke(cli.main, ["providers", "list"])
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "providers.json" in result.output
    assert "private-marker" not in result.output


def test_invalid_provider_secret_is_readable_and_redacted(cli_runner, config_file, providers_file):
    secret = config_file.parent.parent / "secrets/mount_sinai_secrets.json"
    secret.write_text('{"production":"private-marker"}')
    result = cli_runner.invoke(cli.main, ["auth", "mount-sinai"])
    assert result.exit_code == 1
    assert "Invalid environment settings" in result.output
    assert "private-marker" not in result.output


@pytest.mark.parametrize("slug, expected", [("!!!", "letter or number"), ("mount-sinai", "already registered")])
def test_provider_add_invalid_or_duplicate_fails(cli_runner, config_file, providers_file, slug, expected):
    result = cli_runner.invoke(cli.main, [
        "providers", "add", "--slug", slug, "--name", "Hospital",
        "--url", "https://hospital.example/FHIR/R4", "--patient", "person_a", "--no-validate",
    ])
    assert result.exit_code == 1
    assert expected in result.output


@pytest.mark.parametrize("date, expected", [
    ("2024-01-01", "2024-01-01T00:00:00+00:00"),
    ("2024-01-01T08:00:00-05:00", "2024-01-01T13:00:00+00:00"),
])
def test_start_preserves_supplied_timezone(cli_runner, config_file, providers_file, monkeypatch, date, expected):
    providers_file.write_text(json.dumps({"providers": [
        {"slug": "tidepool", "name": "Tidepool", "kind": "tidepool", "patient": "person_a"},
    ]}))
    calls = []

    def fake_sync(*args, **kwargs):
        calls.append(kwargs["start_override"].isoformat())
        return {}

    monkeypatch.setattr("health_sync.sync.loop_engine.sync_tidepool_api", fake_sync)
    result = cli_runner.invoke(cli.main, ["sync", "--provider", "tidepool", "--start", date])
    assert result.exit_code == 0
    assert calls == [expected]


def test_mixed_sync_uses_one_dispatch_and_preserves_order(
    cli_runner, config_file, providers_file, monkeypatch,
):
    from health_sync.sync import engine

    providers_file.write_text(json.dumps({"providers": [
        {"slug": "hospital", "name": "Hospital", "patient": "person_a", "fhir_base_url": "https://hospital.example/FHIR/R4"},
        {"slug": "tidepool", "name": "Tidepool", "kind": "tidepool", "patient": "person_a"},
    ]}))
    dispatches = []
    sources = []
    original_sync_all = engine.sync_all

    def record_dispatch(config, providers, **kwargs):
        dispatches.append([provider.slug for provider in providers])
        return original_sync_all(config, providers, **kwargs)

    def record_source(provider, person, config, **kwargs):
        sources.append((provider.slug, person, kwargs["start_override"].isoformat()))
        return {}

    monkeypatch.setattr(engine, "sync_all", record_dispatch)
    monkeypatch.setattr(engine, "sync_provider", record_source)
    result = cli_runner.invoke(cli.main, ["sync", "--start", "2024-01-01"])
    assert result.exit_code == 0
    assert dispatches == [["tidepool", "hospital"]]
    assert sources == [
        ("tidepool", "person_a", "2024-01-01T00:00:00+00:00"),
        ("hospital", "person_a", "2024-01-01T00:00:00+00:00"),
    ]


def test_help_describes_dry_run_and_current_export_route(cli_runner):
    sync_help = cli_runner.invoke(cli.main, ["sync", "--help"])
    assert sync_help.exit_code == 0
    assert "downloads raw FHIR data" in sync_help.output
    export_help = cli_runner.invoke(cli.main, ["tidepool", "import-export", "--help"])
    assert export_help.exit_code == 0
    assert "app.tidepool.org" in export_help.output
    assert "Account" not in export_help.output
