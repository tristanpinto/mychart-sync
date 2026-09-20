from __future__ import annotations

"""Persistent storage for Tidepool legacy-auth credentials.

Stores email + password at `secrets/tokens/{person}/tidepool_credentials.json`.
Unlike the existing TokenStore for Epic OAuth (which doesn't set file mode),
this module EXPLICITLY chmods the file to 0600 after writing — these are
plaintext passwords, not OAuth refresh tokens, so file-mode discipline matters.

Phase 2 stores a small profile cache alongside credentials (firstname/lastname/
birthday or whatever the Tidepool profile endpoint returns) so we can verify
on each sync that the credentials still belong to the same data subject —
guard against the "user accidentally entered the wrong account's password"
case (Codex review finding #3).
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write then chmod — atomic enough for our single-writer use case.
    path.write_text(credentials.model_dump_json(indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger.warning(f"Could not chmod 0600 on {path}: {e}")
    return path


def load_credentials(person: str, secrets_dir: Path) -> TidepoolCredentials:
    """Read credentials from disk. Raises if missing or malformed."""
    path = _path_for(person, secrets_dir)
    if not path.exists():
        raise TidepoolCredentialsError(
            f"No Tidepool credentials for '{person}' at {path}. "
            f"Run: chartstash auth tidepool --person {person}"
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise TidepoolCredentialsError(
            f"Could not load Tidepool credentials for '{person}': {e}"
        ) from e
    return TidepoolCredentials(**data)


def delete_credentials(person: str, secrets_dir: Path) -> bool:
    """Remove credentials file. Returns True if a file was deleted."""
    path = _path_for(person, secrets_dir)
    if path.exists():
        path.unlink()
        return True
    return False
