from __future__ import annotations

"""Parse FHIR DocumentReference resources for clinical document metadata."""

from typing import Any


def parse_document_references(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract document metadata and attachment URLs; content is downloaded separately."""
    docs = []
    for r in resources:
        if r.get("resourceType") != "DocumentReference":
            continue

        # Document type
        doc_type = r.get("type", {}).get("text", "")
        if not doc_type:
            codings = r.get("type", {}).get("coding", [])
            for c in codings:
                if c.get("display"):
                    doc_type = c["display"]
                    break

        # Category
        categories = r.get("category", [])
        category = ""
        if categories:
            category = categories[0].get("text", "")

        # Date
        date = r.get("date", "") or r.get("context", {}).get("period", {}).get("start", "")

        # Status
        status = r.get("status", "")

        # Content URLs (for downloading)
        content_urls = []
        for content in r.get("content", []):
            attachment = content.get("attachment", {})
            url = attachment.get("url", "")
            content_type = attachment.get("contentType", "")
            if url:
                content_urls.append({
                    "url": url,
                    "content_type": content_type,
                    "title": attachment.get("title", ""),
                })

        # Author
        authors = []
        for author in r.get("author", []):
            if author.get("display"):
                authors.append(author["display"])

        docs.append({
            "id": r.get("id", ""),
            "type": doc_type,
            "category": category,
            "date": date,
            "status": status,
            "authors": authors,
            "content_urls": content_urls,
        })

    return docs
