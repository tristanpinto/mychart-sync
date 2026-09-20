from __future__ import annotations

"""Tests for sources/tidepool_client.py.

Uses pytest-httpx to mock api.tidepool.org endpoints.
"""

import json
import re

import httpx
import pytest

from health_sync.auth.tidepool_credentials import TidepoolCredentials
from health_sync.sources.tidepool_client import (
    TidepoolAuthError,
    TidepoolClient,
    TidepoolFetchError,
)


API_BASE = "https://api.tidepool.org"
SESSION_TOKEN = "fake-session-token-abc"
USERID = "user-1234"


@pytest.fixture
def credentials() -> TidepoolCredentials:
    return TidepoolCredentials(email="person_b@example.com", password="hunter2")


@pytest.fixture
def login_success(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=f"{API_BASE}/auth/login",
        status_code=200,
        headers={"x-tidepool-session-token": SESSION_TOKEN},
        json={"userid": USERID, "username": "person_b@example.com"},
    )


@pytest.fixture
def logout_success(httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=f"{API_BASE}/auth/logout",
        status_code=200,
        json={},
    )


def test_login_captures_session_token_and_userid(
    credentials, login_success, logout_success
):
    client = TidepoolClient(credentials, api_base=API_BASE, rate_limit_seconds=0)
    client._login()
    assert client._session_token == SESSION_TOKEN
    assert client._userid == USERID
    client.close()


def test_login_401_raises_typed_error(httpx_mock, credentials):
    httpx_mock.add_response(
        method="POST",
        url=f"{API_BASE}/auth/login",
        status_code=401,
        text="Unauthorized",
    )
    client = TidepoolClient(credentials, api_base=API_BASE, rate_limit_seconds=0)
    with pytest.raises(TidepoolAuthError) as excinfo:
        client._login()
    assert "401" in str(excinfo.value) or "bad email" in str(excinfo.value).lower()


def test_login_missing_session_token_header_raises(httpx_mock, credentials):
    httpx_mock.add_response(
        method="POST",
        url=f"{API_BASE}/auth/login",
        status_code=200,
        headers={},  # no session token header
        json={"userid": USERID},
    )
    client = TidepoolClient(credentials, api_base=API_BASE, rate_limit_seconds=0)
    with pytest.raises(TidepoolAuthError) as excinfo:
        client._login()
    assert "session-token" in str(excinfo.value).lower()


def test_login_missing_userid_in_body_raises(httpx_mock, credentials):
    httpx_mock.add_response(
        method="POST",
        url=f"{API_BASE}/auth/login",
        status_code=200,
        headers={"x-tidepool-session-token": SESSION_TOKEN},
        json={"username": "person_b@example.com"},  # no userid
    )
    client = TidepoolClient(credentials, api_base=API_BASE, rate_limit_seconds=0)
    with pytest.raises(TidepoolAuthError) as excinfo:
        client._login()
    assert "userid" in str(excinfo.value).lower()


def test_fetch_uses_correct_endpoint_and_headers(
    httpx_mock, credentials, login_success, logout_success
):
    """Verify endpoint is /data/{userId}, NOT the old /data/v1/users/{userid}/data."""
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(API_BASE)}/data/{USERID}\?.*"),
        status_code=200,
        match_headers={"x-tidepool-session-token": SESSION_TOKEN},
        json=[{"id": "r1", "type": "cbg", "time": "2026-05-01T12:00:00.000Z"}],
    )
    client = TidepoolClient(credentials, api_base=API_BASE, rate_limit_seconds=0)
    records = list(client.fetch())
    assert len(records) == 1
    assert records[0]["id"] == "r1"
    client.close()


def test_fetch_returns_array(httpx_mock, credentials, login_success, logout_success):
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(API_BASE)}/data/{USERID}\?.*"),
        status_code=200,
        json=[
            {"id": "r1", "type": "cbg", "time": "2026-05-01T12:00:00.000Z"},
            {"id": "r2", "type": "bolus", "time": "2026-05-01T12:05:00.000Z"},
        ],
    )
    client = TidepoolClient(credentials, api_base=API_BASE, rate_limit_seconds=0)
    records = list(client.fetch())
    assert len(records) == 2
    client.close()


