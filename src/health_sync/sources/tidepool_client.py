from __future__ import annotations

"""Tidepool legacy session-token API client.

Implements DiabetesDataSource against api.tidepool.org. The legacy auth flow:
1. POST /auth/login with HTTP Basic (email:password)
   → response has x-tidepool-session-token header AND a JSON body containing
     userid (which is the data-subject userid for personal-use accounts)
2. GET /data/{userId}?startDate=...&endDate=...&type=... with header
   x-tidepool-session-token
3. POST /auth/logout with the session token (polite)

Endpoint verified against Tidepool docs at tidepool.redocly.app — note the
correct path is `/data/{userId}`, NOT `/data/v1/users/{userid}/data` (the
original plan had this wrong; the Codex review caught it).

Tidepool deprecated this auth path in December 2022 in favor of Keycloak/OIDC,
but as of 2026-05 still serves it. xDrip and AndroidAPS in production also use
it. No published sunset date.
"""

import json
import logging
import time
from datetime import datetime
from typing import Iterator

import httpx

from health_sync.auth.tidepool_credentials import TidepoolCredentials

logger = logging.getLogger(__name__)


class TidepoolAuthError(RuntimeError):
    """Raised when login fails (bad credentials, account locked, 2FA, etc.)."""


class TidepoolFetchError(RuntimeError):
    """Raised when a fetch request fails after retries."""


