from __future__ import annotations

import copy
import json

import pytest

from health_sync.auth.token_store import TokenStore
from health_sync.sync import engine
from health_sync.sync.records import merge_records, records_lock
from health_sync.sync.state import SyncState
from health_sync.writers.clinical_extract import render_clinical_extract
from health_sync.writers.generated import write_generated


def medication(identifier="med-1", dose=5, status="active"):
    return {
        "resourceType": "MedicationRequest",
        "id": identifier,
        "status": status,
        "medicationCodeableConcept": {"text": "Example medicine"},
        "authoredOn": "2026-01-01",
        "dosageInstruction": [
            {"doseAndRate": [{"doseQuantity": {"value": dose, "unit": "mg"}}]}
        ],
    }


def allergy(identifier, name, status="active"):
    return {
        "resourceType": "AllergyIntolerance",
        "id": identifier,
        "code": {"text": name},
        "clinicalStatus": {"coding": [{"code": status}]},
    }


def lab(identifier="lab-1", value=10, category=True):
    result = {
        "resourceType": "Observation",
        "id": identifier,
        "status": "final",
        "code": {"text": "Example lab"},
        "effectiveDateTime": "2026-01-01T12:00:00Z",
        "valueQuantity": {"value": value, "unit": "mg/dL"},
    }
    if category:
        result["category"] = [{"coding": [{"code": "laboratory"}]}]
    return result


@pytest.fixture
def run_sync(monkeypatch, app_config, stored_token_factory, fake_provider_factory):
    class Client:
        incoming = {}
        error = None
        windows = []

        def __init__(self, **kwargs):
            self.base_url = kwargs["base_url"]
            self.patient_id = kwargs["patient_id"]
            assert kwargs["cache_dir"] is None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def fetch_all(self, since=None):
            self.windows.append(since)
            if self.error:
                raise self.error
            return {
                "Patient": [{"resourceType": "Patient", "id": self.patient_id}],
                **copy.deepcopy(self.incoming),
            }

    monkeypatch.setattr(engine, "FHIRClient", Client)
    monkeypatch.setattr(engine, "download_documents", lambda *args: 0)

    def run(data, slug="hospital-a", person="person_a", **kwargs):
        TokenStore(app_config.tokens_dir(person)).save(
            stored_token_factory(slug=slug, person=person)
        )
        Client.incoming = data
        return engine.sync_provider(
            fake_provider_factory(slug=slug, patient=person, name=slug.title()),
            person,
            app_config,
            **kwargs,
        )

    run.client = Client
    run.config = app_config
    run.path = lambda key="clinical_extract", person="person_a": app_config.output_path(
        key, person
    )
    run.raw = lambda slug="hospital-a": (
        app_config.fhir_cache_dir("person_a", slug) / "records.json"
    )
    run.state = lambda slug="hospital-a": SyncState(
        app_config.sync_state_dir("person_a")
    ).load(slug)
    return run


def test_same_id_updates_dose_and_status_distinct_ids_survive(run_sync):
    run_sync({"MedicationRequest": [medication(), medication("med-2", 15)]})
    run_sync({"MedicationRequest": [medication(dose=10, status="stopped")]})
    text = run_sync.path().read_text()
    assert text.count("Example medicine") == 2
    assert "| 10 mg |" in text and "| 15 mg |" in text
    assert "| 5 mg |" not in text
    assert "Stopped" in text
    assert (
        text.index("MedicationRequest/med-2")
        < text.index("### Ended")
        < text.index("MedicationRequest/med-1")
    )


def test_same_ids_and_conflicting_allergies_stay_provider_scoped(run_sync):
    run_sync(
        {
            "MedicationRequest": [medication()],
            "AllergyIntolerance": [allergy("a-1", "Penicillin")],
        }
    )
    run_sync(
        {
            "MedicationRequest": [medication(dose=20)],
            "AllergyIntolerance": [allergy("a-1", "No known allergies")],
        },
        slug="hospital-b",
    )
    run_sync({"AllergyIntolerance": [allergy("a-1", "Penicillin", "resolved")]})
    text = run_sync.path().read_text()
    assert text.count("MedicationRequest/med-1") == 2
    assert "Penicillin" in text and "No known allergies" in text and "resolved" in text
    assert "hospital-a" in text and "hospital-b" in text


