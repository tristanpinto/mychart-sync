from __future__ import annotations

"""Parse FHIR MedicationRequest resources into Medications table rows."""

import re
from typing import Any


def _clean_sig(sig: str) -> str:
    """Clean up a medication Sig field for readable markdown output.

    Removes clinical decision support noise (SOUND-ALIKE alerts, RESTRICTED
    labels, weight-based dosing annotations) and collapses newlines.
    """
    if not sig:
        return sig
    # Collapse newlines to spaces
    sig = " ".join(sig.split())
    # Remove clinical decision support noise
    sig = re.sub(r"SOUND-ALIKE.*?ALERT\.?\s*", "", sig, flags=re.IGNORECASE)
    sig = re.sub(r"LOOK-ALIKE.*?ALERT\.?\s*", "", sig, flags=re.IGNORECASE)
    sig = re.sub(r"RESTRICTED\s*[-—]?\s*.*?(?=\s{2}|$)", "", sig, flags=re.IGNORECASE)
    sig = re.sub(r"Confirmed desired product\.\s*", "", sig, flags=re.IGNORECASE)
    sig = re.sub(r"Clinical Decision Support:.*?(?=\s{2}|$)", "", sig, flags=re.IGNORECASE)
    # Remove weight-based annotations like (0.396 mg/kg) or (6.61 mg/kg)
    sig = re.sub(r"\(\d+\.?\d*\s*m?g/kg\),?\s*", "", sig)
    # Clean up extra whitespace
    sig = " ".join(sig.split()).strip()
    # Remove trailing comma
    sig = sig.rstrip(",").strip()
    return sig


def _condense_hospital_details(sig: str, dose: str, route: str) -> str:
    """Condense a hospital medication's sig into a compact Details field.

    Produces something like: "2 g, EVERY 8 HOURS, For 2 doses"
    instead of the full verbose sig.
    """
    parts = []
    if dose:
        parts.append(dose)
    if sig:
        # Extract key info: frequency, duration, timing
        # Already cleaned by _clean_sig, so no newlines
        # Try to grab the core scheduling info
        for pattern in [
            r"(EVERY\s+\d+\s+\w+)",
            r"(\d+\s+TIMES?\s+\w+)",
            r"(ONCE\s+\w*)",
            r"(CONTINUOUS\s*\w*)",
            r"(DAILY\s*\w*)",
            r"(AT BEDTIME)",
        ]:
            match = re.search(pattern, sig, re.IGNORECASE)
            if match:
                parts.append(match.group(1).strip())
                break
        # Duration
        for pattern in [
            r"(For\s+\d+\s+doses?)",
            r"(For\s+\d+\s+days?)",
            r"(For\s+\d+\s+hours?)",
        ]:
            match = re.search(pattern, sig, re.IGNORECASE)
            if match:
                parts.append(match.group(1).strip())
                break
        # Context (PACU, Anesthesia, etc.)
        for ctx in ["PACU", "Anesthesia Intra-op", "Post-op"]:
            if ctx.lower() in sig.lower():
                parts.append(ctx)
                break
    if not parts:
        return sig[:80] if sig else ""
    return "; ".join(parts)


