from __future__ import annotations

"""Parse FHIR AllergyIntolerance resources into Allergies section."""

from typing import Any


def parse_allergies(resources: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Parse AllergyIntolerance resources into structured allergy data.

    The Allergies section is free-text, not a table. This returns
    structured data that the writer formats appropriately.

    Each dict has: name, category, criticality, reactions, status, onset.

    Args:
        resources: List of FHIR AllergyIntolerance resources.

    Returns:
        List of allergy dicts.
    """
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


def format_allergies_section(allergies: list[dict[str, str]]) -> str:
    """Format parsed allergies into the output directory's free-text format.

    Returns text like:
        - **PENICILLIN G** (medication, low criticality) — Reactions: Rash, Hives. Onset: 2019-04-09.
    Or:
        **No known active allergies**
    """
    active = [a for a in allergies if a.get("status", "").lower() == "active"]

    if not active:
        return "**No known active allergies**\n"

    lines = []
    for a in active:
        parts = []
        if a["category"]:
            parts.append(a["category"])
        if a["criticality"]:
            parts.append(f"{a['criticality']} criticality")
        qualifier = f" ({', '.join(parts)})" if parts else ""

        line = f"- **{a['name']}**{qualifier}"
        if a["reactions"]:
            line += f" — Reactions: {', '.join(a['reactions'])}."
        if a["onset"]:
            line += f" Onset: {a['onset']}."
        lines.append(line)

    return "\n".join(lines) + "\n"
