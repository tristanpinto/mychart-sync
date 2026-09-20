from __future__ import annotations

from pathlib import Path

from health_sync.auth.token_store import TokenStore
from health_sync.sync import engine
from health_sync.sync.state import SyncState


def test_token_isolation(app_config, stored_token_factory) -> None:
    person_b_store = TokenStore(app_config.tokens_dir("person_b"))
    person_b_store.save(stored_token_factory(slug="ucsf", person="person_b"))

    assert (app_config.tokens_dir("person_b") / "ucsf_token.json").exists()
    assert not (app_config.tokens_dir("person_a") / "ucsf_token.json").exists()


def test_state_isolation(app_config) -> None:
    person_a_state = SyncState(app_config.sync_state_dir("person_a"))
    person_a_state.record_sync("mount-sinai", {"Patient": 1})

    assert (app_config.sync_state_dir("person_a") / "mount-sinai_state.json").exists()
    assert not (app_config.sync_state_dir("person_b") / "mount-sinai_state.json").exists()


def test_person_b_brain_paths_do_not_enter_person_a_health_tree(app_config) -> None:
    person_b_path = app_config.brain_path("clinical_extract", "person_b")

    assert "person_b" in str(person_b_path)
    assert "person_a" not in str(person_b_path)


def test_override_flips_token_and_brain_paths(
    monkeypatch,
    app_config,
    mock_fhir_client,
    stored_token_factory,
    fake_provider_factory,
) -> None:
    person_b_store = TokenStore(app_config.tokens_dir("person_b"))
    person_b_store.save(stored_token_factory(slug="mount-sinai", person="person_b"))

    written_paths: list[Path] = []

    def write_marker(path: Path, *args, **kwargs) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("written\n")
        written_paths.append(path)
        return path.read_text()

    monkeypatch.setattr(engine, "FHIRClient", mock_fhir_client)
    monkeypatch.setattr(engine, "write_generated", write_marker)
    monkeypatch.setattr(engine, "download_documents", lambda *args, **kwargs: 0)

    engine.sync_provider(
        fake_provider_factory(slug="mount-sinai", patient="person_a"),
        "person_b",
        app_config,
        override_patient=True,
        full=True,
    )

    assert written_paths
    assert all("person_b" in str(p) for p in written_paths)
    assert not any("person_a" in str(p) for p in written_paths)


def test_mismatched_person_without_override_does_no_io(app_config, fake_provider_factory) -> None:
    try:
        engine.sync_provider(
            fake_provider_factory(slug="mount-sinai", patient="person_a"),
            "person_b",
            app_config,
        )
    except engine.PersonGuardError:
        pass

    assert not app_config.tokens_dir("person_b").joinpath("mount-sinai_token.json").exists()
