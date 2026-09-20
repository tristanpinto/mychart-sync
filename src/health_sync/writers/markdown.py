"""Render Markdown tables without letting cell contents break their structure."""


def render_table(columns: list[str], rows: list[dict[str, str]]) -> str:
    if not columns:
        return ""

    lines = [
        "| " + " | ".join(columns) + " |",
        "|" + "|".join("---" for _ in columns) + "|",
    ]
    for row in rows:
        cells = [
            row.get(col, "")
            .replace("|", "&#124;")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", "<br>")
            for col in columns
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