@pytest.mark.parametrize("full", [False, True])
def test_empty_fetch_retains_records_even_for_full_refresh(run_sync, full):
    run_sync({"MedicationRequest": [medication()]})
    original = run_sync.path().read_bytes()
    run_sync({"MedicationRequest": []}, full=full)
    assert run_sync.path().read_bytes() == original
    assert json.loads(run_sync.raw().read_text())["resources"]["MedicationRequest"] == [
        medication()
    ]
    assert (run_sync.client.windows[-1] is None) == full


def test_repeated_sync_is_idempotent_and_recovers_missing_summary(run_sync):
    run_sync({"MedicationRequest": [medication()]})
    original = run_sync.path().read_bytes()
    modified = run_sync.path().stat().st_mtime_ns
    run_sync({"MedicationRequest": [medication()]})
    assert run_sync.path().read_bytes() == original
    assert run_sync.path().stat().st_mtime_ns == modified
    assert not list(run_sync.path().parent.glob("*.backup-*"))
    run_sync.path().unlink()
    run_sync({})
    assert run_sync.path().read_bytes() == original


@pytest.mark.parametrize("first_run", [False, True])
def test_dry_run_does_not_change_summaries_or_state_and_real_sync_catches_up(
    run_sync, first_run
):
    if not first_run:
        run_sync({"MedicationRequest": [medication()]})
    before = run_sync.path().read_bytes() if run_sync.path().exists() else None
    state = run_sync.state()
    run_sync({"MedicationRequest": [medication(dose=25)]}, dry_run=True)
    assert (
        run_sync.path().read_bytes() if run_sync.path().exists() else None
    ) == before
    assert run_sync.state() == state
    assert not list(run_sync.path().parent.glob("*.backup-*"))
    run_sync({})
    assert "25 mg" in run_sync.path().read_text()
    assert (run_sync.client.windows[-1] is None) == first_run


@pytest.mark.parametrize(
    "bad",
    [
        {"MedicationRequest": [{"resourceType": "MedicationRequest"}]},
        {"MedicationRequest": [None]},
        {"MedicationRequest": [{"resourceType": "Condition", "id": "c-1"}]},
        {"MedicationRequest": [dict(medication(), dosageInstruction="invalid")]},
        {
            "MedicationRequest": [
                {"resourceType": "OperationOutcome", "issue": [{"severity": "error"}]}
            ]
        },
        {"Patient": []},
        {"Patient": [{"resourceType": "Patient", "id": "different-patient"}]},
    ],
)
def test_malformed_fetch_preserves_raw_markdown_and_checkpoint(run_sync, bad):
    run_sync({"MedicationRequest": [medication()]})
    raw, text, state = (
        run_sync.raw().read_bytes(),
        run_sync.path().read_bytes(),
        run_sync.state(),
    )
    with pytest.raises((ValueError, AttributeError, TypeError)):
        run_sync(bad)
    assert run_sync.raw().read_bytes() == raw
    assert run_sync.path().read_bytes() == text
    assert run_sync.state() == state


def test_failed_fetch_preserves_everything(run_sync):
    run_sync({"MedicationRequest": [medication()]})
    raw, text, state = (
        run_sync.raw().read_bytes(),
        run_sync.path().read_bytes(),
        run_sync.state(),
    )
    run_sync.client.error = RuntimeError("A later resource request failed")
    with pytest.raises(RuntimeError, match="later resource"):
        run_sync({})
    assert (
        run_sync.raw().read_bytes(),
        run_sync.path().read_bytes(),
        run_sync.state(),
    ) == (raw, text, state)


