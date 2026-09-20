from __future__ import annotations

"""Write parsed FHIR data into clinical_extract.md.

This is the main output file for structured medical data. It has these sections:
- Medications (Active / Ended tables)
- Active Problems
- Allergies
- Immunizations
- Social History
- Encounters
- Procedures
- Care Team

Each section is updated independently using append-merge (never overwrite).
"""

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from health_sync.parsers.allergies import format_allergies_section, parse_allergies
from health_sync.parsers.care_team import COLUMNS as CT_COLS, KEY_COLS as CT_KEYS, parse_care_team
from health_sync.parsers.conditions import COLUMNS as COND_COLS, KEY_COLS as COND_KEYS, parse_conditions
from health_sync.parsers.encounters import parse_encounters
from health_sync.parsers.immunizations import COLUMNS as IMM_COLS, KEY_COLS as IMM_KEYS, parse_immunizations
from health_sync.parsers.medications import (
    ACTIVE_COLUMNS,
    ENDED_COLUMNS,
    HOSPITAL_COLUMNS,
    HOSPITAL_KEY_COLS,
    KEY_COLS as MED_KEYS,
    parse_medications,
)
from health_sync.parsers.observations import (
    SOCIAL_COLUMNS,
    SOCIAL_KEY_COLS,
    VITAL_COLUMNS,
    VITAL_KEY_COLS,
    parse_social_history,
    parse_vital_observations,
)
from health_sync.parsers.procedures import COLUMNS as PROC_COLS, KEY_COLS as PROC_KEYS, parse_procedures
from health_sync.writers.markdown import (
    find_section,
    _normalize_date,
    parse_table,
    replace_section,
    render_table,
    update_table_in_section,
)

RECENT_WINDOW_DAYS = 730

REQUIRED_TOP_LEVEL_SECTIONS = (
    "Medications",
    "Active Problems",
    "Allergies",
    "Immunizations",
    "Social History",
    "Vitals",
    "Encounters",
    "Procedures",
    "Care Team",
)


