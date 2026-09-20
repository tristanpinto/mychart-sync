"""Retained FHIR records, scoped by person directory, hospital, type, and ID."""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from health_sync.auth.storage import write_private_text

FHIRData = dict[str, list[dict[str, Any]]]
Sources = dict[str, dict[str, Any]]


@contextmanager
def records_lock(root: Path):
    """Serialize a person's syncs so two hospitals cannot overwrite the summary."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "Another sync is running for this person; retry when it finishes."
            ) from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _updated(resource: dict) -> datetime | None:
    value = resource.get("meta", {}).get("lastUpdated")
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Timestamp has no timezone")
        return result
    except (ValueError, AttributeError):
        raise ValueError(
            "Invalid FHIR lastUpdated timestamp; retained records were not changed."
        ) from None


def merge_records(existing: FHIRData, incoming: FHIRData) -> FHIRData:
    """Keep absent IDs, replace updated IDs, and reject unidentified records.

    Search buckets (such as Observation_laboratory) are normalized to resourceType.
    Full searches also retain absent IDs: absence is not evidence of deletion.
    """
    merged: dict[str, dict[str, dict]] = {}
    for data in (existing, incoming):
        if not isinstance(data, dict):
            raise ValueError("Expected a mapping of FHIR resource lists")
        for bucket, resources in data.items():
            if not isinstance(resources, list):
                raise ValueError("Expected a list of FHIR resources")
            for resource in resources:
                if not isinstance(resource, dict):
                    raise ValueError("Expected a FHIR resource object")
                kind = resource.get("resourceType")
                if kind == "OperationOutcome":
                    if any(
                        i.get("severity") in {"error", "fatal"}
                        for i in resource.get("issue", [])
                    ):
                        raise ValueError(
                            "FHIR reported an error; retained records were not changed."
                        )
                    continue
                identifier = resource.get("id")
                if not isinstance(kind, str) or kind != bucket.split("_", 1)[0]:
                    raise ValueError(
                        "Unexpected FHIR resource type; retained records were not changed."
                    )
                if not isinstance(identifier, str) or not identifier.strip():
                    raise ValueError(
                        "FHIR resource has no ID; retained records were not changed."
                    )
                current = merged.setdefault(kind, {}).get(identifier)
                new_date = _updated(resource)
                old_date = _updated(current) if current else None
                if current and old_date and new_date and new_date < old_date:
                    continue
                merged[kind][identifier] = resource
    return {
        kind: [values[key] for key in sorted(values)]
        for kind, values in sorted(merged.items())
    }


def load_sources(root: Path, person: str) -> Sources:
    """Load retained stores, or seed them from old per-type fetch snapshots.

    Legacy snapshots may be incomplete. The first real sync of each hospital
    ignores its old checkpoint and requests all available records.
    """
    sources = {}
    for directory in sorted(root.iterdir()) if root.exists() else []:
        if not directory.is_dir():
            continue
        if directory.is_symlink():
            raise ValueError(
                "Hospital record directories must not be symlinks to other locations."
            )
        path = directory / "records.json"
        if path.exists():
            source = json.loads(path.read_text())
            if not isinstance(source, dict) or source.get("schema") != 1:
                raise ValueError(f"Unrecognized retained-record store: {path}")
            if source.get("person") != person:
                raise ValueError(
                    "Retained records belong to another person; use separate raw/output directories."
                )
            source["resources"] = merge_records({}, source["resources"])
            sources[directory.name] = source
            continue
        data: FHIRData = {}
        for snapshot in sorted(directory.glob("*.json")):
            resources = json.loads(snapshot.read_text())
            if isinstance(resources, dict):
                resources = [resources]
            if not isinstance(resources, list):
                raise ValueError(f"Invalid legacy FHIR snapshot: {snapshot}")
            for resource in resources:
                if not isinstance(resource, dict) or not isinstance(
                    resource.get("resourceType"), str
                ):
                    raise ValueError(f"Invalid legacy FHIR snapshot: {snapshot}")
                kind = resource["resourceType"]
                if kind == "Observation" and snapshot.stem.lower().startswith(
                    "observation_"
                ):
                    kind = "Observation_" + snapshot.stem.split("_", 1)[1]
                data.setdefault(kind, []).append(resource)
        if data:
            sources[directory.name] = {
                "schema": 1,
                "person": person,
                "name": directory.name,
                "resources": merge_records({}, data),
                "full_fetch_complete": False,
                "observation_categories": category_hints(data),
            }
    return sources


def merge_source(
    person: str,
    name: str,
    patient_id: str,
    base_url: str,
    existing: dict,
    incoming: FHIRData,
) -> dict:
    """Prepare one complete fetch; the engine validates its rendering before saving."""
    if existing.get("patient_id", patient_id) != patient_id:
        raise ValueError(
            "Patient identity changed for this hospital; use a separate person/output directory."
        )
    if existing.get("base_url", base_url).rstrip("/") != base_url.rstrip("/"):
        raise ValueError(
            "FHIR endpoint changed for this hospital; review its retained records before syncing."
        )
    resources = merge_records(existing.get("resources", {}), incoming)
    if len(incoming.get("Patient", [])) != 1:
        raise ValueError(
            "Expected one Patient in the completed fetch; retained records were not changed."
        )
    if any(r["id"] != patient_id for r in resources.get("Patient", [])):
        raise ValueError(
            "Fetched Patient does not match the authorized patient; retained records were not changed."
        )
    source = {
        "schema": 1,
        "person": person,
        "name": name,
        "patient_id": patient_id,
        "base_url": base_url.rstrip("/"),
        "resources": resources,
        "full_fetch_complete": existing.get("full_fetch_complete", False),
        "observation_categories": {
            **existing.get("observation_categories", {}),
            **category_hints(incoming, resources.get("Observation", [])),
        },
    }
    return source


def save_source(root: Path, slug: str, source: dict) -> None:
    write_private_text(
        root / slug / "records.json",
        json.dumps(source, indent=2, sort_keys=True) + "\n",
    )


def category_hints(
    data: FHIRData, selected: list[dict] | None = None
) -> dict[str, list[str]]:
    """Keep search provenance separately when an Observation omits its category."""
    hints: dict[str, set[str]] = {}
    selected_by_id = {r["id"]: r for r in selected} if selected is not None else None
    for bucket, resources in data.items():
        if bucket.startswith("Observation_"):
            for resource in resources:
                if resource.get("resourceType") == "Observation":
                    if selected_by_id is not None and resource != selected_by_id.get(
                        resource["id"]
                    ):
                        continue
                    hints.setdefault(resource["id"], set()).add(bucket.split("_", 1)[1])
    return {key: sorted(values) for key, values in hints.items()}


def observations(
    data: FHIRData, category: str, hints: dict | None = None
) -> list[dict]:
    """Select a category, retaining compatibility with older parser callers."""
    result = {
        r.get("id", str(i)): r
        for i, r in enumerate(data.get(f"Observation_{category}", []))
    }
    for resource in data.get("Observation", []):
        codes = [
            coding.get("code")
            for c in resource.get("category", [])
            for coding in c.get("coding", [])
        ]
        if category in codes or (
            not codes and category in (hints or {}).get(resource.get("id"), [])
        ):
            result[resource["id"]] = resource
    return list(result.values())
