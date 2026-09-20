from __future__ import annotations

"""Tests for auth/tidepool_credentials.py."""

import os
import stat
from pathlib import Path

import pytest

from health_sync.auth.tidepool_credentials import (
    TidepoolCredentials,
    TidepoolCredentialsError,
    delete_credentials,
    load_credentials,
    save_credentials,
)


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    """Empty secrets dir; the helper creates tokens/{person}/ on demand."""
    return tmp_path / "secrets"


def test_save_creates_file_with_mode_0600(secrets_dir: Path) -> None:
    creds = TidepoolCredentials(email="person_b@example.com", password="secret")
    path = save_credentials("person_b", creds, secrets_dir)

    assert path.exists()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected mode 0o600, got 0o{mode:o}"


def test_save_and_load_roundtrip(secrets_dir: Path) -> None:
    creds = TidepoolCredentials(
        email="person_b@example.com", password="secret", userid="abc123"
    )
    save_credentials("person_b", creds, secrets_dir)
    loaded = load_credentials("person_b", secrets_dir)
    assert loaded.email == "person_b@example.com"
    assert loaded.password == "secret"
    assert loaded.userid == "abc123"


def test_load_missing_file_raises_clear_error(secrets_dir: Path) -> None:
    with pytest.raises(TidepoolCredentialsError) as excinfo:
        load_credentials("person_b", secrets_dir)
    assert "Run: chartstash auth tidepool" in str(excinfo.value)


def test_load_malformed_file_raises(secrets_dir: Path) -> None:
    path = secrets_dir / "tokens" / "person_b" / "tidepool_credentials.json"
    path.parent.mkdir(parents=True)
    path.write_text("not valid json {")
    with pytest.raises(TidepoolCredentialsError):
        load_credentials("person_b", secrets_dir)


def test_delete_returns_true_when_file_existed(secrets_dir: Path) -> None:
    creds = TidepoolCredentials(email="person_b@example.com", password="secret")
    save_credentials("person_b", creds, secrets_dir)
    assert delete_credentials("person_b", secrets_dir) is True
    assert delete_credentials("person_b", secrets_dir) is False  # already gone


def test_save_overwrite_keeps_mode_0600(secrets_dir: Path) -> None:
    creds1 = TidepoolCredentials(email="a@example.com", password="x")
    save_credentials("person_b", creds1, secrets_dir)
    creds2 = TidepoolCredentials(email="b@example.com", password="y")
    path = save_credentials("person_b", creds2, secrets_dir)

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    assert load_credentials("person_b", secrets_dir).email == "b@example.com"
