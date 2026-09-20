from __future__ import annotations

"""Parse FHIR AllergyIntolerance resources into Allergies section."""

from typing import Any


def parse_allergies(resources: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Extract allergy names, reactions, status, and onset dates."""
    allergies = []

    for r in resources:
        if r.get("resourceType") != "AllergyIntolerance":
            continue

        name = r.get("code", {}).get("text", "")
        if not name:
            codings = r.get("code", {}).get("coding", [])
            for c in codings:
                if c.get("display"):
                    name = c["display"]
                    break

        categories = r.get("category", [])
        category = categories[0] if categories else ""

        criticality = r.get("criticality", "")

        # Reactions
        reaction_texts = []
        for reaction in r.get("reaction", []):
            for manifestation in reaction.get("manifestation", []):
                text = manifestation.get("text", "")
                if not text:
                    codings = manifestation.get("coding", [])
                    for c in codings:
                        if c.get("display"):
                            text = c["display"]
                            break
                if text:
                    reaction_texts.append(text)

        status = r.get("clinicalStatus", {}).get("text", "")
        onset = r.get("onsetDateTime", "")

        allergies.append({
            "name": name,
            "category": category,
            "criticality": criticality,
            "reactions": reaction_texts,
            "status": status,
            "onset": onset,
        })

    return allergies
