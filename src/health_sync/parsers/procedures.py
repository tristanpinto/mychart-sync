from __future__ import annotations

"""Parse FHIR Procedure resources into Procedures table rows."""

from typing import Any


def parse_procedures(resources: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Parse Procedure resources into output Procedures format.

    Target format:
        | Procedure | Date | Associated Diagnosis |

    Args:
        resources: List of FHIR Procedure resources.

    Returns:
        List of row dicts for the Procedures table.
    """
    rows = []
    for r in resources:
        if r.get("resourceType") != "Procedure":
            continue

        # Procedure name
        name = r.get("code", {}).get("text", "")
        if not name:
            codings = r.get("code", {}).get("coding", [])
            for c in codings:
                if c.get("display"):
                    name = c["display"]
                    break

        # Date
        date = r.get("performedDateTime", "")
        if not date:
            period = r.get("performedPeriod", {})
            date = period.get("start", "")

        # Associated diagnosis
        diag_parts = []
        for reason in r.get("reasonCode", []):
            text = reason.get("text", "")
            if not text:
                codings = reason.get("coding", [])
                for c in codings:
                    if c.get("display"):
                        text = c["display"]
                        break
            if text:
                diag_parts.append(text)

        # Also check reasonReference
        for ref in r.get("reasonReference", []):
            if ref.get("display"):
                diag_parts.append(ref["display"])

        rows.append({
            "Procedure": name,
            "Date": date,
            "Associated Diagnosis": "; ".join(diag_parts),
        })

    return rows


COLUMNS = ["Procedure", "Date", "Associated Diagnosis"]
KEY_COLS = ["Procedure", "Date"]
