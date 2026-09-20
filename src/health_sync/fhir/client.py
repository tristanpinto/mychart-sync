from __future__ import annotations

"""FHIR R4 HTTP client with pagination, rate limiting, and error handling."""

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# FHIR resource types we fetch for patient records
RESOURCE_TYPES = [
    "Patient",
    "Condition",
    "MedicationRequest",
    "AllergyIntolerance",
    "Immunization",
    "Observation",
    "Encounter",
    "Procedure",
    "CareTeam",
    "DiagnosticReport",
    "DocumentReference",
]


class FHIRClient:
    """FHIR R4 client for patient data access."""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        patient_id: str,
        cache_dir: Path | None = None,
        rate_limit: float = 1.0,
        timeout: float = 120.0,
    ) -> None:
        """
        Args:
            base_url: FHIR R4 base URL (e.g. https://fhir.epic.com/.../api/FHIR/R4).
            access_token: OAuth2 bearer token.
            patient_id: FHIR Patient ID from token response.
            cache_dir: Directory to cache raw responses (optional).
            rate_limit: Minimum seconds between requests.
            timeout: Seconds to wait for FHIR responses.
        """
        self.base_url = base_url.rstrip("/")
        self.patient_id = patient_id
        self.cache_dir = cache_dir
        self.rate_limit = rate_limit
        self._last_request_time = 0.0

        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/fhir+json",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _throttle(self) -> None:
        """Enforce rate limiting between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self._last_request_time = time.time()

    def _request(self, url: str, params: dict | None = None) -> httpx.Response:
        """Make a throttled GET request with retry on 429."""
        self._throttle()

        for attempt in range(3):
            try:
                resp = self._client.get(url, params=params)
            except httpx.TimeoutException:
                if attempt == 2:
                    raise
                wait = 2 ** (attempt + 1)
                logger.warning(
                    f"FHIR request timed out, waiting {wait}s (attempt {attempt + 1})"
                )
                time.sleep(wait)
                continue

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                logger.warning(f"Rate limited, waiting {wait}s (attempt {attempt + 1})")
                time.sleep(wait)
                continue

            if resp.status_code == 401:
                raise TokenExpiredError("Access token expired during request")

            resp.raise_for_status()
            return resp

        raise RuntimeError(f"Request failed after 3 retries: {url}")

    def fetch_resource(
        self,
        resource_type: str,
        params: dict[str, str] | None = None,
        since: str | None = None,
        cache_key: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all entries of a resource type for the patient.

        Handles pagination automatically via Bundle.link[next].

        Args:
            resource_type: FHIR resource type (e.g. "Condition").
            params: Additional search parameters.
            since: Only fetch resources updated after this ISO datetime
                   (uses _lastUpdated parameter).
            cache_key: Override cache filename (e.g. "observation_laboratory").

        Returns:
            List of FHIR resource dicts (entries from all pages).
        """
        search_params = {"patient": self.patient_id}
        if params:
            search_params.update(params)
        if since:
            search_params["_lastUpdated"] = f"gt{since}"

        # Observation needs category filtering for useful results
        # Caller should specify category for Observation searches

        url = f"{self.base_url}/{resource_type}"
        all_entries: list[dict[str, Any]] = []
        page = 0

        while url:
            resp = self._request(url, params=search_params if page == 0 else None)
            bundle = resp.json()

            if bundle.get("resourceType") != "Bundle":
                logger.warning(f"Expected Bundle, got {bundle.get('resourceType')}")
                break

            for entry in bundle.get("entry", []):
                resource = entry.get("resource", {})
                if resource:
                    all_entries.append(resource)

            # Follow pagination
            url = None
            for link in bundle.get("link", []):
                if link.get("relation") == "next":
                    url = link["url"]
                    break

            page += 1

        logger.info(f"Fetched {len(all_entries)} {resource_type} resources ({page} pages)")

        # Cache raw response
        if self.cache_dir and all_entries:
            fname = cache_key or resource_type.lower()
            cache_file = self.cache_dir / f"{fname}.json"
            cache_file.write_text(json.dumps(all_entries, indent=2))

        return all_entries

    def fetch_patient(self) -> dict[str, Any]:
        """Fetch the Patient resource directly (not a search)."""
        self._throttle()
        resp = self._request(f"{self.base_url}/Patient/{self.patient_id}")
        resource = resp.json()

        if self.cache_dir:
            cache_file = self.cache_dir / "patient.json"
            cache_file.write_text(json.dumps(resource, indent=2))

        return resource

    def fetch_binary(self, binary_url: str) -> bytes:
        """Fetch binary content (clinical notes, documents) by URL.

        The binary_url is typically a relative path like "Binary/{id}"
        from a DocumentReference's content.attachment.url.

        Args:
            binary_url: The Binary resource URL (relative or absolute).

        Returns:
            Raw content bytes.
        """
        # Handle relative URLs
        if not binary_url.startswith("http"):
            url = f"{self.base_url}/{binary_url}"
        else:
            url = binary_url

        self._throttle()
        resp = self._client.get(url, timeout=30)
        if resp.status_code == 401:
            raise TokenExpiredError("Access token expired during binary fetch")
        resp.raise_for_status()
        return resp.content

    def fetch_all(self, since: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """Fetch all supported resource types for the patient.

        Args:
            since: Only fetch resources updated after this ISO datetime.

        Returns:
            Dict mapping resource type names to lists of resources.
        """
        results: dict[str, list[dict[str, Any]]] = {}

        # Patient is fetched directly, not via search
        results["Patient"] = [self.fetch_patient()]

        # Observation needs category-specific fetches
        observation_categories = [
            ("laboratory", "Observation_laboratory"),
            ("vital-signs", "Observation_vital-signs"),
            ("social-history", "Observation_social-history"),
        ]

        for resource_type in RESOURCE_TYPES:
            if resource_type == "Patient":
                continue  # Already fetched

            if resource_type == "Observation":
                for category, key in observation_categories:
                    results[key] = self.fetch_resource(
                        "Observation",
                        params={"category": category},
                        since=since,
                        cache_key=key.lower(),
                    )
                continue

            results[resource_type] = self.fetch_resource(
                resource_type, since=since
            )

        return results


class TokenExpiredError(Exception):
    """Raised when the access token has expired and needs refresh."""

    pass
