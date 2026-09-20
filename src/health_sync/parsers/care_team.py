from __future__ import annotations

"""Parse FHIR CareTeam resources into Care Team table rows."""

from typing import Any


def parse_care_team(resources: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Parse CareTeam resources into output Care Team format.

    Target format:
        | Name | Role | Contact |

    Args:
        resources: List of FHIR CareTeam resources.

    Returns:
        List of row dicts for the Care Team table.
    """
    rows = []
    seen = set()  # Deduplicate by name

    for r in resources:
        if r.get("resourceType") != "CareTeam":
            continue

        for participant in r.get("participant", []):
            member = participant.get("member", {})
            name = member.get("display", "")
            if not name or name in seen:
                continue
            seen.add(name)

            # Role
            roles = participant.get("role", [])
            role_text = ""
            if roles:
                role_text = roles[0].get("text", "")
                if not role_text:
                    codings = roles[0].get("coding", [])
                    for c in codings:
                        if c.get("display"):
                            role_text = c["display"]
                            break

            # Contact — would need to resolve the Practitioner reference
            # for telecom details, which requires additional API calls.
            # For now, leave blank — the writer preserves existing contact info.
            contact = ""

            rows.append({
                "Name": name,
                "Role": role_text,
                "Contact": contact,
            })

    return rows


COLUMNS = ["Name", "Role", "Contact"]
KEY_COLS = ["Name"]
