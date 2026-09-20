from __future__ import annotations

import httpx

from health_sync.fhir.client import FHIRClient


def test_request_retries_timeout(monkeypatch, tmp_path) -> None:
    client = FHIRClient(
        base_url="https://example.test/FHIR/R4",
        access_token="token",
        patient_id="patient",
        cache_dir=tmp_path,
        rate_limit=0,
    )
    calls = 0

    def fake_get(url, params=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"resourceType": "Bundle"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(client._client, "get", fake_get)
    monkeypatch.setattr("health_sync.fhir.client.time.sleep", lambda seconds: None)

    response = client._request("https://example.test/FHIR/R4/Condition")

    assert response.status_code == 200
    assert calls == 2
