from __future__ import annotations

import json
from pathlib import Path

import pytest

from health_sync import cli
from health_sync.auth.token_store import TokenStore
from health_sync.config import AppConfig
from health_sync.fhir.client import FHIRClient
from health_sync.sync import engine


def example_config() -> dict:
    path = Path(__file__).resolve().parents[1] / "config/app.example.json"
    return json.loads(path.read_text())


def test_example_output_is_checkout_relative(tmp_health_sync_root, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config = AppConfig(**example_config())
    assert config.person().output_dir == tmp_health_sync_root / "output"
    assert config.fhir_cache_dir("me", "hospital") == tmp_health_sync_root / "output/raw/hospital"


def test_legacy_aliases_keep_custom_paths(app_config):
    person = app_config.person("person_a")
    assert person.brain_dir == person.output_dir
    assert app_config.brain_path("lab_results", "person_a") == app_config.output_path("lab_results", "person_a")
    assert app_config.fhir_cache_dir("person_a", "hospital") == app_config.cache_dir("person_a") / "fhir/hospital"


def test_missing_config_explains_setup(cli_runner, tmp_health_sync_root):
    result = cli_runner.invoke(cli.main, ["persons", "list"])
    assert result.exit_code == 1
    assert "Copy config/app.example.json" in result.output


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("raw_in_output", [False, True])
def test_first_sync_writes_plain_output(
    tmp_health_sync_root, monkeypatch, httpx_mock, stored_token_factory,
    fake_provider_factory, dry_run, raw_in_output,
):
    data = example_config()
    if not raw_in_output:
        del data["persons"]["me"]["paths"]["raw_fhir_dir"]
    data["persons"]["me"]["paths"]["lab_results"] = "labs/lab_results.md"
    config = AppConfig(**data)
    token = stored_token_factory(slug="hospital", person="me")
    TokenStore(config.tokens_dir("me")).save(token)
    provider = fake_provider_factory(slug="hospital", patient="me", name="Example Hospital")

    def respond(request):
        import httpx

        if "/Patient/" in request.url.path:
            return httpx.Response(200, json={"resourceType": "Patient", "id": "synthetic-patient"})
        entries = []
        if request.url.path.endswith("/Condition"):
            entries = [{"resource": {"resourceType": "Condition", "code": {"text": "Example condition"}}}]
        if request.url.path.endswith("/Observation") and request.url.params.get("category") == "laboratory":
            entries = [{"resource": {
                "resourceType": "Observation", "code": {"text": "Example lab"},
                "effectiveDateTime": "2020-01-01T12:00:00Z",
                "valueQuantity": {"value": 10, "unit": "mg/dL"},
            }}]
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": entries})

    httpx_mock.add_callback(respond, is_reusable=True)
    monkeypatch.setattr(FHIRClient, "_throttle", lambda self: None)
    engine.sync_provider(provider, "me", config, full=True, dry_run=dry_run)

    out = tmp_health_sync_root / "output"
    raw = config.fhir_cache_dir("me", "hospital")
    assert json.loads((raw / "patient.json").read_text())["id"] == "synthetic-patient"
    if dry_run:
        assert not (out / "clinical_extract.md").exists()
        assert not (out / "labs/lab_results.md").exists()
    else:
        assert "Example condition" in (out / "clinical_extract.md").read_text()
        assert (out / "labs/lab_results.md").exists()
    assert not (out / "health_profile.md").exists()
