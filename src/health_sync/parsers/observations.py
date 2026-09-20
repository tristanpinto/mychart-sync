from __future__ import annotations

"""Parse FHIR Observation resources into Labs, Vitals, and Social History."""

from typing import Any


def _coding_labels(concept: dict[str, Any]) -> list[str]:
    labels = []
    if concept.get("text"):
        labels.append(str(concept["text"]))
    for coding in concept.get("coding", []):
        for key in ("code", "display"):
            if coding.get(key):
                labels.append(str(coding[key]))
    return labels


def _is_narrative_lab_report(resource: dict[str, Any]) -> bool:
    """Return True for report-like lab Observations that are not discrete results."""
    value = resource.get("valueString")
    if not isinstance(value, str):
        return False

    normalized_value = value.lower()
    multiline_or_long = "\n" in value or "\r" in value or len(value) > 300
    if not multiline_or_long:
        return False

    labels = []
    labels.extend(_coding_labels(resource.get("code", {})))
    for category in resource.get("category", []):
        labels.extend(_coding_labels(category))
    for based_on in resource.get("basedOn", []):
        if based_on.get("display"):
            labels.append(str(based_on["display"]))

    label_text = " ".join(labels).lower()
    report_label_terms = (
        "surgical report",
        "pathology report",
        "lab pathology",
        "surgreport",
    )
    report_body_markers = (
        "final microscopic diagnosis",
        "gross description",
        "tissues:",
        "clinical history:",
    )

    return any(term in label_text for term in report_label_terms) or any(
        marker in normalized_value for marker in report_body_markers
    )


def parse_lab_observations(resources: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Parse laboratory Observation resources into lab result rows.

    Target format:
        | Test | Value | Unit | Ref Range | Flag |

    Observations are grouped by effectiveDateTime (draw date) by the writer.

    Args:
        resources: List of FHIR Observation resources (category=laboratory).

    Returns:
        List of lab result dicts with an extra 'draw_date' field for grouping.
    """
    rows = []
    for r in resources:
        if r.get("resourceType") != "Observation":
            continue
        if _is_narrative_lab_report(r):
            continue

        # Test name
        test_name = r.get("code", {}).get("text", "")
        if not test_name:
            codings = r.get("code", {}).get("coding", [])
            for c in codings:
                if c.get("display"):
                    test_name = c["display"]
                    break

        # Value
        value = ""
        unit = ""
        if "valueQuantity" in r:
            vq = r["valueQuantity"]
            value = str(vq.get("value", ""))
            unit = vq.get("unit", "")
        elif "valueCodeableConcept" in r:
            value = r["valueCodeableConcept"].get("text", "")
        elif "valueString" in r:
            value = r["valueString"]

        # Reference range
        ref_range = ""
        ranges = r.get("referenceRange", [])
        if ranges:
            rr = ranges[0]
            low = rr.get("low", {}).get("value")
            high = rr.get("high", {}).get("value")
            if low is not None and high is not None:
                ref_range = f"{low}–{high}"
            elif rr.get("text"):
                ref_range = rr["text"]

        # Flag (interpretation)
        flag = ""
        interpretations = r.get("interpretation", [])
        if interpretations:
            for interp in interpretations:
                codings = interp.get("coding", [])
                for c in codings:
                    code = c.get("code", "")
                    if code in ("H", "HH"):
                        flag = "**High**"
                    elif code in ("L", "LL"):
                        flag = "**Low**"
                    elif code == "A":
                        flag = "**Abnormal**"
                    elif c.get("display"):
                        flag = c["display"]

        # Draw date for grouping
        draw_date = r.get("effectiveDateTime", "")
        if not draw_date:
            draw_date = r.get("effectivePeriod", {}).get("start", "")

        # Panel name from DiagnosticReport (if available, handled by writer)
        rows.append({
            "Test": test_name,
            "Value": value,
            "Unit": unit,
            "Ref Range": ref_range,
            "Flag": flag,
            "draw_date": draw_date,
        })

    return rows


def parse_social_history(resources: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Parse social-history Observation resources.

    Target format:
        | Category | Value |

    Args:
        resources: List of FHIR Observation resources (category=social-history).

    Returns:
        List of row dicts for the Social History table.
    """
    rows = []
    for r in resources:
        if r.get("resourceType") != "Observation":
            continue

        category_name = r.get("code", {}).get("text", "")
        if not category_name:
            codings = r.get("code", {}).get("coding", [])
            for c in codings:
                if c.get("display"):
                    category_name = c["display"]
                    break

        value = ""
        if "valueCodeableConcept" in r:
            value = r["valueCodeableConcept"].get("text", "")
            if not value:
                codings = r["valueCodeableConcept"].get("coding", [])
                for c in codings:
                    if c.get("display"):
                        value = c["display"]
                        break
        elif "valueString" in r:
            value = r["valueString"]
        elif "valueQuantity" in r:
            vq = r["valueQuantity"]
            value = f"{vq.get('value', '')} {vq.get('unit', '')}".strip()

        if category_name:
            rows.append({
                "Category": category_name,
                "Value": value,
            })

    return rows


def parse_vital_observations(resources: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Parse vital-signs Observation resources into vitals rows.

    Target format:
        | Vital | Value | Unit | Date |

    Args:
        resources: List of FHIR Observation resources (category=vital-signs).

    Returns:
        List of vitals row dicts.
    """
    rows = []
    for r in resources:
        if r.get("resourceType") != "Observation":
            continue

        # Vital name
        vital_name = r.get("code", {}).get("text", "")
        if not vital_name:
            codings = r.get("code", {}).get("coding", [])
            for c in codings:
                if c.get("display"):
                    vital_name = c["display"]
                    break

        # Value and unit
        value = ""
        unit = ""
        if "valueQuantity" in r:
            vq = r["valueQuantity"]
            value = str(vq.get("value", ""))
            unit = vq.get("unit", "")
        elif "valueCodeableConcept" in r:
            value = r["valueCodeableConcept"].get("text", "")
        elif "valueString" in r:
            value = r["valueString"]
        # Blood pressure has components instead of a single value
        elif "component" in r:
            parts = []
            for comp in r.get("component", []):
                comp_name = comp.get("code", {}).get("text", "")
                comp_vq = comp.get("valueQuantity", {})
                if comp_vq.get("value") is not None:
                    parts.append(f"{comp_vq['value']}")
                    if not unit:
                        unit = comp_vq.get("unit", "")
            if parts:
                value = "/".join(parts)

        # Date
        date = r.get("effectiveDateTime", "")
        if not date:
            date = r.get("effectivePeriod", {}).get("start", "")
        # Normalize to just date
        if "T" in date:
            date = date.split("T")[0]

        if vital_name and value:
            rows.append({
                "Vital": vital_name,
                "Value": value,
                "Unit": unit,
                "Date": date,
            })

    return rows


LAB_COLUMNS = ["Test", "Value", "Unit", "Ref Range", "Flag"]
LAB_KEY_COLS = ["Test", "draw_date"]
VITAL_COLUMNS = ["Vital", "Value", "Unit", "Date"]
VITAL_KEY_COLS = ["Vital", "Date"]
SOCIAL_COLUMNS = ["Category", "Value"]
SOCIAL_KEY_COLS = ["Category"]