def test_document_failure_retains_new_records_but_not_summary_or_checkpoint(
    run_sync, monkeypatch
):
    run_sync({"MedicationRequest": [medication()]})
    text, state = run_sync.path().read_bytes(), run_sync.state()

    def fail(*args):
        raise RuntimeError("Document download incomplete")

    monkeypatch.setattr(engine, "download_documents", fail)
    with pytest.raises(RuntimeError, match="incomplete"):
        run_sync({"MedicationRequest": [medication(dose=10)]})
    assert run_sync.path().read_bytes() == text
    assert run_sync.state() == state
    assert json.loads(run_sync.raw().read_text())["resources"]["MedicationRequest"][
        0
    ] == medication(dose=10)
    monkeypatch.setattr(engine, "download_documents", lambda *args: 0)
    run_sync({})
    assert "10 mg" in run_sync.path().read_text()


def test_migration_preserves_snapshot_records_and_manual_summaries(run_sync):
    raw_dir = run_sync.raw().parent
    raw_dir.mkdir(parents=True)
    (raw_dir / "medicationrequest.json").write_text(json.dumps([medication()]))
    (raw_dir / "observation_laboratory.json").write_text(
        json.dumps([lab(category=False)])
    )
    other = raw_dir.parent / "hospital-b"
    other.mkdir()
    (other / "allergyintolerance.json").write_text(
        json.dumps([allergy("allergy-1", "Peanuts")])
    )
    run_sync.path().write_text(
        "# Clinical Extract\n\nHandwritten note and old unavailable records.\n"
    )
    run_sync.path("lab_results").write_text("# Labs\n\nHandwritten lab note.\n")
    profile = run_sync.path("health_profile")
    profile.write_text("# My personal notes\n")
    SyncState(run_sync.config.sync_state_dir("person_a")).record_sync(
        "hospital-a", {}, synced_at="2026-01-01T00:00:00Z"
    )
    run_sync({})
    assert run_sync.client.windows[-1] is None
    assert (
        "Example medicine" in run_sync.path().read_text()
        and "Peanuts" in run_sync.path().read_text()
    )
    assert "Example lab" in run_sync.path("lab_results").read_text()
    assert (
        "Handwritten note"
        in next(run_sync.path().parent.glob("clinical_extract.md.backup-*")).read_text()
    )
    assert (
        "Handwritten lab note"
        in next(
            run_sync.path("lab_results").parent.glob("lab_results.md.backup-*")
        ).read_text()
    )
    assert profile.read_text() == "# My personal notes\n"
    assert (raw_dir / "medicationrequest.json").exists()
    assert (other / "records.json").exists()


def test_lab_corrections_same_test_ids_and_hospital_ids_remain_separate(run_sync):
    run_sync({"Observation_laboratory": [lab(), lab("lab-2", 20, category=False)]})
    run_sync({"Observation_laboratory": [lab(value=30)]}, slug="hospital-b")
    run_sync({"Observation_laboratory": [dict(lab(value=15), status="corrected")]})
    text = run_sync.path("lab_results").read_text()
    assert text.count("Example lab") == 3
    assert "| 15 |" in text and "| 20 |" in text and "| 30 |" in text
    assert "| 10 |" not in text
    assert "corrected" in text


def test_older_versions_cannot_replace_newer_records():
    current = dict(medication(dose=10), meta={"lastUpdated": "2026-01-02T00:00:00Z"})
    older = dict(medication(), meta={"lastUpdated": "2026-01-01T00:00:00Z"})
    assert merge_records(
        {"MedicationRequest": [current]}, {"MedicationRequest": [older]}
    )["MedicationRequest"] == [current]


def test_category_changes_remove_old_summary_membership(run_sync):
    run_sync({"Observation_laboratory": [lab()]})
    vital = dict(lab(), category=[{"coding": [{"code": "vital-signs"}]}])
    run_sync({"Observation_vital-signs": [vital]})
    assert "Example lab" not in run_sync.path("lab_results").read_text()
    assert "Observation/lab-1" in run_sync.path().read_text()


