from __future__ import annotations

"""Render clinical Markdown from retained records, never from previous Markdown."""

from collections import defaultdict
from datetime import date, timedelta

from health_sync.parsers.allergies import parse_allergies
from health_sync.parsers.care_team import COLUMNS as CT_COLS, parse_care_team
from health_sync.parsers.conditions import COLUMNS as COND_COLS, parse_conditions
from health_sync.parsers.encounters import COLUMNS as ENC_COLS, parse_encounters
from health_sync.parsers.immunizations import COLUMNS as IMM_COLS, parse_immunizations
from health_sync.parsers.medications import (
    ACTIVE_COLUMNS,
    ENDED_COLUMNS,
    HOSPITAL_COLUMNS,
    parse_medications,
)
from health_sync.parsers.observations import (
    SOCIAL_COLUMNS,
    VITAL_COLUMNS,
    parse_social_history,
    parse_vital_observations,
)
from health_sync.parsers.procedures import COLUMNS as PROC_COLS, parse_procedures
from health_sync.sync.records import Sources, observations
from health_sync.writers.markdown import render_table

RECENT_WINDOW_DAYS = 730
PROVENANCE = ["Source", "Record"]


def sourced_rows(parser, resources: list[dict], source: str) -> list[dict]:
    rows = []
    for resource in resources:
        for row in parser([resource]):
            rows.append(
                {
                    **row,
                    "Source": source,
                    "Record": f"{resource['resourceType']}/{resource.get('id', 'unknown')}",
                }
            )
    return rows


def _row_date(row: dict, columns: tuple[str, ...]) -> date | None:
    for column in columns:
        try:
            # FHIR dates, datetimes, and encounter ranges start with an ISO date.
            return date.fromisoformat(row.get(column, "")[:10])
        except ValueError:
            continue
    return None


def _sort(rows: list[dict], columns: tuple[str, ...]) -> list[dict]:
    return sorted(
        rows, key=lambda row: _row_date(row, columns) or date.min, reverse=True
    )


def _split(rows: list[dict], columns: tuple[str, ...], unknown_recent=False):
    recent, historical = [], []
    threshold = date.today() - timedelta(days=RECENT_WINDOW_DAYS)
    for row in _sort(rows, columns):
        day = _row_date(row, columns)
        is_recent = day >= threshold if day else unknown_recent
        if is_recent:
            recent.append(row)
        else:
            historical.append(row)
    return recent, historical


def render_clinical_extract(sources: Sources) -> str:
    tables: dict[str, list[dict]] = defaultdict(list)
    hospital: dict[str, list[dict]] = defaultdict(list)
    for slug, source in sorted(sources.items()):
        name = f"{source['name']} ({slug})" if source["name"] != slug else slug
        data = source["resources"]
        for key, kind, parser in (
            ("Active Problems", "Condition", parse_conditions),
            ("Immunizations", "Immunization", parse_immunizations),
            ("Encounters", "Encounter", parse_encounters),
            ("Procedures", "Procedure", parse_procedures),
            ("Care Team", "CareTeam", parse_care_team),
        ):
            tables[key].extend(sourced_rows(parser, data.get(kind, []), name))
        hints = source.get("observation_categories", {})
        tables["Social History"].extend(
            sourced_rows(
                parse_social_history, observations(data, "social-history", hints), name
            )
        )
        tables["Vitals"].extend(
            sourced_rows(
                parse_vital_observations, observations(data, "vital-signs", hints), name
            )
        )
        for resource in data.get("MedicationRequest", []):
            active, ended, stays = parse_medications(
                [resource], encounters=data.get("Encounter", [])
            )
            provenance = {
                "Source": name,
                "Record": f"MedicationRequest/{resource.get('id', 'unknown')}",
            }
            tables["Active"].extend({**row, **provenance} for row in active)
            tables["Ended"].extend({**row, **provenance} for row in ended)
            for stay, rows in stays.items():
                hospital[stay].extend(
                    {**row, "Status": resource.get("status", ""), **provenance}
                    for row in rows
                )
        for resource in data.get("AllergyIntolerance", []):
            allergy = parse_allergies([resource])[0]
            status = allergy["status"] or next(
                (
                    c.get("code", "")
                    for c in resource.get("clinicalStatus", {}).get("coding", [])
                ),
                "",
            )
            tables["Allergies"].append(
                {
                    "Allergy": allergy["name"],
                    "Status": status or "Unknown",
                    "Verification": resource.get("verificationStatus", {}).get("text")
                    or next(
                        (
                            c.get("code", "")
                            for c in resource.get("verificationStatus", {}).get(
                                "coding", []
                            )
                        ),
                        "",
                    ),
                    "Reactions": ", ".join(allergy["reactions"]),
                    "Onset": allergy["onset"],
                    "Source": name,
                    "Record": f"AllergyIntolerance/{resource.get('id', 'unknown')}",
                }
            )

    parts = [
        "# Clinical Extract",
        "Generated from retained FHIR JSON. Keep personal notes in health_profile.md, not this file.",
        "Records reflect hospital reports, not a reconciled current medication or allergy list. Source and Record identify each entry.",
        "## Medications",
        "Active means the order's FHIR status. Older active entries may no longer be used. Recent means the last two years.",
    ]

    def table(heading, columns, rows, level=2):
        parts.extend(
            [f"{'#' * level} {heading}", render_table(columns + PROVENANCE, rows)]
        )

    recent, old = _split(tables["Active"], ("Started",), unknown_recent=True)
    table("Active", ACTIVE_COLUMNS, recent, 3)
    table("Older Active Entries", ACTIVE_COLUMNS, old, 3)
    recent, old = _split(tables["Ended"], ("End Date", "Started"))
    table("Ended", ENDED_COLUMNS, recent, 3)
    table("Historical Ended", ENDED_COLUMNS, old, 3)
    for stay, rows in sorted(hospital.items(), reverse=True):
        table(
            f"Administered in Hospital ({stay})", HOSPITAL_COLUMNS + ["Status"], rows, 3
        )
    table("Active Problems", COND_COLS, _sort(tables["Active Problems"], ("Onset",)))
    table(
        "Allergies",
        ["Allergy", "Status", "Verification", "Reactions", "Onset"],
        tables["Allergies"],
    )
    if not tables["Allergies"]:
        parts.append(
            "No allergy records downloaded. This does not establish that there are no allergies."
        )
    table("Immunizations", IMM_COLS, _sort(tables["Immunizations"], ("Date",)))
    table("Social History", SOCIAL_COLUMNS, tables["Social History"])
    for heading, columns in (("Vitals", VITAL_COLUMNS), ("Encounters", ENC_COLS)):
        parts.extend(
            [
                f"## {heading}",
                "Recent means the last two years; unknown dates are listed under Historical.",
            ]
        )
        recent, old = _split(tables[heading], ("Date",))
        table("Recent", columns, recent, 3)
        table("Historical", columns, old, 3)
    table("Procedures", PROC_COLS, _sort(tables["Procedures"], ("Date",)))
    table("Care Team", CT_COLS, tables["Care Team"])
    return "\n\n".join(parts) + "\n"
