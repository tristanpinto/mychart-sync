from __future__ import annotations

"""Write lab results to lab_results.md.

Labs are organized by draw date, with panels as sub-sections.
New draw dates create new sections; existing dates get new tests merged in.
"""

from collections import defaultdict
from pathlib import Path
from typing import Any
import re

from health_sync.parsers.observations import LAB_COLUMNS, parse_lab_observations
from health_sync.writers.markdown import find_section, parse_table, merge_rows, render_table


def _filter_resources(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in data if r.get("resourceType") != "OperationOutcome"]


def _lab_section_sort_key(block: str) -> tuple[int, str]:
    match = re.match(r"^##\s+(\d{4}-\d{2}-\d{2}|Unknown)\b", block)
    if match is None:
        return (2, "")

    date = match.group(1)
    if date == "Unknown":
        return (1, date)
    return (0, date)


def _sort_lab_sections(text: str) -> str:
    """Sort lab result date sections globally, regardless of provider source."""
    heading_matches = list(re.finditer(r"^##\s+.+$", text, flags=re.MULTILINE))
    if not heading_matches:
        return text

    preamble = text[: heading_matches[0].start()].rstrip()
    sections = []
    for index, match in enumerate(heading_matches):
        start = match.start()
        end = (
            heading_matches[index + 1].start()
            if index + 1 < len(heading_matches)
            else len(text)
        )
        sections.append(text[start:end].strip())

    sections.sort(key=_lab_section_sort_key)
    return preamble + "\n\n" + "\n\n".join(sections).rstrip() + "\n"


def update_lab_results(
    file_path: Path,
    fhir_data: dict[str, list[dict[str, Any]]],
    provider_name: str,
) -> str:
    """Update lab_results.md with new lab observations.

    Groups labs by draw date, creates sections per date, merges into
    existing sections.

    Args:
        file_path: Path to lab_results.md.
        fhir_data: Dict mapping resource type names to FHIR resource lists.
        provider_name: Provider name for source tracking.

    Returns:
        The updated markdown text.
    """
    # Collect lab observations
    lab_obs = _filter_resources(fhir_data.get("Observation_laboratory", []))
    if not lab_obs:
        all_obs = _filter_resources(fhir_data.get("Observation", []))
        lab_obs = [o for o in all_obs
                   if any(c.get("coding", [{}])[0].get("code") == "laboratory"
                          for c in o.get("category", []))]

    if not lab_obs:
        return file_path.read_text() if file_path.exists() else ""

    # Parse into rows
    rows = parse_lab_observations(lab_obs)
    if not rows:
        return file_path.read_text() if file_path.exists() else ""

    # Group by draw date
    by_date: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        date = row.pop("draw_date", "Unknown")
        # Normalize date to just the date portion
        if "T" in date:
            date = date.split("T")[0]
        by_date[date].append(row)

    # Read or scaffold
    if file_path.exists():
        text = file_path.read_text()
    else:
        text = "# Lab Results\n\nComplete lab history. Updated automatically via chartstash.\n\n---\n\n"

    # For each draw date, find or create a section and merge
    for date, date_rows in sorted(by_date.items()):
        section_heading = f"{date} ({provider_name})"

        # Try to find an existing section for this date
        result = find_section(text, date, level=2)
        if result is None:
            # Also try with provider name
            result = find_section(text, section_heading, level=2)

        if result is not None:
            # Existing section — parse its table and merge
            _, _, content = result
            existing_cols, existing_rows = parse_table(content)
            if existing_rows:
                merged = merge_rows(existing_rows, date_rows, ["Test"])
                table = render_table(LAB_COLUMNS, merged)
            else:
                table = render_table(LAB_COLUMNS, date_rows)

            heading_to_replace = date if find_section(text, date, level=2) else section_heading
            new_content = f"\n**Source:** {provider_name} — FHIR sync\n\n{table}\n"
            from health_sync.writers.markdown import replace_section
            text = replace_section(text, heading_to_replace, new_content, level=2)
        else:
            # New section — append before the final ---
            table = render_table(LAB_COLUMNS, date_rows)
            new_section = f"\n## {section_heading}\n\n**Source:** {provider_name} — FHIR sync\n\n{table}\n\n---\n"
            # Append at the end
            text = text.rstrip() + "\n" + new_section

    text = _sort_lab_sections(text)
    file_path.write_text(text)
    return text
