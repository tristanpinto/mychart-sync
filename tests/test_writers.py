from __future__ import annotations

from datetime import date, timedelta

from health_sync.parsers.observations import parse_lab_observations
from health_sync.writers.clinical_extract import update_clinical_extract
from health_sync.writers.documents import download_documents
from health_sync.writers.health_profile import update_health_profile
from health_sync.writers.lab_results import update_lab_results


def test_clinical_extract_placeholder_is_scaffolded_before_update(tmp_path):
    path = tmp_path / "clinical_extract.md"
    path.write_text("# Clinical Extract\n\nPlaceholder created before first auth.\n")

    text = update_clinical_extract(
        path,
        {
            "Condition": [
                {
                    "resourceType": "Condition",
                    "code": {"text": "Synthetic test condition"},
                    "onsetDateTime": "2020-01-01",
                    "clinicalStatus": {"text": "Active"},
                }
            ],
        },
        "Sutter Health",
    )

    assert "## Active Problems" in text
    assert "## Procedures" in text
    assert "Synthetic test condition" in text
    assert "Sutter Health" in text


def test_narrative_pathology_observation_is_not_rendered_as_lab_row():
    rows = parse_lab_observations([_narrative_pathology_observation()])

    assert rows == []


def test_narrative_pathology_observation_is_saved_as_record(tmp_path):
    count = download_documents(
        {"Observation_laboratory": [_narrative_pathology_observation()]},
        client=object(),
        records_dir=tmp_path,
    )

    path = tmp_path / "2020-01-01_surgical_report.txt"
    assert count == 1
    assert path.exists()
    assert "FINAL MICROSCOPIC DIAGNOSIS" in path.read_text()


def test_lab_results_are_globally_sorted_by_date(tmp_path):
    path = tmp_path / "lab_results.md"
    path.write_text(
        "# Lab Results\n\n"
        "Canonical labs.\n\n"
        "## 2026-05-05 (Sutter Health)\n\n"
        "**Source:** Sutter Health — FHIR sync\n\n"
        "| Test | Value | Unit | Ref Range | Flag |\n"
        "|---|---|---|---|---|\n"
        "| Hemoglobin A1c | 6.0 | % | 4.0–5.6 | **High** |\n\n"
        "---\n"
    )

    text = update_lab_results(
        path,
        {
            "Observation_laboratory": [
                {
                    "resourceType": "Observation",
                    "status": "final",
                    "code": {"text": "POCT Hemoglobin A1C"},
                    "effectiveDateTime": "2018-06-14T22:00:00Z",
                    "valueQuantity": {"value": 7, "unit": "%"},
                }
            ]
        },
        "UCSF Health",
    )

    assert text.index("## 2018-06-14 (UCSF Health)") < text.index(
        "## 2026-05-05 (Sutter Health)"
    )


def test_clinical_extract_splits_recent_and_historical_rows(tmp_path):
    path = tmp_path / "clinical_extract.md"
    recent_date = date.today().isoformat()
    historical_date = (date.today() - timedelta(days=RECENT_WINDOW_FOR_TESTS)).isoformat()

    text = update_clinical_extract(
        path,
        {
            "MedicationRequest": [
                {
                    "resourceType": "MedicationRequest",
                    "status": "active",
                    "authoredOn": recent_date,
                    "medicationCodeableConcept": {"text": "Recent active med"},
                },
                {
                    "resourceType": "MedicationRequest",
                    "status": "active",
                    "authoredOn": historical_date,
                    "medicationCodeableConcept": {"text": "Older active med"},
                },
            ],
            "Observation_vital-signs": [
                {
                    "resourceType": "Observation",
                    "code": {"text": "Height"},
                    "effectiveDateTime": recent_date,
                    "valueQuantity": {"value": 172.7, "unit": "cm"},
                },
                {
                    "resourceType": "Observation",
                    "code": {"text": "Height"},
                    "effectiveDateTime": historical_date,
                    "valueQuantity": {"value": 170.0, "unit": "cm"},
                },
            ],
            "Encounter": [
                {
                    "resourceType": "Encounter",
                    "class": {"code": "AMB"},
                    "period": {"start": recent_date},
                    "location": [{"location": {"display": "Recent Clinic"}}],
                },
                {
                    "resourceType": "Encounter",
                    "class": {"code": "AMB"},
                    "period": {"start": historical_date},
                    "location": [{"location": {"display": "Historical Clinic"}}],
                },
            ],
        },
        "Test Health",
    )

    assert text.index("### Active") < text.index("Recent active med")
    assert text.index("### Older Active Entries") < text.index("Older active med")
    assert text.index("### Recent") < text.index("Recent Clinic")
    assert text.index("### Historical") < text.index("Historical Clinic")
    assert text.index("### Recent") < text.index("172.7")
    assert text.index("### Historical") < text.index("170.0")


def test_clinical_extract_sorts_structured_tables(tmp_path):
    path = tmp_path / "clinical_extract.md"

    text = update_clinical_extract(
        path,
        {
            "Immunization": [
                {
                    "resourceType": "Immunization",
                    "vaccineCode": {"text": "Older vaccine"},
                    "occurrenceDateTime": "2020-01-01",
                },
                {
                    "resourceType": "Immunization",
                    "vaccineCode": {"text": "Newer vaccine"},
                    "occurrenceDateTime": "2025-01-01",
                },
            ],
            "Procedure": [
                {
                    "resourceType": "Procedure",
                    "code": {"text": "Older procedure"},
                    "performedDateTime": "2019-01-01",
                },
                {
                    "resourceType": "Procedure",
                    "code": {"text": "Newer procedure"},
                    "performedDateTime": "2026-01-01",
                },
            ],
        },
        "Test Health",
    )

    assert text.index("Newer vaccine") < text.index("Older vaccine")
    assert text.index("Newer procedure") < text.index("Older procedure")


def test_health_profile_writer_is_manual_only(tmp_path):
    path = tmp_path / "health_profile.md"
    original = "# Health Profile\n\nCurated summary.\n"
    path.write_text(original)

    result = update_health_profile(
        path,
        {
            "Immunization": [
                {
                    "resourceType": "Immunization",
                    "vaccineCode": {"text": "FHIR vaccine"},
                    "occurrenceDateTime": "2026-01-01",
                }
            ],
            "Observation_social-history": [
                {
                    "resourceType": "Observation",
                    "code": {"text": "Tobacco smoking status"},
                    "valueString": "Never",
                }
            ],
        },
    )

    assert result == original
    assert path.read_text() == original


def _narrative_pathology_observation():
    return {
        "resourceType": "Observation",
        "id": "obs-1",
        "status": "final",
        "category": [
            {
                "coding": [
                    {
                        "code": "laboratory",
                        "display": "Laboratory",
                    }
                ]
            },
            {
                "coding": [
                    {
                        "code": "LAB PATHOLOG",
                        "display": "LAB PATHOLOGY",
                    }
                ]
            },
        ],
        "code": {
            "coding": [
                {"code": "SURGREPORT"},
                {"display": "Surgical Report"},
            ],
            "text": "Surgical Report",
        },
        "basedOn": [{"display": "PATHOLOGY REPORT"}],
        "effectiveDateTime": "2020-01-01T08:00:00Z",
        "valueString": (
            "TISSUES:\n"
            "Synthetic specimen A\n"
            "FINAL MICROSCOPIC DIAGNOSIS:\n"
            "Synthetic narrative for parser testing.\n"
            "GROSS DESCRIPTION:\n"
            "No patient data."
        ),
    }


RECENT_WINDOW_FOR_TESTS = 800
