from __future__ import annotations

"""Download and save clinical document content from FHIR DocumentReferences.

Saves readable text and original PDF bytes under stable, provider-specific
filenames. Existing records are never overwritten.
"""

import base64
import hashlib
import json
import logging
import re
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from typing import Any

from health_sync.fhir.client import FHIRClient, TokenExpiredError
from health_sync.parsers.documents import parse_document_references
from health_sync.parsers.observations import _is_narrative_lab_report

logger = logging.getLogger(__name__)


class DocumentDownloadError(RuntimeError):
    """One or more documents could not be saved; retry before advancing sync state."""


class _HTMLTextExtractor(HTMLParser):
    """Simple HTML to plain text converter."""

    def __init__(self):
        super().__init__()
        self._text = StringIO()
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("br", "p", "div", "h1", "h2", "h3", "h4", "tr"):
            self._text.write("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "li"):
            self._text.write("\n")

    def handle_data(self, data):
        if not self._skip:
            self._text.write(data)

    def get_text(self) -> str:
        return self._text.getvalue()


def _html_to_text(html: str) -> str:
    """Convert HTML to plain text."""
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    text = extractor.get_text()
    # Clean up excessive whitespace
    lines = [line.strip() for line in text.split("\n")]
    # Remove excessive blank lines
    cleaned = []
    prev_blank = False
    for line in lines:
        if not line:
            if not prev_blank:
                cleaned.append("")
            prev_blank = True
        else:
            cleaned.append(line)
            prev_blank = False
    return "\n".join(cleaned).strip()


def _slugify(text: str) -> str:
    """Convert text to a filename-safe slug."""
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9\s_-]", "", s)
    s = re.sub(r"[\s-]+", "_", s)
    return s[:50]


def _document_name(source: str, resource: dict, date: str, title: str) -> str:
    """Keep readable labels, but use source and resource identity for uniqueness."""
    identity = resource.get("id") or json.dumps(resource, sort_keys=True)
    key = json.dumps([source.rstrip("/"), resource.get("resourceType"), identity])
    suffix = hashlib.sha256(key.encode()).hexdigest()[:16]
    match = re.match(r"^\d{4}-\d{2}-\d{2}(?:T|$)", date)
    day = date[:10] if match else "undated"
    return f"{day}_{_slugify(title) or 'document'}_{suffix}"


def _write_new(path: Path, content: bytes) -> bool:
    """Do not overwrite existing records or leave failed writes looking complete."""
    try:
        stream = path.open("xb")
    except FileExistsError:
        return False
    try:
        with stream:
            stream.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return True


def _document_content(content: bytes, content_type: str) -> tuple[str, bytes]:
    """Unwrap FHIR Binary and retain PDF bytes; reject API errors as documents."""
    if content.lstrip().startswith((b"{", b"[")):
        try:
            resource = json.loads(content)
        except json.JSONDecodeError:
            # Clinical text can begin with a bracket without being JSON.
            if content_type.partition(";")[0].lower() not in {"text/plain", "text/html", "application/xhtml+xml"}:
                raise
        else:
            if not isinstance(resource, dict) or resource.get("resourceType") != "Binary":
                raise ValueError("Expected a FHIR Binary document")
            content = base64.b64decode(resource["data"], validate=True)
            content_type = resource.get("contentType") or content_type

    mime = content_type.partition(";")[0].strip().lower()
    if content.startswith(b"%PDF-"):
        return ".pdf", content
    if mime == "application/pdf":
        raise ValueError("PDF document has no PDF header")
    if mime and mime not in ("text/html", "application/xhtml+xml", "text/plain"):
        raise ValueError("Unsupported document content type")

    text = content.decode("utf-8")
    if "\x00" in text:
        raise ValueError("Unexpected binary document")
    if mime != "text/plain":
        text = _html_to_text(text)
    if not text.strip():
        raise ValueError("Empty document")
    return ".txt", text.encode("utf-8")


def _first_display(concept: dict[str, Any]) -> str:
    if concept.get("text"):
        return str(concept["text"])
    for coding in concept.get("coding", []):
        if coding.get("display"):
            return str(coding["display"])
    return ""


def _save_narrative_observation_reports(
    fhir_data: dict[str, list[dict[str, Any]]],
    records_dir: Path,
    source: str,
) -> int:
    """Save report-like Observation.valueString payloads as clinical records."""
    observations = []
    observations.extend(fhir_data.get("Observation_laboratory", []))
    observations.extend(fhir_data.get("Observation", []))

    saved = 0
    seen_names = set()
    for observation in observations:
        if observation.get("resourceType") != "Observation":
            continue
        obs_id = observation.get("id", "")
        if not _is_narrative_lab_report(observation):
            continue

        body = observation.get("valueString", "").strip()
        if not body:
            continue

        date = (
            observation.get("effectiveDateTime")
            or observation.get("issued")
            or observation.get("effectivePeriod", {}).get("start")
            or "undated"
        )
        report_type = _first_display(observation.get("code", {})) or "clinical_report"
        base_name = _document_name(source, observation, date, report_type)
        if base_name in seen_names:
            continue
        seen_names.add(base_name)
        txt_path = records_dir / f"{base_name}.txt"
        if txt_path.exists():
            continue

        header = [
            report_type,
            "",
            f"Source: FHIR Observation/{obs_id}" if obs_id else "Source: FHIR Observation",
            f"Date: {date}",
        ]
        if observation.get("status"):
            header.append(f"Status: {observation['status']}")

        text = "\n".join(header) + "\n\n" + body.replace("\r\n", "\n").replace("\r", "\n") + "\n"
        saved += _write_new(txt_path, text.encode("utf-8"))

    return saved


def download_documents(
    fhir_data: dict[str, list[dict[str, Any]]],
    client: FHIRClient,
    records_dir: Path,
) -> int:
    """Save new attachments and narrative reports; return the number written."""
    doc_refs = [r for r in fhir_data.get("DocumentReference", [])
                if r.get("resourceType") == "DocumentReference"]

    records_dir.mkdir(parents=True, exist_ok=True)
    source = getattr(client, "base_url", "")
    try:
        downloaded = _save_narrative_observation_reports(fhir_data, records_dir, source)
    except Exception:
        raise DocumentDownloadError("Could not save narrative reports; retry the sync.") from None

    if not doc_refs:
        return downloaded

    docs = parse_document_references(doc_refs)
    failures = 0

    for doc in docs:
        if not doc["content_urls"]:
            continue

        identity = {"resourceType": "DocumentReference", **doc}
        base_name = _document_name(source, identity, doc["date"], doc["type"])

        # Check if already downloaded
        if any((records_dir / f"{base_name}{ext}").exists() for ext in (".txt", ".pdf")):
            continue

        preference = {"text/html": 0, "application/xhtml+xml": 0, "text/plain": 1, "application/pdf": 2}
        attachment = min(
            doc["content_urls"],
            key=lambda cu: preference.get(cu["content_type"].partition(";")[0].strip().lower(), 3),
        )

        try:
            content = client.fetch_binary(attachment["url"])
            extension, content = _document_content(content, attachment["content_type"])
            downloaded += _write_new(records_dir / f"{base_name}{extension}", content)

        except TokenExpiredError:
            raise
        except Exception:
            failures += 1

    if failures:
        raise DocumentDownloadError(
            f"Could not save {failures} document(s); retry the sync."
        )
    return downloaded
