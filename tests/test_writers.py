from __future__ import annotations

from datetime import date, timedelta

from health_sync.parsers.observations import parse_lab_observations
from health_sync.writers.clinical_extract import render_clinical_extract
from health_sync.writers.documents import download_documents
from health_sync.writers.lab_results import render_lab_results


def test_clinical_extract_includes_sections_and_source():
    text = render_clinical_extract(
        {"sutter": {"name": "Sutter Health", "resources": {
            "Condition": [
                {
                    "resourceType": "Condition",
                    "code": {"text": "Synthetic test condition"},
                    "onsetDateTime": "2020-01-01",
                    "clinicalStatus": {"text": "Active"},
                }
            ],
        }}},
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

    paths = list(tmp_path.glob("2020-01-01_surgical_report_*.txt"))
    assert count == 1
    assert len(paths) == 1
    assert "FINAL MICROSCOPIC DIAGNOSIS" in paths[0].read_text()


def test_lab_results_are_globally_sorted_by_date():
    text = render_lab_results(
        {
            "sutter": {"name": "Sutter Health", "resources": {
                "Observation_laboratory": [{
                    "resourceType": "Observation", "id": "lab-1",
                    "code": {"text": "Hemoglobin A1c"},
                    "effectiveDateTime": "2026-05-05", "valueQuantity": {"value": 6, "unit": "%"},
                }],
            }},
            "ucsf": {"name": "UCSF Health", "resources": {"Observation_laboratory": [
                {
                    "resourceType": "Observation",
                    "status": "final",
                    "code": {"text": "POCT Hemoglobin A1C"},
                    "effectiveDateTime": "2018-06-14T22:00:00Z",
                    "valueQuantity": {"value": 7, "unit": "%"},
                }
            ]}},
        },
    )

    assert text.index("## 2018-06-14") < text.index("## 2026-05-05")
    assert "UCSF Health" in text
    assert "Sutter Health" in text


def test_clinical_extract_splits_recent_and_historical_rows():
    recent_date = date.today().isoformat()
    historical_date = (date.today() - timedelta(days=RECENT_WINDOW_FOR_TESTS)).isoformat()

    text = render_clinical_extract(
        {"test": {"name": "Test Health", "resources": {
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
        }}},
    )

    assert text.index("### Active") < text.index("Recent active med")
    assert text.index("### Older Active Entries") < text.index("Older active med")
    assert text.index("### Recent") < text.index("Recent Clinic")
    assert text.index("### Historical") < text.index("Historical Clinic")
    assert text.index("### Recent") < text.index("172.7")
    assert text.index("### Historical") < text.index("170.0")


def test_clinical_extract_sorts_structured_tables():
    text = render_clinical_extract(
        {"test": {"name": "Test Health", "resources": {
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
        }}},
    )

    assert text.index("Newer vaccine") < text.index("Older vaccine")
    assert text.index("Newer procedure") < text.index("Older procedure")


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
