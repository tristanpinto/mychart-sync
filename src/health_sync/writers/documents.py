from __future__ import annotations

"""Download and save clinical document content from FHIR DocumentReferences.

Fetches the actual HTML/text content via Binary.Read and saves to the
configured records directory. Deduplicates by checking if a file already
exists at the target path.
"""

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
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s-]+", "_", s)
    return s[:50]


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
) -> int:
    """Save report-like Observation.valueString payloads as clinical records."""
    observations = []
    observations.extend(fhir_data.get("Observation_laboratory", []))
    observations.extend(fhir_data.get("Observation", []))

    saved = 0
    seen_ids = set()
    for observation in observations:
        if observation.get("resourceType") != "Observation":
            continue
        obs_id = observation.get("id", "")
        if obs_id in seen_ids:
            continue
        seen_ids.add(obs_id)

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
        date_prefix = date.split("T")[0] if "T" in date else date
        report_type = _first_display(observation.get("code", {})) or "clinical_report"
        base_name = f"{date_prefix}_{_slugify(report_type)}"
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
        txt_path.write_text(text)
        logger.info(f"  Saved narrative report: {base_name}.txt ({len(text)} chars)")
        saved += 1

    return saved


def download_documents(
    fhir_data: dict[str, list[dict[str, Any]]],
    client: FHIRClient,
    records_dir: Path,
) -> int:
    """Download clinical document content and save to records directory.

    Args:
        fhir_data: Dict with "DocumentReference" key containing FHIR resources.
        client: Authenticated FHIRClient for Binary.Read calls.
        records_dir: Target directory for saved documents.

    Returns:
        Number of documents downloaded.
    """
    doc_refs = [r for r in fhir_data.get("DocumentReference", [])
                if r.get("resourceType") == "DocumentReference"]

    records_dir.mkdir(parents=True, exist_ok=True)
    downloaded = _save_narrative_observation_reports(fhir_data, records_dir)

    if not doc_refs:
        return downloaded

    docs = parse_document_references(doc_refs)

    for doc in docs:
        if not doc["content_urls"]:
            continue

        # Build filename: date_type.txt
        date_prefix = doc["date"].split("T")[0] if doc["date"] else "undated"
        type_slug = _slugify(doc["type"]) if doc["type"] else "document"
        base_name = f"{date_prefix}_{type_slug}"

        # Check if already downloaded
        txt_path = records_dir / f"{base_name}.txt"
        if txt_path.exists():
            continue

        # Prefer text/html content
        html_url = None
        for cu in doc["content_urls"]:
            if "html" in cu.get("content_type", ""):
                html_url = cu["url"]
                break

        if not html_url:
            # Fall back to first available
            html_url = doc["content_urls"][0]["url"]

        try:
            content = client.fetch_binary(html_url)
            content_str = content.decode("utf-8", errors="replace")

            # Epic returns FHIR Binary resources as JSON with base64 data
            import json as _json
            import base64 as _b64
            try:
                binary_resource = _json.loads(content_str)
                if binary_resource.get("resourceType") == "Binary" and "data" in binary_resource:
                    decoded_bytes = _b64.b64decode(binary_resource["data"])
                    content_str = decoded_bytes.decode("utf-8", errors="replace")
            except (_json.JSONDecodeError, KeyError):
                pass  # Not JSON — treat as raw content

            # Convert HTML to plain text
            plain_text = _html_to_text(content_str)

            if plain_text.strip():
                txt_path.write_text(plain_text)
                logger.info(f"  Downloaded: {base_name}.txt ({len(plain_text)} chars)")
                downloaded += 1

        except TokenExpiredError:
            logger.warning("Token expired during document download — stopping")
            break
        except Exception as e:
            logger.warning(f"  Failed to download {base_name}: {e}")

    return downloaded
