from __future__ import annotations

"""Parse FHIR Encounter resources into Encounters table rows."""

from typing import Any

# Map FHIR encounter class codes to readable types
CLASS_MAP = {
    "IMP": "Hospital Encounter",
    "AMB": "Office Visit",
    "EMER": "ED Visit",
    "VR": "Telephone",
    "HH": "Home Health",
    "OBSENC": "Observation",
    "SS": "Short Stay",
}


def parse_encounters(resources: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Parse Encounter resources into output Encounters format.

    Target format:
        | Date | Type | Department | Care Team | Provider Notes | Other Notes |

    Args:
        resources: List of FHIR Encounter resources.

    Returns:
        List of row dicts for the Encounters table.
    """
    rows = []
    for r in resources:
        if r.get("resourceType") != "Encounter":
            continue

        # Date — period start/end
        period = r.get("period", {})
        start = period.get("start", "")
        end = period.get("end", "")
        if start and end and start != end:
            date = f"{start} - {end}"
        else:
            date = start

        # Type
        enc_class = r.get("class", {})
        class_code = enc_class.get("code", "")
        enc_type = CLASS_MAP.get(class_code, class_code)
        # Override with type text if available
        type_list = r.get("type", [])
        if type_list:
            type_text = type_list[0].get("text", "")
            if type_text:
                enc_type = type_text

        # Department / location
        department = ""
        locations = r.get("location", [])
        if locations:
            department = locations[0].get("location", {}).get("display", "")

        # Care Team — participants (deduplicated, preserving order)
        care_team_parts = []
        seen_names = set()
        for participant in r.get("participant", []):
            name = participant.get("individual", {}).get("display", "")
            if name and name not in seen_names:
                care_team_parts.append(name)
                seen_names.add(name)
        care_team = ", ".join(care_team_parts)

        # Provider notes — encounter reason/summary from FHIR
        notes_parts = []
        for reason in r.get("reasonCode", []):
            text = reason.get("text", "")
            if not text:
                codings = reason.get("coding", [])
                for c in codings:
                    if c.get("display"):
                        text = c["display"]
                        break
            if text:
                notes_parts.append(text)
        provider_notes = "; ".join(notes_parts)

        rows.append({
            "Date": date,
            "Type": enc_type,
            "Department": department,
            "Care Team": care_team,
            "Provider Notes": provider_notes,
            "Other Notes": "",
        })

    return rows


COLUMNS = ["Date", "Type", "Department", "Care Team", "Provider Notes", "Other Notes"]
