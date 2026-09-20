from __future__ import annotations

"""Render lab results from retained records, preserving separate result IDs."""

from collections import defaultdict

from health_sync.parsers.observations import LAB_COLUMNS, parse_lab_observations
from health_sync.sync.records import Sources, observations
from health_sync.writers.clinical_extract import PROVENANCE, sourced_rows
from health_sync.writers.markdown import render_table


def render_lab_results(sources: Sources) -> str:
    by_date = defaultdict(list)
    for slug, source in sorted(sources.items()):
        name = f"{source['name']} ({slug})" if source["name"] != slug else slug
        for resource in observations(
            source["resources"], "laboratory", source.get("observation_categories")
        ):
            for row in sourced_rows(parse_lab_observations, [resource], name):
                timestamp = row.pop("draw_date") or "Unknown"
                row["Collected"] = timestamp
                row["Status"] = resource.get("status", "")
                by_date[timestamp.split("T")[0]].append(row)
    parts = [
        "# Lab Results",
        "Generated from retained FHIR JSON. Separate results and hospital sources are kept, including corrections and non-final statuses.",
    ]
    for day, rows in sorted(by_date.items()):
        parts.extend(
            [
                f"## {day}",
                render_table(LAB_COLUMNS + ["Collected", "Status"] + PROVENANCE, rows),
            ]
        )
    if not by_date:
        parts.append("No laboratory results downloaded.")
    return "\n\n".join(parts) + "\n"
