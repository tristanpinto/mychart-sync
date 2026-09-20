from __future__ import annotations

"""SMART on FHIR OAuth2 authentication with PKCE.

Implements the Standalone Patient Launch flow:
1. Discover authorize/token endpoints from FHIR server
2. Generate PKCE code_verifier + code_challenge
3. Open browser for MyChart login + consent
4. Local callback server captures authorization code
5. Exchange code for access + refresh tokens
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import httpx

# FHIR resource scopes (SMART v1 syntax for broad compatibility)
SCOPES = [
    "patient/Patient.read",
    "patient/Condition.read",
    "patient/MedicationRequest.read",
    "patient/AllergyIntolerance.read",
    "patient/Immunization.read",
    "patient/Observation.read",
    "patient/Encounter.read",
    "patient/Procedure.read",
    "patient/DocumentReference.read",
    "patient/CareTeam.read",
    "patient/DiagnosticReport.read",
    "patient/CarePlan.read",
    "patient/Device.read",
    "patient/Binary.read",
    "patient/Appointment.read",
    "patient/Coverage.read",
    "patient/Goal.read",
    "patient/FamilyMemberHistory.read",
    "patient/ImagingStudy.read",
    "launch/patient",
    "openid",
    "fhirUser",
    "offline_access",
]


@dataclass
class SMARTEndpoints:
    """Authorization and token endpoints discovered from a FHIR server."""

    authorize_url: str
    token_url: str
    audience_url: str | None = None


@dataclass
class AuthResult:
    """Result of a successful SMART on FHIR authentication."""

    access_token: str
    refresh_token: str | None
    expires_in: int
    patient_id: str
    token_endpoint: str
    scope: str


def _implementation_url(capability_statement: dict) -> str | None:
    """Return the server's canonical FHIR base, when it advertises one."""
    implementation = capability_statement.get("implementation")
    if not isinstance(implementation, dict):
        return None
    url = implementation.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    return url.rstrip("/")