class TidepoolClient:
    """Legacy session-token API client. Implements DiabetesDataSource."""

    SESSION_TOKEN_HEADER = "x-tidepool-session-token"

    def __init__(
        self,
        credentials: TidepoolCredentials,
        api_base: str = "https://api.tidepool.org",
        rate_limit_seconds: float = 1.0,
        timeout: float = 60.0,
    ) -> None:
        self.credentials = credentials
        self.api_base = api_base.rstrip("/")
        self.rate_limit = rate_limit_seconds
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)
        self._session_token: str | None = None
        self._userid: str | None = None
        self._last_request_time = 0.0

    # --- Protocol contract ---

    def fetch(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> Iterator[dict]:
        """Yield records in [start, end). Logs in on first call.

        Phase 2 callers pass a window; for very large windows the caller is
        responsible for monthly chunking (see loop_engine.sync_tidepool_api).
        """
        if self._session_token is None:
            self._login()
        records = self._fetch_window(start, end)
        for r in records:
            yield r

    def close(self) -> None:
        """Logout (best-effort) and release the HTTP client."""
        if self._session_token is not None:
            self._logout()
        self._client.close()
        self._session_token = None
        self._userid = None

    # --- Internals ---

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit:
            time.sleep(self.rate_limit - elapsed)
        self._last_request_time = time.time()

    def _login(self) -> None:
        """POST /auth/login with HTTP Basic. Captures session token and userid."""
        self._throttle()
        try:
            resp = self._client.post(
                f"{self.api_base}/auth/login",
                auth=(self.credentials.email, self.credentials.password),
            )
        except httpx.HTTPError as e:
            raise TidepoolAuthError(f"Tidepool login network error: {e}") from e

        if resp.status_code == 401:
            raise TidepoolAuthError(
                "Tidepool returned 401 — bad email/password, account locked, "
                "or 2FA enabled. Run: chartstash auth tidepool to re-enter."
            )
        if resp.status_code >= 400:
            raise TidepoolAuthError(
                f"Tidepool login failed: HTTP {resp.status_code} {resp.text[:200]}"
            )

        token = resp.headers.get(self.SESSION_TOKEN_HEADER)
        if not token:
            raise TidepoolAuthError(
                f"Tidepool login succeeded but no {self.SESSION_TOKEN_HEADER} "
                f"header in response"
            )
        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise TidepoolAuthError(f"Tidepool login: malformed JSON body: {e}") from e

        userid = body.get("userid")
        if not userid:
            raise TidepoolAuthError(
                f"Tidepool login body missing 'userid'. Body keys: {list(body)}"
            )

        self._session_token = token
        self._userid = userid
        logger.info(f"Tidepool authenticated; userid={userid}")

    def _logout(self) -> None:
        """POST /auth/logout. Best-effort — failures are logged, not raised."""
        if self._session_token is None:
            return
        try:
            self._throttle()
            resp = self._client.post(
                f"{self.api_base}/auth/logout",
                headers={self.SESSION_TOKEN_HEADER: self._session_token},
            )
            if resp.status_code >= 400:
                logger.warning(
                    f"Tidepool logout returned {resp.status_code} (ignored)"
                )
        except httpx.HTTPError as e:
            logger.warning(f"Tidepool logout network error (ignored): {e}")

    def _fetch_window(
        self, start: datetime | None, end: datetime | None
    ) -> list[dict]:
        """GET /data/{userId} with optional startDate/endDate filters.

        Retries on 429 (Retry-After honored) and timeout (exponential backoff).
        Mirrors the pattern from fhir/client.py:82-111.
        """
        assert self._session_token is not None and self._userid is not None

        params: dict[str, str] = {
            "type": "cbg,smbg,bolus,wizard,basal,food,deviceEvent",
        }
        if start is not None:
            params["startDate"] = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if end is not None:
            params["endDate"] = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        url = f"{self.api_base}/data/{self._userid}"
        headers = {self.SESSION_TOKEN_HEADER: self._session_token}

        for attempt in range(3):
            self._throttle()
            try:
                resp = self._client.get(url, params=params, headers=headers)
            except httpx.TimeoutException:
                if attempt == 2:
                    raise TidepoolFetchError(
                        f"Tidepool fetch timed out after 3 attempts"
                    )
                wait = 2 ** (attempt + 1)
                logger.warning(
                    f"Tidepool fetch timeout, waiting {wait}s "
                    f"(attempt {attempt + 1}/3)"
                )
                time.sleep(wait)
                continue

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                logger.warning(
                    f"Tidepool rate-limited, waiting {wait}s "
                    f"(attempt {attempt + 1}/3)"
                )
                time.sleep(wait)
                continue

            if resp.status_code == 401:
                # Session token expired mid-fetch — try once to re-login
                if attempt < 2:
                    logger.warning(
                        "Tidepool session expired mid-fetch, re-authenticating"
                    )
                    self._session_token = None
                    self._login()
                    headers[self.SESSION_TOKEN_HEADER] = self._session_token  # type: ignore[assignment]
                    continue
                raise TidepoolAuthError(
                    "Tidepool session expired and re-login failed"
                )

            if resp.status_code >= 400:
                raise TidepoolFetchError(
                    f"Tidepool fetch HTTP {resp.status_code}: {resp.text[:200]}"
                )

            data = resp.json()
            if not isinstance(data, list):
                raise TidepoolFetchError(
                    f"Expected JSON array, got {type(data).__name__}"
                )
            logger.info(
                f"Fetched {len(data)} records from Tidepool "
                f"({params.get('startDate', '?')} → {params.get('endDate', '?')})"
            )
            return [_normalize_record(r) for r in data]

        raise TidepoolFetchError("Tidepool fetch failed after 3 attempts")


def _normalize_record(record: dict) -> dict:
    """Normalize an API record to match the export-file unit conventions.

    The Tidepool API and the web-UI JSON export return the same record shapes
    but with different units in a few fields. Most notably, basal.duration is
    in MILLISECONDS in the API (per Tidepool's data-model docs) but in MINUTES
    in the export (empirically verified by comparing adjacent records' gaps).

    This function converts the API representation to match the export's units
    so the downstream parser (parsers/loop.py) sees consistent inputs from
    both sources. Returns a shallow-copied dict; the original is not mutated.
    """
    if record.get("type") == "basal" and "duration" in record:
        duration_ms = record["duration"]
        if isinstance(duration_ms, (int, float)):
            normalized = dict(record)
            normalized["duration"] = duration_ms / 60000.0  # ms → minutes
            return normalized
    return record
