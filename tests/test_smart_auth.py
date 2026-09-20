from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from health_sync.auth import smart_auth


def test_discover_endpoints_uses_canonical_capability_audience(httpx_mock) -> None:
    base = "https://alias.example/FHIR/R4"
    canonical = "https://proxy.example/APIProxy/api/FHIR/R4"
    httpx_mock.add_response(
        method="GET",
        url=f"{base}/.well-known/smart-configuration",
        json={
            "authorization_endpoint": "https://proxy.example/oauth2/authorize",
            "token_endpoint": "https://proxy.example/oauth2/token",
        },
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{base}/metadata",
        json={"implementation": {"url": canonical}},
    )

    endpoints = smart_auth.discover_endpoints(base)

    assert endpoints.authorize_url == "https://proxy.example/oauth2/authorize"
    assert endpoints.token_url == "https://proxy.example/oauth2/token"
    assert endpoints.audience_url == canonical


def test_authenticate_uses_discovered_audience(monkeypatch, tmp_path) -> None:
    canonical = "https://proxy.example/APIProxy/api/FHIR/R4"
    monkeypatch.setattr(
        smart_auth,
        "discover_endpoints",
        lambda _base: smart_auth.SMARTEndpoints(
            authorize_url="https://proxy.example/oauth2/authorize",
            token_url="https://proxy.example/oauth2/token",
            audience_url=canonical,
        ),
    )
    opened: list[str] = []
    monkeypatch.setattr(smart_auth.webbrowser, "open", opened.append)

    with pytest.raises(RuntimeError, match="Authentication timed out"):
        smart_auth.authenticate(
            fhir_base_url="https://alias.example/FHIR/R4",
            client_id="client-id",
            redirect_uri="http://localhost:8080/callback",
            port=0,
            timeout=0,
            secrets_dir=tmp_path,
        )

    assert len(opened) == 1
    params = parse_qs(urlsplit(opened[0]).query)
    assert params["aud"] == [canonical]
