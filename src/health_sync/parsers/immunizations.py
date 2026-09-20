from __future__ import annotations

"""Parse FHIR Immunization resources into Immunizations table rows."""

from typing import Any


def parse_immunizations(resources: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Parse Immunization resources into output Immunizations format.

    Target format:
        | Vaccine | Date | Notes |

    Args:
        resources: List of FHIR Immunization resources.

    Returns:
        List of row dicts for the Immunizations table.
    """
    rows = []
    for r in resources:
        if r.get("resourceType") != "Immunization":
            continue

        # Vaccine name
        vaccine = r.get("vaccineCode", {}).get("text", "")
        if not vaccine:
            codings = r.get("vaccineCode", {}).get("coding", [])
            for c in codings:
                if c.get("display"):
                    vaccine = c["display"]
                    break

        # Date
        date = r.get("occurrenceDateTime", "")

        # Notes — combine lot number, dose, manufacturer, site
        notes_parts = []
        if r.get("manufacturer", {}).get("display"):
            notes_parts.append(r["manufacturer"]["display"])
        if r.get("lotNumber"):
            notes_parts.append(f"Lot {r['lotNumber']}")
        dose_qty = r.get("doseQuantity", {})
        if dose_qty.get("value"):
            notes_parts.append(f"{dose_qty['value']} {dose_qty.get('unit', '')}".strip())
        if r.get("site", {}).get("text"):
            notes_parts.append(r["site"]["text"])
        for note in r.get("note", []):
            if note.get("text"):
                notes_parts.append(note["text"])

        rows.append({
            "Vaccine": vaccine,
            "Date": date,
            "Notes": ", ".join(notes_parts) if notes_parts else "",
        })

    return rows


COLUMNS = ["Vaccine", "Date", "Notes"]