def test_fetch_429_retries_and_succeeds(
    httpx_mock, credentials, login_success, logout_success
):
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(API_BASE)}/data/.*"),
        status_code=429,
        headers={"Retry-After": "0"},
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(API_BASE)}/data/.*"),
        status_code=200,
        json=[{"id": "r1", "type": "cbg", "time": "2026-05-01T12:00:00.000Z"}],
    )
    client = TidepoolClient(credentials, api_base=API_BASE, rate_limit_seconds=0)
    records = list(client.fetch())
    assert len(records) == 1
    client.close()


def test_fetch_401_mid_request_re_authenticates_once(
    httpx_mock, credentials, logout_success
):
    # First login
    httpx_mock.add_response(
        method="POST",
        url=f"{API_BASE}/auth/login",
        status_code=200,
        headers={"x-tidepool-session-token": "first-token"},
        json={"userid": USERID},
    )
    # First fetch: session expired
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(API_BASE)}/data/.*"),
        status_code=401,
    )
    # Re-login
    httpx_mock.add_response(
        method="POST",
        url=f"{API_BASE}/auth/login",
        status_code=200,
        headers={"x-tidepool-session-token": "second-token"},
        json={"userid": USERID},
    )
    # Successful retry
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(API_BASE)}/data/.*"),
        status_code=200,
        json=[{"id": "r1", "type": "cbg", "time": "2026-05-01T12:00:00.000Z"}],
    )

    client = TidepoolClient(credentials, api_base=API_BASE, rate_limit_seconds=0)
    records = list(client.fetch())
    assert len(records) == 1
    assert client._session_token == "second-token"
    client.close()


def test_fetch_500_raises_typed_error(
    httpx_mock, credentials, login_success, logout_success
):
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(API_BASE)}/data/.*"),
        status_code=500,
        text="server error",
    )
    client = TidepoolClient(credentials, api_base=API_BASE, rate_limit_seconds=0)
    with pytest.raises(TidepoolFetchError):
        list(client.fetch())
    client.close()


def test_logout_500_does_not_raise(httpx_mock, credentials, login_success):
    httpx_mock.add_response(
        method="POST",
        url=f"{API_BASE}/auth/logout",
        status_code=500,
    )
    client = TidepoolClient(credentials, api_base=API_BASE, rate_limit_seconds=0)
    client._login()
    # Should not raise
    client.close()


def test_close_without_login_does_not_call_logout(httpx_mock, credentials):
    """A client that never logged in shouldn't try to log out."""
    client = TidepoolClient(credentials, api_base=API_BASE, rate_limit_seconds=0)
    client.close()
    # No requests should have been made
    assert len(httpx_mock.get_requests()) == 0


def test_basal_duration_normalized_from_ms_to_minutes(
    httpx_mock, credentials, login_success, logout_success
):
    """REGRESSION: API returns basal.duration in ms; export uses minutes.

    Synthetic API record {duration: 900000} (ms) = export {duration: 15} (min).
    The source layer normalizes API → minutes so parsers see consistent units.
    Without this, basal sums are ~25,000x too high.
    """
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"{re.escape(API_BASE)}/data/.*"),
        status_code=200,
        json=[
            {
                "id": "b1",
                "type": "basal",
                "time": "2020-01-01T12:00:00.000Z",
                "duration": 900000,  # ms = 15 min
                "rate": 0.5,
                "deliveryType": "automated",
            },
            {
                "id": "c1",
                "type": "cbg",
                "time": "2026-05-12T07:20:00.000Z",
                "value": 120,
                "units": "mg/dL",
            },
        ],
    )
    client = TidepoolClient(credentials, api_base=API_BASE, rate_limit_seconds=0)
    records = list(client.fetch())
    basal = [r for r in records if r["type"] == "basal"][0]
    assert basal["duration"] == 15
    # Non-basal records pass through untouched
    cgm = [r for r in records if r["type"] == "cbg"][0]
    assert cgm["value"] == 120
    client.close()
