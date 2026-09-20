from __future__ import annotations

"""Person-scoped provider token storage and refresh.

Tokens are stored as JSON files under secrets/tokens/{person}/{slug}_token.json.
Handles automatic refresh when access tokens expire.
"""

import base64
import time
from pathlib import Path
from typing import Optional

import httpx
from pydantic import BaseModel, ValidationError

from health_sync.auth.storage import write_private_text


class StoredToken(BaseModel):
    """Persisted token data for a single provider."""

    provider_slug: str
    person: str
    fhir_base_url: str
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: float  # Unix timestamp
    patient_id: str
    token_endpoint: str
    scope: str = ""

    def is_expired(self, buffer_seconds: int = 60) -> bool:
        """Check if the access token is expired (with buffer)."""
        return time.time() >= (self.expires_at - buffer_seconds)


class TokenStore:
    """Manages token persistence and refresh for one person's token directory."""

    def __init__(self, tokens_dir: Path) -> None:
        self.tokens_dir = tokens_dir
        self.person = tokens_dir.name
        self.tokens_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.tokens_dir.chmod(0o700)

    def _token_path(self, slug: str) -> Path:
        return self.tokens_dir / f"{slug}_token.json"

    def save(self, token: StoredToken) -> None:
        """Save a token to disk."""
        self._validate_person(token)
        path = self._token_path(token.provider_slug)
        write_private_text(path, token.model_dump_json(indent=2))

    def load(self, slug: str) -> Optional[StoredToken]:
        """Load a token from disk. Returns None if not found."""
        path = self._token_path(slug)
        if not path.exists():
            return None
        try:
            token = StoredToken.model_validate_json(path.read_text())
        except ValidationError:
            raise RuntimeError(
                f"Invalid token file for '{slug}'. Run: mychart-sync auth {slug}"
            ) from None
        self._validate_person(token)
        return token

    def _validate_person(self, token: StoredToken) -> None:
        if token.person != self.person:
            raise RuntimeError(
                f"Token for '{token.provider_slug}' belongs to '{token.person}', "
                f"not '{self.person}'"
            )

    def delete(self, slug: str) -> None:
        """Delete a token file."""
        path = self._token_path(slug)
        if path.exists():
            path.unlink()

    def list_authenticated(self) -> list[str]:
        """Return slugs of all providers with stored tokens."""
        return [
            p.stem.removesuffix("_token")
            for p in self.tokens_dir.glob("*_token.json")
        ]

    def get_valid_token(
        self, slug: str, client_id: str, client_secret: Optional[str] = None
    ) -> StoredToken:
        """Refresh an expired token; raise RuntimeError if missing or refresh fails."""
        token = self.load(slug)
        if token is None:
            raise RuntimeError(
                f"No token found for '{slug}'. Run: mychart-sync auth {slug}"
            )

        if not token.is_expired():
            return token

        if not token.refresh_token:
            raise RuntimeError(
                f"Token expired for '{slug}' and no refresh token available. "
                f"Run: mychart-sync auth {slug}"
            )

        return self._refresh(token, client_id, client_secret)

    def _refresh(
        self, token: StoredToken, client_id: str, client_secret: Optional[str] = None
    ) -> StoredToken:
        """Refresh an expired access token."""
        refresh_data = {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
        }
        refresh_headers = {"Content-Type": "application/x-www-form-urlencoded"}

        if client_secret:
            # Confidential client: Basic auth header
            credentials = base64.b64encode(
                f"{client_id}:{client_secret}".encode()
            ).decode()
            refresh_headers["Authorization"] = f"Basic {credentials}"
        else:
            # Public client: client_id in body
            refresh_data["client_id"] = client_id

        resp = httpx.post(
            token.token_endpoint,
            data=refresh_data,
            headers=refresh_headers,
            timeout=15,
        )

        if resp.status_code == 401 or resp.status_code == 400:
            raise RuntimeError(
                f"Refresh token expired for '{token.provider_slug}'. "
                f"Run: mychart-sync auth {token.provider_slug}"
            )

        resp.raise_for_status()
        data = resp.json()

        refreshed = StoredToken(
            provider_slug=token.provider_slug,
            person=token.person,
            fhir_base_url=token.fhir_base_url,
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", token.refresh_token),
            expires_at=time.time() + data.get("expires_in", 3600),
            patient_id=data.get("patient", token.patient_id),
            token_endpoint=token.token_endpoint,
            scope=data.get("scope", token.scope),
        )

        self.save(refreshed)
        return refreshed
