from __future__ import annotations

"""Parse FHIR Patient resource into demographics for health_profile.md."""

from typing import Any


def parse_patient(resource: dict[str, Any]) -> dict[str, str]:
    """Parse a Patient resource into health profile demographics.

    Returns a dict with keys: name, dob, gender, address, phone, email,
    marital_status, race, ethnicity.

    Args:
        resource: A single FHIR Patient resource.

    Returns:
        Dict of demographic fields.
    """
    if resource.get("resourceType") != "Patient":
        return {}

    # Name
    names = resource.get("name", [])
    name = ""
    for n in names:
        if n.get("use") == "official" or not name:
            given = " ".join(n.get("given", []))
            family = n.get("family", "")
            name = f"{given} {family}".strip()

    # DOB
    dob = resource.get("birthDate", "")

    # Gender
    gender = resource.get("gender", "")

    # Address
    addresses = resource.get("address", [])
    address = ""
    if addresses:
        a = addresses[0]
        parts = a.get("line", []) + [
            a.get("city", ""),
            a.get("state", ""),
            a.get("postalCode", ""),
        ]
        address = ", ".join(p for p in parts if p)

    # Phone and email from telecom
    phone = ""
    email = ""
    for telecom in resource.get("telecom", []):
        if telecom.get("system") == "phone" and not phone:
            phone = telecom.get("value", "")
        elif telecom.get("system") == "email" and not email:
            email = telecom.get("value", "")

    # Marital status
    marital = resource.get("maritalStatus", {}).get("text", "")

    # Race and ethnicity from extensions
    race = ""
    ethnicity = ""
    for ext in resource.get("extension", []):
        url = ext.get("url", "")
        if "us-core-race" in url:
            for sub in ext.get("extension", []):
                if sub.get("url") == "text":
                    race = sub.get("valueString", "")
                elif sub.get("url") == "ombCategory":
                    coding = sub.get("valueCoding", {})
                    if not race and coding.get("display"):
                        race = coding["display"]
        elif "us-core-ethnicity" in url:
            for sub in ext.get("extension", []):
                if sub.get("url") == "text":
                    ethnicity = sub.get("valueString", "")
                elif sub.get("url") == "ombCategory":
                    coding = sub.get("valueCoding", {})
                    if not ethnicity and coding.get("display"):
                        ethnicity = coding["display"]

    return {
        "name": name,
        "dob": dob,
        "gender": gender,
        "address": address,
        "phone": phone,
        "email": email,
        "marital_status": marital,
        "race": race,
        "ethnicity": ethnicity,
    }
