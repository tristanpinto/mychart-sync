from __future__ import annotations

"""Core markdown table utilities for reading, merging, and writing markdown tables.

These utilities power all output file writers — they parse existing markdown tables,
merge new rows without duplicating or losing manual entries, and render back to
markdown.
"""

import re
from datetime import datetime
from typing import Optional


def _normalize_date(value: str) -> str:
    """Normalize a date string for comparison.

    Handles:
    - 3/23/2026, 2026-03-23, 2026-03-23T14:16:31Z
    - Date ranges: "2026-02-25T02:29:00Z - 2026-02-27T20:45:00Z"
    - Human date ranges: "1/1/2020 9:00 AM - 1/3/2020 3:00 PM"
    - Dates with time: "3/12/2026 12:15 PM"

    Returns YYYY-MM-DD format (start date only for ranges),
    or the original string lowercased if unparseable.
    """
    v = value.strip()
    if not v:
        return v

    # Handle date ranges — extract start date only (before " - ")
    if " - " in v:
        v = v.split(" - ")[0].strip()

    # Strip time portion for dates with spaces (e.g., "3/12/2026 12:15 PM")
    # But preserve ISO T-separator handling below
    if "T" not in v and " " in v:
        # Likely "M/D/YYYY H:MM AM" or similar — take just the date part
        date_part = v.split(" ")[0]
        # Only use if it looks like a date (contains / or -)
        if "/" in date_part or (date_part.count("-") >= 2):
            v = date_part

    # Strip ISO time component
    if "T" in v:
        v = v.split("T")[0]

    # Try ISO format first: 2026-03-23
    try:
        d = datetime.strptime(v, "%Y-%m-%d")
        return d.strftime("%Y-%m-%d")
    except ValueError:
        pass

    # Try US format: 3/23/2026 or 03/23/2026
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            d = datetime.strptime(v, fmt)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return value.strip().lower()


def parse_table(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """Parse a markdown table into column names and row dicts.

    Args:
        text: Markdown text containing a table (header row, separator row, data rows).

    Returns:
        (columns, rows) where columns is a list of header names and rows is a
        list of dicts mapping column name → cell value.
        Returns ([], []) if no table is found.
    """
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]

    # Find header row (contains |)
    table_start = None
    for i, line in enumerate(lines):
        if "|" in line and i + 1 < len(lines) and re.match(r"^\|?[\s\-:|]+\|", lines[i + 1]):
            table_start = i
            break

    if table_start is None:
        return [], []

    # Parse header
    header_line = lines[table_start]
    columns = [c.strip() for c in header_line.split("|") if c.strip()]

    # Parse data rows (skip separator)
    rows = []
    for line in lines[table_start + 2:]:
        if not line.startswith("|") and "|" not in line:
            break
        cells = [c.strip() for c in line.split("|")]
        # Remove empty first/last from leading/trailing |
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]

        row = {}
        for j, col in enumerate(columns):
            row[col] = cells[j] if j < len(cells) else ""
        rows.append(row)

    return columns, rows


def render_table(columns: list[str], rows: list[dict[str, str]]) -> str:
    """Render a list of row dicts back to a markdown table.

    Args:
        columns: Column names (determines order).
        rows: List of dicts mapping column name → cell value.

    Returns:
        Markdown table string.
    """
    if not columns:
        return ""

    # Header
    header = "| " + " | ".join(columns) + " |"
    separator = "|" + "|".join("---" for _ in columns) + "|"

    # Data rows
    data_lines = []
    for row in rows:
        cells = [row.get(col, "") for col in columns]
        data_lines.append("| " + " | ".join(cells) + " |")

    return "\n".join([header, separator] + data_lines)


def merge_rows(
    existing: list[dict[str, str]],
    new: list[dict[str, str]],
    key_cols: list[str],
) -> list[dict[str, str]]:
    """Merge new rows into existing rows, deduplicating by key columns.

    Existing rows are preserved. New rows are only added if no existing row
    matches on all key columns. This ensures manual entries are never overwritten.

    Args:
        existing: Current rows from the output file.
        new: New rows from FHIR data.
        key_cols: Column names used for deduplication.

    Returns:
        Merged list with existing rows first, then new unique rows.
    """
    # Columns likely to contain dates (for normalization)
    date_cols = {"Started", "Date", "Onset", "End Date", "draw_date"}

    def normalize_val(col: str, val: str) -> str:
        v = val.strip().lower()
        if col in date_cols:
            return _normalize_date(val)
        return v

    def row_key(row: dict[str, str]) -> tuple:
        return tuple(normalize_val(c, row.get(c, "")) for c in key_cols)

    existing_keys = {row_key(r) for r in existing}

    merged = list(existing)
    for row in new:
        if row_key(row) not in existing_keys:
            merged.append(row)
            existing_keys.add(row_key(row))

    return merged


