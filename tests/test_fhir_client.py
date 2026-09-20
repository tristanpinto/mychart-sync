from __future__ import annotations

import httpx
import pytest

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

    def fake_get(url, params=None, headers=None):
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


def test_external_attachment_never_receives_hospital_token(httpx_mock):
    httpx_mock.add_response(url="https://files.example/doc", content=b"document")
    with FHIRClient("https://hospital.example/FHIR/R4", "private-token", "patient", rate_limit=0) as client:
        assert client.fetch_binary("https://files.example/doc") == b"document"
    assert "authorization" not in httpx_mock.get_request().headers


@pytest.mark.parametrize("url", ["http://hospital.example/doc", "https://other.example/page"])
def test_unsafe_authenticated_urls_are_rejected_before_request(url):
    with FHIRClient("https://hospital.example/FHIR/R4", "private-token", "patient", rate_limit=0) as client:
        with pytest.raises(ValueError):
            client._request(url)


def test_relative_pagination_keeps_auth_and_replaces_empty_cache(httpx_mock, tmp_path):
    httpx_mock.add_response(json={"resourceType": "Bundle", "link": [{"relation": "next", "url": "?page=2"}]})
    httpx_mock.add_response(json={"resourceType": "Bundle"})
    (tmp_path / "condition.json").write_text('[{"old":true}]')
    with FHIRClient("https://hospital.example/FHIR/R4", "private-token", "patient", tmp_path, rate_limit=0) as client:
        assert client.fetch_resource("Condition") == []
    requests = httpx_mock.get_requests()
    assert str(requests[1].url) == "https://hospital.example/FHIR/R4/Condition?page=2"
    assert all(r.headers["Authorization"] == "Bearer private-token" for r in requests)
    assert (tmp_path / "condition.json").read_text() == "[]"


@pytest.mark.parametrize("body", [
    {"resourceType": "OperationOutcome"},
    {"resourceType": "Bundle", "entry": [{"resource": {
        "resourceType": "OperationOutcome", "issue": [{"severity": "error"}],
    }}]},
])
def test_error_payload_is_not_successful_empty_result(httpx_mock, body):
    httpx_mock.add_response(json=body)
    with FHIRClient("https://hospital.example/FHIR/R4", "private-token", "patient", rate_limit=0) as client:
        with pytest.raises(RuntimeError, match="incomplete"):
            client.fetch_resource("Condition")


def test_repeating_pagination_fails_instead_of_looping(httpx_mock):
    httpx_mock.add_response(json={"resourceType": "Bundle", "link": [
        {"relation": "next", "url": "https://hospital.example/FHIR/R4/Condition"}
    ]})
    with FHIRClient("https://hospital.example/FHIR/R4", "private-token", "patient", rate_limit=0) as client:
        with pytest.raises(RuntimeError, match="repeated"):
            client.fetch_resource("Condition")


def test_empty_incremental_keeps_previous_raw_snapshot(httpx_mock, tmp_path):
    httpx_mock.add_response(json={"resourceType": "Bundle"})
    path = tmp_path / "condition.json"
    previous = '[{"resourceType":"Condition","id":"old"}]'
    path.write_text(previous)
    with FHIRClient("https://hospital.example/FHIR/R4", "private-token", "patient", tmp_path, rate_limit=0) as client:
        assert client.fetch_resource("Condition", since="2020-01-01T00:00:00Z") == []
    assert path.read_text() == previous