def _filter_resources(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter out OperationOutcome responses that aren't real resources."""
    return [r for r in data if r.get("resourceType") != "OperationOutcome"]


def _ensure_clinical_extract_scaffold(text: str) -> str:
    """Ensure placeholder files have the sections required by table writers."""
    if all(find_section(text, section, level=2) is None for section in REQUIRED_TOP_LEVEL_SECTIONS):
        return _scaffold_clinical_extract()

    scaffold = _scaffold_clinical_extract()
    for section in REQUIRED_TOP_LEVEL_SECTIONS:
        if find_section(text, section, level=2) is not None:
            continue
        scaffold_section = find_section(scaffold, section, level=2)
        if scaffold_section is None:
            continue
        _, _, content = scaffold_section
        text = text.rstrip() + f"\n\n## {section}\n{content.rstrip()}\n"

    return text


def _update_sources(text: str, provider_name: str) -> str:
    """Update the Sources section at the top of the file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    source_line = f"- {provider_name} — FHIR sync {timestamp}"

    def updated_source_content(content: str) -> str:
        # Check if this provider is already listed
        if provider_name.lower() in content.lower():
            lines = content.strip().split("\n")
            new_lines = []
            for line in lines:
                if provider_name.lower() in line.lower():
                    new_lines.append(source_line)
                else:
                    new_lines.append(line)
            return "\n".join(new_lines)
        stripped = content.strip()
        return f"{stripped}\n{source_line}" if stripped else source_line

    result = find_section(text, "Sources", level=2)
    if result is not None:
        _, _, content = result
        return replace_section(
            text,
            "Sources",
            "\n" + updated_source_content(content) + "\n",
            level=2,
        )

    bold_sources = re.search(
        r"(?ms)^(\*\*Sources:\*\*\s*\n)(.*?)(?=^---\s*$|^##\s+|\Z)",
        text,
    )
    if bold_sources is None:
        return text

    replacement = bold_sources.group(1) + updated_source_content(bold_sources.group(2)) + "\n\n"
    return text[:bold_sources.start()] + replacement + text[bold_sources.end():]


def _merge_people(existing: str, incoming: str) -> str:
    """Merge comma-separated care team names without duplicating them."""
    merged = []
    seen = set()
    for raw in [existing, incoming]:
        for part in raw.split(","):
            name = part.strip()
            key = name.lower()
            if name and key not in seen:
                merged.append(name)
                seen.add(key)
    return ", ".join(merged)


def _parse_date(value: str) -> date | None:
    """Parse the start date from a FHIR/markdown date or date range."""
    normalized = _normalize_date(value or "")
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").date()
    except ValueError:
        return None


def _is_recent_row(
    row: dict[str, str],
    date_columns: tuple[str, ...],
    *,
    keep_unknown_recent: bool = False,
) -> bool:
    dates = [_parse_date(row.get(col, "")) for col in date_columns]
    dates = [d for d in dates if d is not None]
    if not dates:
        return keep_unknown_recent
    return max(dates) >= date.today() - timedelta(days=RECENT_WINDOW_DAYS)


def _sort_rows_by_date(rows: list[dict[str, str]], date_columns: tuple[str, ...]) -> list[dict[str, str]]:
    def row_date(row: dict[str, str]) -> date:
        for col in date_columns:
            parsed = _parse_date(row.get(col, ""))
            if parsed is not None:
                return parsed
        return date.min

    return sorted(rows, key=row_date, reverse=True)


def _parse_all_tables(content: str) -> list[dict[str, str]]:
    """Parse every markdown table in a section, including nested subsections."""
    rows: list[dict[str, str]] = []
    lines = content.splitlines()
    idx = 0
    while idx < len(lines):
        if "|" not in lines[idx]:
            idx += 1
            continue
        if idx + 1 >= len(lines) or not re.match(r"^\|?[\s\-:|]+\|", lines[idx + 1].strip()):
            idx += 1
            continue

        start = idx
        idx += 2
        while idx < len(lines) and "|" in lines[idx]:
            idx += 1
        _, table_rows = parse_table("\n".join(lines[start:idx]))
        rows.extend(table_rows)

    return rows


def _dedupe_rows(rows: list[dict[str, str]], key_cols: tuple[str, ...]) -> list[dict[str, str]]:
    deduped = []
    seen = set()
    for row in rows:
        key = tuple((row.get(col, "") or "").strip().lower() for col in key_cols)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _combine_note_values(existing: str, incoming: str) -> str:
    """Combine note fields conservatively, preserving both when distinct."""
    existing = existing.strip()
    incoming = incoming.strip()

    if not existing:
        return incoming
    if not incoming:
        return existing

    if existing.lower() == incoming.lower():
        return existing
    if existing.lower() in incoming.lower():
        return incoming
    if incoming.lower() in existing.lower():
        return existing

    return f"{existing} || {incoming}"


def _dedupe_note_columns(row: dict[str, str]) -> dict[str, str]:
    """Drop redundant other-notes text when it matches provider notes."""
    provider = row.get("Provider Notes", "").strip()
    other = row.get("Other Notes", "").strip()
    if provider and other and provider.lower() == other.lower():
        row["Other Notes"] = ""
    return row


def _render_split_table_section(
    intro: str,
    recent_heading: str,
    historical_heading: str,
    columns: list[str],
    rows: list[dict[str, str]],
    date_columns: tuple[str, ...],
    *,
    keep_unknown_recent: bool = False,
) -> str:
    recent = []
    historical = []
    for row in rows:
        if _is_recent_row(row, date_columns, keep_unknown_recent=keep_unknown_recent):
            recent.append(row)
        else:
            historical.append(row)

    parts = [intro.strip()] if intro.strip() else []
    parts.extend([
        f"### {recent_heading}",
        render_table(columns, _sort_rows_by_date(recent, date_columns)),
        "",
        f"### {historical_heading}",
        render_table(columns, _sort_rows_by_date(historical, date_columns)),
    ])
    return "\n\n".join(parts).rstrip() + "\n"


def _update_encounters_section(text: str, new_rows: list[dict[str, str]]) -> str:
    """Merge encounter rows, keeping provider notes separate from manual notes."""
    columns = ["Date", "Type", "Department", "Care Team", "Provider Notes", "Other Notes"]
    result = find_section(text, "Encounters", level=2)
    if result is None:
        return text

    _, _, section_content = result
    existing_rows = _parse_all_tables(section_content)

    migrated_existing = []
    for row in existing_rows:
        provider_notes = row.get("Provider Notes", "").strip()
        other_notes = row.get("Other Notes", "").strip()
        legacy_notes = row.get("Notes", "").strip()

        if legacy_notes and not provider_notes and not other_notes:
            other_notes = legacy_notes
        elif legacy_notes and legacy_notes.lower() != provider_notes.lower():
            other_notes = _combine_note_values(other_notes, legacy_notes)

        migrated_existing.append({
            "Date": row.get("Date", ""),
            "Type": row.get("Type", ""),
            "Department": row.get("Department", ""),
            "Care Team": row.get("Care Team", ""),
            "Provider Notes": provider_notes,
            "Other Notes": other_notes,
        })

    merged_by_key = {}
    merged_order = []

    def row_key(row: dict[str, str]) -> tuple[str, str]:
        from health_sync.writers.markdown import _normalize_date

        return (
            _normalize_date(row.get("Date", "")),
            row.get("Department", "").strip().lower(),
        )

    for row in migrated_existing:
        key = row_key(row)
        merged_by_key[key] = dict(row)
        merged_order.append(key)

    for row in new_rows:
        key = row_key(row)
        if key not in merged_by_key:
            merged_by_key[key] = dict(row)
            merged_order.append(key)
            continue

        existing = merged_by_key[key]
        existing["Type"] = existing["Type"] or row.get("Type", "")
        existing["Department"] = existing["Department"] or row.get("Department", "")
        existing["Care Team"] = _merge_people(existing["Care Team"], row.get("Care Team", ""))
        existing["Provider Notes"] = _combine_note_values(
            existing["Provider Notes"],
            row.get("Provider Notes", ""),
        )
        existing["Other Notes"] = _combine_note_values(
            existing["Other Notes"],
            row.get("Other Notes", ""),
        )
        _dedupe_note_columns(existing)

    merged_rows = [_dedupe_note_columns(merged_by_key[key]) for key in merged_order]
    new_section = _render_split_table_section(
        "Recent means the last two years. Older encounters are retained below for context.",
        "Recent",
        "Historical",
        columns,
        merged_rows,
        ("Date",),
    )

    return replace_section(text, "Encounters", "\n" + new_section, level=2)


def _update_vitals_section(text: str, new_rows: list[dict[str, str]]) -> str:
    columns = VITAL_COLUMNS
    result = find_section(text, "Vitals", level=2)
    if result is None:
        return text

    _, _, section_content = result
    rows = _parse_all_tables(section_content) + new_rows
    rows = _dedupe_rows(rows, tuple(VITAL_KEY_COLS))
    new_section = _render_split_table_section(
        "Recent means the last two years. Historical vitals are retained below.",
        "Recent",
        "Historical",
        columns,
        rows,
        ("Date",),
    )
    return replace_section(text, "Vitals", "\n" + new_section, level=2)


def _organize_medications_section(text: str) -> str:
    result = find_section(text, "Medications", level=2)
    if result is None:
        return text

    _, _, content = result
    subsection_matches = list(re.finditer(r"^###\s+(.+)$", content, flags=re.MULTILINE))
    if not subsection_matches:
        return text

    active_rows: list[dict[str, str]] = []
    ended_rows: list[dict[str, str]] = []
    hospital_blocks: list[str] = []

    for idx, match in enumerate(subsection_matches):
        heading = match.group(1).strip()
        start = match.end()
        end = subsection_matches[idx + 1].start() if idx + 1 < len(subsection_matches) else len(content)
        block_content = content[start:end].strip()
        block = f"### {heading}\n\n{block_content}".strip()
        heading_lower = heading.lower()

        if heading_lower in {"active", "older active entries", "historical active"}:
            active_rows.extend(_parse_all_tables(block_content))
        elif heading_lower in {"ended", "historical ended"}:
            ended_rows.extend(_parse_all_tables(block_content))
        elif heading_lower.startswith("administered in hospital"):
            hospital_blocks.append(block)

    active_rows = _dedupe_rows(active_rows, tuple(MED_KEYS))
    ended_rows = _dedupe_rows(ended_rows, tuple(MED_KEYS))

    active_recent = []
    active_older = []
    for row in active_rows:
        if _is_recent_row(row, ("Started",), keep_unknown_recent=True):
            active_recent.append(row)
        else:
            active_older.append(row)

    ended_recent = []
    ended_older = []
    for row in ended_rows:
        if _is_recent_row(row, ("End Date", "Started")):
            ended_recent.append(row)
        else:
            ended_older.append(row)

    parts = [
        "FHIR-active medications are split so older active entries do not crowd the current view. Verify active status against the curated profile before treating a row as current.",
        "### Active",
        render_table(ACTIVE_COLUMNS, _sort_rows_by_date(active_recent, ("Started",))),
        "",
        "### Older Active Entries",
        render_table(ACTIVE_COLUMNS, _sort_rows_by_date(active_older, ("Started",))),
        "",
        "### Ended",
        render_table(ENDED_COLUMNS, _sort_rows_by_date(ended_recent, ("End Date", "Started"))),
        "",
        "### Historical Ended",
        render_table(ENDED_COLUMNS, _sort_rows_by_date(ended_older, ("End Date", "Started"))),
    ]
    if hospital_blocks:
        parts.extend([""] + hospital_blocks)

    return replace_section(text, "Medications", "\n" + "\n\n".join(parts).rstrip() + "\n", level=2)


def _sort_table_section(
    text: str,
    heading: str,
    columns: list[str],
    key_cols: tuple[str, ...],
    sort_key,
) -> str:
    result = find_section(text, heading, level=2)
    if result is None:
        return text

    _, _, content = result
    rows = _dedupe_rows(_parse_all_tables(content), key_cols)
    rows = sorted(rows, key=sort_key)
    return replace_section(text, heading, "\n" + render_table(columns, rows) + "\n", level=2)


def _normalize_structured_order(text: str) -> str:
    """Keep non-split clinical extract tables in stable, scan-friendly order."""
    text = _sort_table_section(
        text,
        "Active Problems",
        COND_COLS,
        tuple(COND_KEYS),
        lambda row: (
            0 if row.get("Status", "").strip().lower() == "active" else 1,
            -(_parse_date(row.get("Onset", "")) or date.min).toordinal(),
            row.get("Diagnosis", "").strip().lower(),
        ),
    )
    text = _sort_table_section(
        text,
        "Immunizations",
        IMM_COLS,
        tuple(IMM_KEYS),
        lambda row: (
            -(_parse_date(row.get("Date", "")) or date.min).toordinal(),
            row.get("Vaccine", "").strip().lower(),
        ),
    )
    text = _sort_table_section(
        text,
        "Social History",
        SOCIAL_COLUMNS,
        tuple(SOCIAL_KEY_COLS),
        lambda row: row.get("Category", "").strip().lower(),
    )
    text = _sort_table_section(
        text,
        "Procedures",
        PROC_COLS,
        tuple(PROC_KEYS),
        lambda row: (
            -(_parse_date(row.get("Date", "")) or date.min).toordinal(),
            row.get("Procedure", "").strip().lower(),
        ),
    )
    text = _sort_table_section(
        text,
        "Care Team",
        CT_COLS,
        tuple(CT_KEYS),
        lambda row: row.get("Name", "").strip().lower(),
    )
    return text


def update_clinical_extract(
    file_path: Path,
    fhir_data: dict[str, list[dict[str, Any]]],
    provider_name: str,
) -> str:
    """Update clinical_extract.md with new FHIR data.

    Reads the existing file, merges each section with new data, writes back.

    Args:
        file_path: Path to clinical_extract.md.
        fhir_data: Dict mapping resource type names to FHIR resource lists.
        provider_name: Display name of the provider (for Sources tracking).

    Returns:
        The updated markdown text.
    """
    if file_path.exists():
        text = file_path.read_text()
    else:
        text = _scaffold_clinical_extract()
    text = _ensure_clinical_extract_scaffold(text)

    # Update sources
    text = _update_sources(text, provider_name)

    # Conditions → Active Problems
    conditions = _filter_resources(fhir_data.get("Condition", []))
    if conditions:
        rows = parse_conditions(conditions)
        text = update_table_in_section(text, "Active Problems", COND_COLS, rows, COND_KEYS)

    # Medications → Active, Ended (outpatient), and Hospital Administered tables
    # Rule: if a med already exists anywhere in the file (Active, Ended, or Hospital),
    # don't add a new row. This handles status drift and duplicate entries.
    meds = _filter_resources(fhir_data.get("MedicationRequest", []))
    if meds:
        encounter_resources = _filter_resources(fhir_data.get("Encounter", []))
        active, ended, hospital_by_stay = parse_medications(meds, encounters=encounter_resources)

        # Build a set of all existing medication names across all sections
        existing_med_names = set()
        medications_section = find_section(text, "Medications", level=2)
        if medications_section:
            _, _, medications_content = medications_section
            for r in _parse_all_tables(medications_content):
                name = r.get("Medication", "").strip().lower()
                existing_med_names.add(name)
                base = name.split("(")[0].strip()
                drug_only = base.split(" ")[0] if base else ""
                if drug_only:
                    existing_med_names.add(drug_only)

        def med_already_exists(med_name: str) -> bool:
            name = med_name.strip().lower()
            if name in existing_med_names:
                return True
            base = name.split("(")[0].strip()
            drug_only = base.split(" ")[0] if base else ""
            if drug_only and drug_only in existing_med_names:
                return True
            return False

        # Filter out meds that already exist in any section
        if existing_med_names:
            filtered_active = [m for m in active if not med_already_exists(m["Medication"])]
            filtered_ended = [m for m in ended if not med_already_exists(m["Medication"])]
            filtered_hospital_by_stay = {
                stay: [m for m in meds_list if not med_already_exists(m["Medication"])]
                for stay, meds_list in hospital_by_stay.items()
            }
        else:
            filtered_active = active
            filtered_ended = ended
            filtered_hospital_by_stay = hospital_by_stay

        if filtered_active:
            text = update_table_in_section(text, "Active", ACTIVE_COLUMNS, filtered_active, MED_KEYS, level=3)
        if filtered_ended:
            text = update_table_in_section(text, "Ended", ENDED_COLUMNS, filtered_ended, MED_KEYS, level=3)

        # Write per-stay hospital sections
        # First, find all existing "Administered in Hospital" sections and their date ranges
        import re as _re
        from health_sync.writers.markdown import _normalize_date

        def _find_existing_hospital_section(text: str, stay_dates: str) -> str | None:
            """Find an existing hospital section whose date range overlaps with stay_dates.

            Returns the exact section heading if found, None otherwise.
            """
            pattern = _re.compile(r"^### Administered in Hospital \((.+?)\)\s*$", _re.MULTILINE)
            # Normalize the FHIR stay start date for comparison
            fhir_start = _normalize_date(stay_dates.split(" to ")[0] if " to " in stay_dates else stay_dates)

            for match in pattern.finditer(text):
                existing_range = match.group(1)
                # Normalize existing range's start date
                # Handle formats like "1/1-1/3/2020" or "1/1/2020 - 1/3/2020"
                existing_start_raw = _re.split(r"[-–—]", existing_range)[0].strip()
                # If it's just M/D without year, append the year from the end
                if "/" in existing_range and existing_start_raw.count("/") < 2:
                    year_match = _re.search(r"(\d{4})", existing_range)
                    if year_match:
                        existing_start_raw += f"/{year_match.group(1)}"
                existing_start = _normalize_date(existing_start_raw)

                # Check if dates are within 2 days (admission vs first med order)
                try:
                    from datetime import datetime, timedelta
                    d1 = datetime.strptime(fhir_start, "%Y-%m-%d")
                    d2 = datetime.strptime(existing_start, "%Y-%m-%d")
                    if abs((d1 - d2).days) <= 2:
                        return f"Administered in Hospital ({existing_range})"
                except (ValueError, TypeError):
                    pass

            return None

        for stay_dates, stay_meds in sorted(filtered_hospital_by_stay.items()):
            if not stay_meds:
                continue

            # Check if an existing hospital section covers this stay
            existing_heading = _find_existing_hospital_section(text, stay_dates)

            if existing_heading:
                # Merge into the existing section
                section_name = existing_heading.replace("### ", "")
                text = update_table_in_section(text, section_name, HOSPITAL_COLUMNS, stay_meds, HOSPITAL_KEY_COLS, level=3)
            else:
                # Create new section for this stay
                section_name = f"Administered in Hospital ({stay_dates})"
                if find_section(text, section_name, level=3) is None:
                    ended_sec = find_section(text, "Ended", level=3)
                    if ended_sec:
                        _, end_pos, _ = ended_sec
                        scaffold = f"\n### {section_name}\n\n| Medication | Route | Details |\n|---|---|---|\n\n"
                        text = text[:end_pos] + scaffold + text[end_pos:]
                text = update_table_in_section(text, section_name, HOSPITAL_COLUMNS, stay_meds, HOSPITAL_KEY_COLS, level=3)

    # Allergies — only update if there are actual active allergies from FHIR.
    # If FHIR says "no known allergies," preserve the existing section
    # (which may have manual annotations like provenance details).
    allergies_data = _filter_resources(fhir_data.get("AllergyIntolerance", []))
    if allergies_data:
        allergies = parse_allergies(allergies_data)
        active_allergies = [a for a in allergies if a.get("status", "").lower() == "active"
                           and a.get("name", "").lower() not in ("no known allergies", "nka", "")]
        if active_allergies:
            allergy_text = format_allergies_section(active_allergies)
            text = replace_section(text, "Allergies", "\n" + allergy_text, level=2)

    # Immunizations
    immunizations = _filter_resources(fhir_data.get("Immunization", []))
    if immunizations:
        rows = parse_immunizations(immunizations)
        text = update_table_in_section(text, "Immunizations", IMM_COLS, rows, IMM_KEYS)

    # Social History
    social = _filter_resources(fhir_data.get("Observation_social-history", []))
    if not social:
        # Fall back to merged observation file
        all_obs = _filter_resources(fhir_data.get("Observation", []))
        social = [o for o in all_obs
                  if any(c.get("coding", [{}])[0].get("code") == "social-history"
                         for c in o.get("category", []))]
    if social:
        rows = parse_social_history(social)
        text = update_table_in_section(text, "Social History", SOCIAL_COLUMNS, rows, SOCIAL_KEY_COLS)

    # Vitals
    vitals = _filter_resources(fhir_data.get("Observation_vital-signs", []))
    if vitals:
        rows = parse_vital_observations(vitals)
        # Create Vitals section if it doesn't exist
        if find_section(text, "Vitals", level=2) is None:
            # Insert before ## Encounters
            enc_section = find_section(text, "Encounters", level=2)
            if enc_section:
                start, _, _ = enc_section
                vitals_scaffold = "\n## Vitals\n\n| Vital | Value | Unit | Date |\n|---|---|---|---|\n\n---\n\n"
                text = text[:start] + vitals_scaffold + text[start:]
        text = _update_vitals_section(text, rows)

    # Encounters
    encounters = _filter_resources(fhir_data.get("Encounter", []))
    if encounters:
        rows = parse_encounters(encounters)
        text = _update_encounters_section(text, rows)

    # Procedures
    procedures = _filter_resources(fhir_data.get("Procedure", []))
    if procedures:
        rows = parse_procedures(procedures)
        text = update_table_in_section(text, "Procedures", PROC_COLS, rows, PROC_KEYS)

    # Care Team
    care_teams = _filter_resources(fhir_data.get("CareTeam", []))
    if care_teams:
        rows = parse_care_team(care_teams)
        text = update_table_in_section(text, "Care Team", CT_COLS, rows, CT_KEYS)

    text = _update_vitals_section(text, [])
    text = _update_encounters_section(text, [])
    text = _organize_medications_section(text)
    text = _normalize_structured_order(text)

    # Write back
    file_path.write_text(text)
    return text


def _scaffold_clinical_extract() -> str:
    """Create a new clinical_extract.md scaffold."""
    return """# Clinical Extract

Canonical source for structured medical data. Updated automatically via chartstash.

**Sources:**

---

## Medications

FHIR-active medications are split so older active entries do not crowd the current view. Verify active status against the curated profile before treating a row as current.

### Active

| Medication | Sig | Route | Dose | Started | Prescriber |
|---|---|---|---|---|---|

### Older Active Entries

| Medication | Sig | Route | Dose | Started | Prescriber |
|---|---|---|---|---|---|

### Ended

| Medication | Sig | Route | Dose | Started | End Date | Status | Prescriber |
|---|---|---|---|---|---|---|---|

### Historical Ended

| Medication | Sig | Route | Dose | Started | End Date | Status | Prescriber |
|---|---|---|---|---|---|---|---|

### Administered in Hospital

| Medication | Route | Details |
|---|---|---|

---

## Active Problems

| Diagnosis | ICD Code | Onset | Status |
|---|---|---|---|

---

## Allergies

**No known active allergies**

---

## Immunizations

| Vaccine | Date | Notes |
|---|---|---|

---

## Social History

| Category | Value |
|---|---|

---

## Vitals

Recent means the last two years. Historical vitals are retained below.

### Recent

| Vital | Value | Unit | Date |
|---|---|---|---|

### Historical

| Vital | Value | Unit | Date |
|---|---|---|---|

---

## Encounters

Recent means the last two years. Older encounters are retained below for context.

### Recent

| Date | Type | Department | Care Team | Provider Notes | Other Notes |
|---|---|---|---|---|---|

### Historical

| Date | Type | Department | Care Team | Provider Notes | Other Notes |
|---|---|---|---|---|---|

---

## Procedures

| Procedure | Date | Associated Diagnosis |
|---|---|---|

---

## Care Team

| Name | Role | Contact |
|---|---|---|

"""