def parse_medications(
    resources: list[dict[str, Any]],
    encounters: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, list[dict[str, str]]]]:
    """Parse MedicationRequest resources into output Medications format.

    Active medications format:
        | Medication | Sig | Route | Dose | Started | Prescriber |

    Ended medications format:
        | Medication | Sig | Route | Dose | Started | End Date | Status | Prescriber |

    Hospital administered format (per stay):
        | Medication | Route | Details |

    Args:
        resources: List of FHIR MedicationRequest resources.
        encounters: Optional list of FHIR Encounter resources for date range lookup.

    Returns:
        (active_rows, ended_outpatient_rows, hospital_by_stay) where
        hospital_by_stay maps "M/D/YYYY - M/D/YYYY" date range strings
        to lists of hospital med rows.
    """
    # Build encounter lookup: ID -> date range string
    enc_dates: dict[str, str] = {}
    if encounters:
        for e in encounters:
            if e.get("resourceType") != "Encounter":
                continue
            for ident in e.get("identifier", []):
                enc_id = ident.get("value", "")
                if enc_id:
                    period = e.get("period", {})
                    start = period.get("start", "")[:10]
                    end = period.get("end", "")[:10]
                    if start:
                        enc_dates[enc_id] = f"{start} to {end}" if end and end != start else start

    # Find inpatient encounter date ranges to merge anesthesia meds into
    inpatient_stays: list[tuple[str, str, str]] = []  # (enc_id, start, end)
    if encounters:
        for e in encounters:
            if e.get("resourceType") != "Encounter":
                continue
            enc_class = e.get("class", {}).get("code", "")
            # Class codes for inpatient: IMP, or numeric codes used by Epic
            period = e.get("period", {})
            start = period.get("start", "")[:10]
            end = period.get("end", "")[:10]
            if start and end and start != end:  # Multi-day = inpatient
                for ident in e.get("identifier", []):
                    inpatient_stays.append((ident.get("value", ""), start, end))

    active = []
    ended = []
    hospital_by_stay: dict[str, list[dict[str, str]]] = {}

    for r in resources:
        if r.get("resourceType") != "MedicationRequest":
            continue

        # Medication name
        med_name = ""
        if "medicationReference" in r:
            med_name = r["medicationReference"].get("display", "")
        elif "medicationCodeableConcept" in r:
            med_name = r["medicationCodeableConcept"].get("text", "")

        # Category: Inpatient vs Outpatient vs Community
        category = ""
        cat_list = r.get("category", [])
        if cat_list:
            category = cat_list[0].get("text", "")

        # Dosage instruction
        sig = ""
        route = ""
        dose = ""
        dosage_list = r.get("dosageInstruction", [])
        if dosage_list:
            dosage = dosage_list[0]
            sig = _clean_sig(dosage.get("patientInstruction", "") or dosage.get("text", ""))
            route = dosage.get("route", {}).get("text", "")

            # Dose — prefer the 'ordered' or 'calculated' type
            for dr in dosage.get("doseAndRate", []):
                dose_qty = dr.get("doseQuantity", {})
                if dose_qty:
                    val = dose_qty.get("value", "")
                    unit = dose_qty.get("unit", "")
                    dtype = dr.get("type", {}).get("text", "")
                    if dtype in ("ordered", "calculated", ""):
                        dose = f"{val} {unit}".strip()
                        break

        # Dates
        started = r.get("authoredOn", "")

        # Prescriber
        prescriber = r.get("requester", {}).get("display", "")

        # Status
        status = r.get("status", "")

        # End date from dispenseRequest or dosageInstruction bounds
        end_date = ""
        if dosage_list:
            bounds = dosage_list[0].get("timing", {}).get("repeat", {}).get("boundsPeriod", {})
            end_date = bounds.get("end", "")
        if not end_date:
            end_date = r.get("dispenseRequest", {}).get("validityPeriod", {}).get("end", "")

        # Detect hospital/inpatient medications
        # Category "Inpatient" is the primary signal, but anesthesia meds
        # are categorized as "Outpatient" by Epic — catch those via encounter
        encounter_display = r.get("encounter", {}).get("display", "").lower()
        is_hospital = (
            category.lower() == "inpatient"
            or encounter_display in ("anesthesia", "hospital encounter")
            or "intra-op" in sig.lower()
        )

        # Route hospital meds to per-stay lists
        if is_hospital and status != "active":
            details = _condense_hospital_details(sig, dose, route)
            # Determine which stay this belongs to
            enc_id = r.get("encounter", {}).get("identifier", {}).get("value", "")
            authored_date = r.get("authoredOn", "")[:10]

            # Try to find the parent inpatient stay for this med
            stay_key = ""
            for ip_id, ip_start, ip_end in inpatient_stays:
                if enc_id == ip_id:
                    stay_key = f"{ip_start} to {ip_end}"
                    break
                # Check if med date falls within an inpatient stay
                if authored_date and ip_start <= authored_date <= ip_end:
                    stay_key = f"{ip_start} to {ip_end}"
                    break

            if not stay_key:
                stay_key = enc_dates.get(enc_id, authored_date or "unknown")

            if stay_key not in hospital_by_stay:
                hospital_by_stay[stay_key] = []
            hospital_by_stay[stay_key].append({
                "Medication": med_name,
                "Route": route,
                "Details": details,
            })
        elif status == "active":
            active.append({
                "Medication": med_name,
                "Sig": sig,
                "Route": route,
                "Dose": dose,
                "Started": started,
                "Prescriber": prescriber,
            })
        else:
            ended.append({
                "Medication": med_name,
                "Sig": sig,
                "Route": route,
                "Dose": dose,
                "Started": started,
                "End Date": end_date,
                "Status": status.capitalize(),
                "Prescriber": prescriber,
            })

    return active, ended, hospital_by_stay


ACTIVE_COLUMNS = ["Medication", "Sig", "Route", "Dose", "Started", "Prescriber"]
ENDED_COLUMNS = ["Medication", "Sig", "Route", "Dose", "Started", "End Date", "Status", "Prescriber"]
HOSPITAL_COLUMNS = ["Medication", "Route", "Details"]
KEY_COLS = ["Medication", "Started"]
HOSPITAL_KEY_COLS = ["Medication", "Route"]
