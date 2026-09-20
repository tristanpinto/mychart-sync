from __future__ import annotations

"""Private storage for Tidepool login credentials and optional account identity."""

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ValidationError

from health_sync.auth.storage import write_private_text


class TidepoolCredentials(BaseModel):
    """Plaintext Tidepool email + password and an optional cached profile."""

    email: str
    password: str
    userid: Optional[str] = None
    # Profile cache for identity verification (set on save after a verify roundtrip)
    profile_fullname: Optional[str] = None
    profile_birthday: Optional[str] = None


class TidepoolCredentialsError(RuntimeError):
    """Raised when credentials are missing, malformed, or fail identity verification."""


def _path_for(person: str, secrets_dir: Path) -> Path:
    return secrets_dir / "tokens" / person / "tidepool_credentials.json"


def save_credentials(
    person: str, credentials: TidepoolCredentials, secrets_dir: Path
) -> Path:
    """Write credentials to disk with mode 0600.

    Returns the path written.
    """
    path = _path_for(person, secrets_dir)
    write_private_text(path, credentials.model_dump_json(indent=2))
    return path


def load_credentials(person: str, secrets_dir: Path) -> TidepoolCredentials:
    """Read credentials from disk. Raises if missing or malformed."""
    path = _path_for(person, secrets_dir)
    if not path.exists():
        raise TidepoolCredentialsError(
            f"No Tidepool credentials for '{person}' at {path}. "
            f"Run: mychart-sync auth tidepool --person {person}"
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise TidepoolCredentialsError(
            f"Could not load Tidepool credentials for '{person}': {e}"
        ) from e
    try:
        return TidepoolCredentials.model_validate(data)
    except ValidationError:
        raise TidepoolCredentialsError(
            f"Invalid Tidepool credentials for '{person}'. "
            f"Run: mychart-sync auth tidepool --person {person}"
        ) from None


def delete_credentials(person: str, secrets_dir: Path) -> bool:
    """Remove credentials file. Returns True if a file was deleted."""
    path = _path_for(person, secrets_dir)
    if path.exists():
        path.unlink()
        return True
    return False