def find_section(text: str, heading: str, level: int = 2) -> Optional[tuple[int, int, str]]:
    """Find a markdown section by heading.

    Args:
        text: Full markdown text.
        heading: The heading text to find (without # prefix).
        level: Heading level (2 = ##, 3 = ###).

    Returns:
        (start_idx, end_idx, content) where start is the position of the heading line,
        end is the start of the next same-or-higher-level heading (or end of text),
        and content is the text between them.
        Returns None if heading not found.
    """
    prefix = "#" * level
    pattern = re.compile(
        rf"^{prefix}\s+{re.escape(heading)}\s*$",
        re.MULTILINE,
    )

    match = pattern.search(text)
    if not match:
        return None

    start = match.start()
    heading_end = match.end()

    # Find next heading at same or higher level
    next_heading = re.compile(rf"^#{{{1},{level}}}\s+", re.MULTILINE)
    next_match = next_heading.search(text, heading_end)

    if next_match:
        end = next_match.start()
    else:
        end = len(text)

    content = text[heading_end:end]
    return start, end, content


def replace_section(text: str, heading: str, new_content: str, level: int = 2) -> str:
    """Replace the content of a markdown section.

    Preserves the heading line, replaces everything between it and the next
    heading at the same or higher level.

    Args:
        text: Full markdown text.
        heading: The heading text to find.
        new_content: New content (should start with newline).
        level: Heading level.

    Returns:
        Updated markdown text.
    """
    result = find_section(text, heading, level)
    if result is None:
        return text

    start, end, _ = result
    prefix = "#" * level
    heading_line = f"{prefix} {heading}"

    return text[:start] + heading_line + "\n" + new_content + "\n" + text[end:]


def update_table_in_section(
    text: str,
    heading: str,
    columns: list[str],
    new_rows: list[dict[str, str]],
    key_cols: list[str],
    level: int = 2,
) -> str:
    """Find a section, parse its table, merge new rows, and write back.

    This is the main entry point for updating output files. It:
    1. Finds the section by heading
    2. Parses the existing table
    3. Merges new rows (dedup by key_cols)
    4. Renders the updated table back into the section

    Args:
        text: Full markdown text of the output file.
        heading: Section heading containing the table.
        columns: Expected column names.
        new_rows: New rows to merge in.
        key_cols: Columns used for deduplication.
        level: Heading level.

    Returns:
        Updated markdown text.
    """
    result = find_section(text, heading, level)
    if result is None:
        return text

    _, _, section_content = result

    # Parse existing table in this section
    existing_cols, existing_rows = parse_table(section_content)

    # If there's an existing table, use its columns to preserve order
    if existing_cols:
        # Merge any new columns not in existing
        merged_cols = list(existing_cols)
        for c in columns:
            if c not in merged_cols:
                merged_cols.append(c)
    else:
        merged_cols = columns

    # Merge rows
    merged = merge_rows(existing_rows, new_rows, key_cols)

    # Render new table
    table = render_table(merged_cols, merged)

    # Preserve non-table content in the section (text before/after the table)
    # Find where the table starts and ends in the section
    table_lines = []
    non_table_before = []
    non_table_after = []
    in_table = False
    past_table = False

    for line in section_content.split("\n"):
        stripped = line.strip()
        if not in_table and not past_table and "|" in stripped and "---" not in stripped:
            in_table = True
        if in_table:
            if stripped == "" or ("|" not in stripped and "---" not in stripped):
                in_table = False
                past_table = True
                non_table_after.append(line)
            else:
                table_lines.append(line)
        elif past_table:
            non_table_after.append(line)
        else:
            non_table_before.append(line)

    before_text = "\n".join(non_table_before).strip()
    after_text = "\n".join(non_table_after).strip()

    new_section = ""
    if before_text:
        new_section += before_text + "\n\n"
    new_section += table + "\n"
    if after_text:
        new_section += "\n" + after_text + "\n"

    return replace_section(text, heading, "\n" + new_section, level)
