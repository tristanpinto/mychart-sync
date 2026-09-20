from __future__ import annotations

"""Epic FHIR endpoint registry.

Provides search and validation for Epic's 1,178+ published FHIR endpoints.
The endpoint list can be loaded from a local cache or fetched from Epic's
open directory.
"""

import json
import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Epic publishes endpoints at this URL
EPIC_ENDPOINTS_URL = "https://open.epic.com/MyApps/EndpointsJson"

# Sandbox endpoint for testing
SANDBOX_ENDPOINT = {
    "slug": "epic-sandbox",
    "name": "Epic Sandbox (Test Data)",
    "fhir_base_url": "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
}


def load_endpoint_directory(cache_dir: Path) -> list[dict[str, Any]]:
    """Load the Epic endpoint directory, using cache if available.

    The directory is a JSON array of objects with OrganizationName,
    FHIRPatientFacingURI, and other fields.
    """
    cache_file = cache_dir / "epic_endpoints.json"

    if cache_file.exists():
        data = json.loads(cache_file.read_text())
        # Handle both raw list and {Entries: [...]} wrapper
        if isinstance(data, dict) and "Entries" in data:
            return data["Entries"]
        return data if isinstance(data, list) else []

    logger.info("Fetching Epic endpoint directory...")
    try:
        resp = httpx.get(EPIC_ENDPOINTS_URL, timeout=30)
        resp.raise_for_status()
        raw = resp.json()
        cache_file.write_text(json.dumps(raw, indent=2))
        # Handle both formats
        if isinstance(raw, dict) and "Entries" in raw:
            endpoints = raw["Entries"]
        elif isinstance(raw, list):
            endpoints = raw
        else:
            endpoints = []
        logger.info(f"Cached {len(endpoints)} endpoints")
        return endpoints
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        logger.warning(f"Could not fetch endpoint directory: {e}")
        return []


def search_endpoints(
    query: str,
    cache_dir: Path,
    limit: int = 10,
) -> list[dict[str, str]]:
    """Search for Epic FHIR endpoints by organization name.

    Returns a list of dicts with 'name' and 'fhir_base_url' keys.
    """
    directory = load_endpoint_directory(cache_dir)
    query_lower = query.lower()

    matches = []
    for entry in directory:
        org_name = entry.get("OrganizationName", "")
        fhir_url = entry.get("FHIRPatientFacingURI", "")
        if query_lower in org_name.lower() and fhir_url:
            matches.append({
                "name": org_name,
                "fhir_base_url": fhir_url.rstrip("/"),
            })

    # Sort by relevance: exact prefix match first, then alphabetical
    matches.sort(key=lambda m: (not m["name"].lower().startswith(query_lower), m["name"]))
    return matches[:limit]


def validate_endpoint(fhir_base_url: str) -> bool:
    """Validate that a FHIR base URL responds with a CapabilityStatement."""
    try:
        resp = httpx.get(
            f"{fhir_base_url.rstrip('/')}/metadata",
            headers={"Accept": "application/fhir+json"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("resourceType") == "CapabilityStatement"
    except (httpx.HTTPError, json.JSONDecodeError):
        pass
    return False