def test_rejected_older_observation_cannot_change_search_category(run_sync):
    current = dict(lab(category=False), meta={"lastUpdated": "2026-01-02T00:00:00Z"})
    older = dict(lab(category=False), meta={"lastUpdated": "2026-01-01T00:00:00Z"})
    run_sync({"Observation_vital-signs": [current]})
    run_sync({"Observation_laboratory": [older]})
    assert "Observation/lab-1" in run_sync.path().read_text()
    assert "Observation/lab-1" not in run_sync.path("lab_results").read_text()


def test_corrupt_existing_store_is_not_silently_replaced(run_sync):
    run_sync({"MedicationRequest": [medication()]})
    run_sync.raw().write_text("invalid json")
    text, state = run_sync.path().read_bytes(), run_sync.state()
    with pytest.raises(ValueError):
        run_sync({})
    assert run_sync.raw().read_text() == "invalid json"
    assert run_sync.path().read_bytes() == text and run_sync.state() == state


def test_manual_generated_edits_are_backed_up_once(tmp_path):
    path = tmp_path / "clinical.md"
    write_generated(path, "# Generated\n")
    edited = path.read_text() + "My annotation\n"
    path.write_text(edited)
    write_generated(path, "# Next generation\n")
    backups = list(tmp_path.glob("*.backup-*"))
    assert len(backups) == 1 and backups[0].read_text() == edited
    write_generated(path, "# Final generation\n")
    assert list(tmp_path.glob("*.backup-*")) == backups


def test_same_person_sync_lock_prevents_overlapping_writes(tmp_path):
    with records_lock(tmp_path):
        with pytest.raises(RuntimeError, match="Another sync"):
            with records_lock(tmp_path):
                pass
    with records_lock(tmp_path):
        pass


def test_sync_lock_is_checked_before_refreshing_tokens(run_sync, monkeypatch):
    def unexpected(*args):
        raise AssertionError(
            "Should not refresh a token while another sync holds the lock"
        )

    monkeypatch.setattr(TokenStore, "get_valid_token", unexpected)
    with records_lock(run_sync.raw().parent.parent):
        with pytest.raises(RuntimeError, match="Another sync"):
            run_sync({})


def test_profile_path_collision_fails_before_fetch(run_sync):
    run_sync.config.person("person_a").paths.clinical_extract = run_sync.config.person(
        "person_a"
    ).paths.health_profile
    with pytest.raises(ValueError, match="different files"):
        run_sync({})
    assert run_sync.client.windows == []


def test_shared_raw_directory_cannot_mix_people(run_sync, monkeypatch):
    run_sync({"MedicationRequest": [medication()]})
    original = run_sync.path().read_bytes()
    raw_root = run_sync.raw().parent.parent
    monkeypatch.setattr(
        type(run_sync.config),
        "fhir_cache_dir",
        lambda self, person, slug: raw_root / slug,
    )
    with pytest.raises(ValueError, match="another person"):
        run_sync({}, slug="hospital-b", person="person_b")
    assert run_sync.path().read_bytes() == original
    assert not run_sync.path(person="person_b").exists()


def test_no_allergy_records_does_not_claim_no_allergies():
    assert "No known active allergies" not in render_clinical_extract({})
    assert "does not establish" in render_clinical_extract({})


def test_private_atomic_write_failure_keeps_old_records(run_sync, monkeypatch):
    from health_sync.auth import storage

    run_sync({"MedicationRequest": [medication()]})
    before = run_sync.raw().read_bytes()

    def fail(*args):
        raise OSError("Disk unavailable")

    monkeypatch.setattr(storage.os, "replace", fail)
    # Save the already-valid token before injecting the failure into the sync.
    provider = type(
        "Provider",
        (),
        {"slug": "hospital-a", "name": "Hospital A", "patient": "person_a"},
    )()
    run_sync.client.incoming = {"MedicationRequest": [medication(dose=20)]}
    with pytest.raises(OSError, match="Disk unavailable"):
        engine.sync_provider(provider, "person_a", run_sync.config)
    assert run_sync.raw().read_bytes() == before
    assert not list(run_sync.raw().parent.glob(".records.json.*"))
