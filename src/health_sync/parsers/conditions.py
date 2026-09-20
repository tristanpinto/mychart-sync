from __future__ import annotations

"""Parse FHIR Condition resources into Active Problems table rows.

Deduplicates across encounter diagnoses — the same condition often appears
multiple times (once per encounter). Preserves all unique ICD codes.
"""

from typing import Any


def parse_conditions(resources: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Parse Condition resources into output Active Problems format.

    Target format:
        | Diagnosis | ICD Code | Onset | Status |

    Deduplicates by diagnosis text. When the same diagnosis appears
    with different ICD codes (e.g., ICD-9 823.00 and ICD-10 S82.131A),
    all unique codes are preserved in the ICD Code column.

    Args:
        resources: List of FHIR Condition resources.

    Returns:
        List of row dicts for the Active Problems table (deduplicated).
    """
    # Collect all data per unique diagnosis text
    seen: dict[str, dict[str, Any]] = {}  # diagnosis_lower -> merged data

    for r in resources:
        if r.get("resourceType") != "Condition":
            continue

        # Diagnosis text
        diagnosis = r.get("code", {}).get("text", "")
        if not diagnosis:
            codings = r.get("code", {}).get("coding", [])
            for c in codings:
                if c.get("display"):
                    diagnosis = c["display"]
                    break

        if not diagnosis:
            continue

        # Collect ALL ICD and SNOMED codes
        codes = set()
        for coding in r.get("code", {}).get("coding", []):
            system = coding.get("system", "")
            code = coding.get("code", "")
            if not code:
                continue
            if "icd-10" in system.lower():
                codes.add(f"ICD-10: {code}")
            elif "icd-9" in system.lower():
                codes.add(f"ICD-9: {code}")
            # Skip SNOMED — too many codes, not useful in the table

        # Onset
        onset = r.get("onsetDateTime", "")
        if not onset:
            onset_period = r.get("onsetPeriod", {})
            onset = onset_period.get("start", "")

        # Clinical status
        status = r.get("clinicalStatus", {}).get("text", "")
        if not status:
            status_codings = r.get("clinicalStatus", {}).get("coding", [])
            if status_codings:
                status = status_codings[0].get("display", status_codings[0].get("code", ""))

        # Merge into existing entry or create new
        key = diagnosis.lower()
        if key in seen:
            # Merge codes
            seen[key]["codes"].update(codes)
            # Prefer onset if we didn't have one
            if not seen[key]["onset"] and onset:
                seen[key]["onset"] = onset
            # Prefer active status
            if status.lower() == "active":
                seen[key]["status"] = status
        else:
            seen[key] = {
                "diagnosis": diagnosis,
                "codes": codes,
                "onset": onset,
                "status": status,
            }

    # Convert to row dicts
    rows = []
    for data in seen.values():
        # Sort codes: ICD-10 first, then ICD-9
        sorted_codes = sorted(data["codes"], key=lambda c: (
            0 if c.startswith("ICD-10") else 1
        ))
        rows.append({
            "Diagnosis": data["diagnosis"],
            "ICD Code": ", ".join(sorted_codes) if sorted_codes else "",
            "Onset": data["onset"],
            "Status": data["status"],
        })

    return rows


COLUMNS = ["Diagnosis", "ICD Code", "Onset", "Status"]