def _discover_capability_audience(fhir_base_url: str) -> str | None:
    """Read the canonical FHIR base used as the OAuth ``aud`` value.

    Reverse proxies can expose a friendly alias while advertising a different
    canonical FHIR base. SMART authorization servers validate ``aud`` against
    that canonical value, so use it when the CapabilityStatement provides one.
    """
    try:
        resp = httpx.get(
            f"{fhir_base_url.rstrip('/')}/metadata",
            headers={"Accept": "application/fhir+json"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        return _implementation_url(resp.json())
    except (httpx.HTTPError, json.JSONDecodeError):
        return None


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    # code_verifier: 43-128 chars from unreserved characters
    code_verifier = secrets.token_urlsafe(64)
    # code_challenge: BASE64URL(SHA256(code_verifier))
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def discover_endpoints(fhir_base_url: str) -> SMARTEndpoints:
    """Discover SMART authorization endpoints from a FHIR server.

    Tries .well-known/smart-configuration first, falls back to metadata
    (CapabilityStatement).
    """
    base = fhir_base_url.rstrip("/")

    # Try .well-known/smart-configuration first
    try:
        resp = httpx.get(
            f"{base}/.well-known/smart-configuration",
            headers={"Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return SMARTEndpoints(
                authorize_url=data["authorization_endpoint"],
                token_url=data["token_endpoint"],
                audience_url=_discover_capability_audience(base),
            )
    except (httpx.HTTPError, KeyError, json.JSONDecodeError):
        pass

    # Fall back to metadata (CapabilityStatement)
    resp = httpx.get(
        f"{base}/metadata",
        headers={"Accept": "application/fhir+json"},
        timeout=15,
    )
    resp.raise_for_status()
    cap = resp.json()

    # Navigate: rest[0].security.extension where url contains "oauth-uris"
    security = cap.get("rest", [{}])[0].get("security", {})
    for ext in security.get("extension", []):
        if "oauth-uris" in ext.get("url", ""):
            authorize_url = None
            token_url = None
            for sub_ext in ext.get("extension", []):
                if sub_ext.get("url") == "authorize":
                    authorize_url = sub_ext["valueUri"]
                elif sub_ext.get("url") == "token":
                    token_url = sub_ext["valueUri"]
            if authorize_url and token_url:
                return SMARTEndpoints(
                    authorize_url=authorize_url,
                    token_url=token_url,
                    audience_url=_implementation_url(cap),
                )

    raise RuntimeError(
        f"Could not discover SMART endpoints from {base}. "
        "Neither .well-known/smart-configuration nor metadata provided OAuth URIs."
    )


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that captures the OAuth2 callback."""

    auth_code: str | None = None
    returned_state: str | None = None
    error: str | None = None

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "error" in params:
            _CallbackHandler.error = params["error"][0]
            self._respond("Authentication failed. You can close this tab.")
        elif "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            _CallbackHandler.returned_state = params.get("state", [None])[0]
            self._respond("Authentication successful! You can close this tab.")
        else:
            self._respond("Unexpected callback. You can close this tab.")

    def _respond(self, message: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        html = f"""<!DOCTYPE html>
<html><body style="font-family: system-ui; text-align: center; margin-top: 80px;">
<h2>{message}</h2>
</body></html>"""
        self.wfile.write(html.encode())

    def log_message(self, format, *args) -> None:  # noqa: A002
        # Suppress HTTP server logs
        pass


def _build_authorization_url(
    endpoints: SMARTEndpoints,
    fhir_base_url: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """Build the SMART authorization URL with the discovered FHIR audience."""
    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state,
        "aud": endpoints.audience_url or fhir_base_url,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{endpoints.authorize_url}?{urllib.parse.urlencode(auth_params)}"


def _ensure_localhost_cert(secrets_dir: Path) -> tuple[str, str]:
    """Ensure a self-signed cert exists for localhost HTTPS callback.

    Returns (certfile, keyfile) paths.
    """
    cert_file = secrets_dir / "localhost.pem"
    key_file = secrets_dir / "localhost-key.pem"

    if cert_file.exists() and key_file.exists():
        return str(cert_file), str(key_file)

    secrets_dir.mkdir(parents=True, exist_ok=True)

    # Generate self-signed cert via openssl
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key_file),
            "-out", str(cert_file),
            "-days", "365",
            "-nodes",
            "-subj", "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )

    return str(cert_file), str(key_file)


def authenticate(
    fhir_base_url: str,
    client_id: str,
    redirect_uri: str = "https://localhost:8080/callback",
    port: int = 8080,
    timeout: int = 120,
    secrets_dir: Path | None = None,
    client_secret: str | None = None,
) -> AuthResult:
    """Open MyChart login, capture the local callback, and exchange the code.

    redirect_uri must match the Epic registration. A client_secret uses Basic
    auth for the confidential-client flow. Failures/timeouts raise RuntimeError.
    """
    # 1. Discover endpoints
    endpoints = discover_endpoints(fhir_base_url)

    # 2. Generate PKCE
    code_verifier, code_challenge = _generate_pkce()

    # 3. Generate state for CSRF protection
    state = secrets.token_urlsafe(32)

    # 4. Build authorization URL
    auth_url = _build_authorization_url(
        endpoints,
        fhir_base_url,
        client_id,
        redirect_uri,
        state,
        code_challenge,
    )

    # 5. Start local callback server
    _CallbackHandler.auth_code = None
    _CallbackHandler.returned_state = None
    _CallbackHandler.error = None

    server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)

    # Wrap with SSL if using https redirect
    use_https = redirect_uri.startswith("https://")
    if use_https:
        if secrets_dir is None:
            secrets_dir = Path(__file__).resolve().parent.parent.parent.parent / "secrets"
        cert_file, key_file = _ensure_localhost_cert(secrets_dir)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_file, key_file)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        # 6. Open browser
        print(f"Opening browser for MyChart login...")
        print(f"If the browser doesn't open, visit:\n{auth_url}\n")
        webbrowser.open(auth_url)

        # 7. Wait for callback
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _CallbackHandler.auth_code or _CallbackHandler.error:
                break
            time.sleep(0.5)

        if _CallbackHandler.error:
            raise RuntimeError(f"Authentication error: {_CallbackHandler.error}")

        if not _CallbackHandler.auth_code:
            raise RuntimeError(f"Authentication timed out after {timeout}s")

        # Verify state
        if _CallbackHandler.returned_state != state:
            raise RuntimeError("State mismatch — possible CSRF attack")

        # 8. Exchange code for tokens
        token_data = {
            "grant_type": "authorization_code",
            "code": _CallbackHandler.auth_code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
        token_headers = {"Content-Type": "application/x-www-form-urlencoded"}

        if client_secret:
            # Confidential client: Basic auth header
            credentials = base64.b64encode(
                f"{client_id}:{client_secret}".encode()
            ).decode()
            token_headers["Authorization"] = f"Basic {credentials}"
        else:
            # Public client: client_id in body
            token_data["client_id"] = client_id

        token_resp = httpx.post(
            endpoints.token_url,
            data=token_data,
            headers=token_headers,
            timeout=15,
        )
        token_resp.raise_for_status()
        token_json = token_resp.json()

        return AuthResult(
            access_token=token_json["access_token"],
            refresh_token=token_json.get("refresh_token"),
            expires_in=token_json.get("expires_in", 3600),
            patient_id=token_json.get("patient", ""),
            token_endpoint=endpoints.token_url,
            scope=token_json.get("scope", ""),
        )

    finally:
        server.shutdown()
