from __future__ import annotations

"""FHIR R4 HTTP client with pagination, rate limiting, and error handling."""

import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

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
        """rate_limit and timeout are in seconds; cache_dir saves raw snapshots."""
        self.base_url = base_url.rstrip("/")
        self.patient_id = patient_id
        self.cache_dir = cache_dir
        self.rate_limit = rate_limit
        self._last_request_time = 0.0

        self._client = httpx.Client(
            headers={"Accept": "application/fhir+json"},
            timeout=timeout,
        )
        self._authorization = f"Bearer {access_token}"

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

    def _same_origin(self, url: str) -> bool:
        target, base = httpx.URL(url), httpx.URL(self.base_url)
        return (target.scheme, target.host, target.port) == (base.scheme, base.host, base.port)

    def _request(
        self, url: str, params: dict | None = None, *, authenticated: bool = True
    ) -> httpx.Response:
        """Make a throttled GET request with retry on 429."""
        target = httpx.URL(url)
        if target.scheme != "https" or not target.host or target.userinfo:
            raise ValueError("FHIR requests require an HTTPS URL without embedded credentials")
        if authenticated and not self._same_origin(url):
            raise ValueError("Refusing to send a hospital token to a different origin")
        headers = {"Authorization": self._authorization} if authenticated else {}
        self._throttle()

        for attempt in range(3):
            try:
                resp = self._client.get(url, params=params, headers=headers)
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
        """Follow all result pages; since filters by the FHIR _lastUpdated timestamp."""
        search_params = {"patient": self.patient_id}
        if params:
            search_params.update(params)
        if since:
            search_params["_lastUpdated"] = f"gt{since}"

        url = f"{self.base_url}/{resource_type}"
        all_entries: list[dict[str, Any]] = []
        page = 0
        visited = set()

        while url:
            if url in visited:
                raise RuntimeError("FHIR pagination repeated a page; sync is incomplete")
            visited.add(url)
            resp = self._request(url, params=search_params if page == 0 else None)
            bundle = resp.json()

            if bundle.get("resourceType") != "Bundle":
                raise RuntimeError(f"Expected a FHIR Bundle for {resource_type}; sync is incomplete")

            for entry in bundle.get("entry", []):
                resource = entry.get("resource", {})
                if resource.get("resourceType") == "OperationOutcome" and any(
                    issue.get("severity") in {"error", "fatal"}
                    for issue in resource.get("issue", [])
                ):
                    raise RuntimeError(f"FHIR reported an error for {resource_type}; sync is incomplete")
                if resource:
                    all_entries.append(resource)

            # Follow pagination
            url = None
            for link in bundle.get("link", []):
                if link.get("relation") == "next":
                    url = urljoin(str(resp.url), link["url"])
                    break

            page += 1

        logger.info(f"Fetched {len(all_entries)} {resource_type} resources ({page} pages)")

        # Cache raw response
        if self.cache_dir and (all_entries or since is None):
            fname = cache_key or resource_type.lower()
            cache_file = self.cache_dir / f"{fname}.json"
            cache_file.write_text(json.dumps(all_entries, indent=2))

        return all_entries

    def fetch_patient(self) -> dict[str, Any]:
        """Fetch the Patient resource directly (not a search)."""
        resp = self._request(f"{self.base_url}/Patient/{self.patient_id}")
        resource = resp.json()
        if resource.get("resourceType") != "Patient":
            raise RuntimeError("Expected a FHIR Patient; sync is incomplete")

        if self.cache_dir:
            cache_file = self.cache_dir / "patient.json"
            cache_file.write_text(json.dumps(resource, indent=2))

        return resource

    def fetch_binary(self, binary_url: str) -> bytes:
        """Fetch a relative or absolute attachment URL without leaking hospital tokens."""
        url = urljoin(f"{self.base_url}/", binary_url)
        # External attachments may be public/signed URLs, but never receive the token.
        resp = self._request(url, authenticated=self._same_origin(url))
        return resp.content

    def fetch_all(self, since: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """Fetch supported resources, querying observations separately by category."""
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
