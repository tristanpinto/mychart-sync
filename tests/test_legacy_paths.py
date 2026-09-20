from __future__ import annotations

from pathlib import Path

from health_sync.auth.token_store import TokenStore
from health_sync.sync import engine


def test_legacy_person_paths_stay_stable(
    monkeypatch,
    app_config,
    providers_file,
    person_a_legacy_state,
    mock_fhir_client,
    fake_provider_factory,
    stored_token_factory,
) -> None:
    root = person_a_legacy_state
    expected_paths = {
        root
        / "brain"
        / "person_a"
        / "records"
        / "clinical_extract.md",
        root
        / "brain"
        / "person_a"
        / "records"
        / "labs"
        / "lab_results.md",
    }
    written_paths: list[Path] = []

    def write_marker(path: Path, *args, **kwargs) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("person_a\n")
        written_paths.append(path)
        return path.read_text()

    TokenStore(app_config.tokens_dir("person_a")).save(stored_token_factory())

    monkeypatch.setattr(engine, "FHIRClient", mock_fhir_client)
    monkeypatch.setattr(engine, "write_generated", write_marker)
    monkeypatch.setattr(engine, "download_documents", lambda *args, **kwargs: 0)

    engine.sync_provider(
        fake_provider_factory(slug="mount-sinai", patient="person_a"),
        "person_a",
        app_config,
        full=True,
    )

    assert set(written_paths) == expected_paths
    assert all("person_a" in str(path) for path in written_paths)
    assert not (
        root / "brain" / "person_b" / "clinical_extract.md"
    ).exists()
