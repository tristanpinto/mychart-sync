from __future__ import annotations

"""Tidepool legacy session-token API client.

This deprecated login method requires the user's password. Session tokens
stay in memory; a saved user ID pins subsequent logins to the same account.
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
    """Fetch personal records using a legacy Tidepool session."""

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

        The sync engine splits large windows into monthly requests.
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
                "or 2FA enabled. Run: mychart-sync auth tidepool to re-enter."
            )
        if resp.status_code >= 400:
            raise TidepoolAuthError(
                f"Tidepool login failed: HTTP {resp.status_code}"
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

        userid = body.get("userid") if isinstance(body, dict) else None
        if not isinstance(userid, str) or not userid:
            raise TidepoolAuthError(
                "Tidepool login response missing a valid 'userid'"
            )

        self._session_token = token
        if self.credentials.userid and userid != self.credentials.userid:
            self._logout()
            self._session_token = None
            raise TidepoolAuthError(
                "Tidepool account does not match the saved user ID. "
                "Check the account before authenticating again."
            )
        self._userid = userid
        logger.info("Tidepool authenticated")

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
            except httpx.HTTPError as e:
                raise TidepoolFetchError("Tidepool fetch failed because of a network error") from e

            if resp.status_code == 429:
                if attempt == 2:
                    break
                try:
                    wait = max(0, int(resp.headers.get("Retry-After", 2 ** (attempt + 1))))
                except ValueError:
                    wait = 2 ** (attempt + 1)
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
                    f"Tidepool fetch HTTP {resp.status_code}"
                )

            try:
                data = resp.json()
            except ValueError as e:
                raise TidepoolFetchError("Tidepool fetch returned invalid JSON") from e
            if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
                raise TidepoolFetchError(
                    "Tidepool fetch must return a JSON array of records"
                )
            logger.info(
                f"Fetched {len(data)} records from Tidepool "
                f"({params.get('startDate', '?')} → {params.get('endDate', '?')})"
            )
            return [_normalize_record(r) for r in data]

        raise TidepoolFetchError("Tidepool fetch failed after 3 attempts")


def _normalize_record(record: dict) -> dict:
    """Convert API basal milliseconds to the export/parser's minutes.

    Export units were verified against gaps between adjacent basal records.
    Copy changed records so the input is not mutated.
    """
    if record.get("type") == "basal" and "duration" in record:
        duration_ms = record["duration"]
        if isinstance(duration_ms, (int, float)):
            normalized = dict(record)
            normalized["duration"] = duration_ms / 60000.0  # ms → minutes
            return normalized
    return record
