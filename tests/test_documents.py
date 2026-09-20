import base64
import json

import pytest

from health_sync.fhir.client import TokenExpiredError
from health_sync.writers.documents import DocumentDownloadError, download_documents


class DocumentClient:
    def __init__(self, content=b"<p>Synthetic note</p>", base_url="https://a.example/FHIR/R4"):
        self.base_url = base_url
        self.content = content
        self.calls = 0

    def fetch_binary(self, url):
        self.calls += 1
        if isinstance(self.content, Exception):
            raise self.content
        return self.content


def document(identifier="note-a", content_type="text/html", **changes):
    return {
        "resourceType": "DocumentReference",
        "id": identifier,
        "date": "2020-01-01T12:00:00Z",
        "type": {"text": "Visit note"},
        "content": [{"attachment": {"url": f"Binary/{identifier}", "contentType": content_type}}],
        **changes,
    }


def download(tmp_path, client, docs):
    return download_documents({"DocumentReference": docs}, client, tmp_path)


def test_distinct_ids_and_providers_do_not_collide(tmp_path):
    docs = [document("first"), document("second")]
    assert download(tmp_path, DocumentClient(), docs) == 2
    assert download(tmp_path, DocumentClient(base_url="https://b.example/FHIR/R4"), docs) == 2
    assert len(list(tmp_path.glob("2020-01-01_visit_note_*.txt"))) == 4


def test_missing_id_uses_stable_content_identity(tmp_path):
    docs = [document("", content=[{"attachment": {"url": f"Binary/{n}"}}]) for n in (1, 2)]
    client = DocumentClient()
    assert download(tmp_path, client, docs) == 2
    assert download(tmp_path, client, docs) == 0
    assert client.calls == 2


def test_unsafe_labels_stay_in_records_directory(tmp_path):
    doc = document(date="../../outside", type={"text": "../../../Visit note"})
    assert download(tmp_path, DocumentClient(), [doc]) == 1
    assert len(list(tmp_path.glob("undated_visit_note_*.txt"))) == 1


@pytest.mark.parametrize("wrapped", [False, True])
def test_pdf_bytes_are_preserved(tmp_path, wrapped):
    pdf = b"%PDF-1.4\n\xff\x00 synthetic bytes\n%%EOF"
    content = pdf
    if wrapped:
        content = json.dumps({
            "resourceType": "Binary", "contentType": "application/pdf",
            "data": base64.b64encode(pdf).decode(),
        }).encode()
    assert download(tmp_path, DocumentClient(content), [document(content_type="application/pdf")]) == 1
    assert next(tmp_path.glob("*.pdf")).read_bytes() == pdf
    assert not list(tmp_path.glob("*.txt"))


def test_html_is_readable_text_without_scripts(tmp_path):
    client = DocumentClient(b"<h1>Note</h1><p>Readable text.</p><script>secret()</script>")
    assert download(tmp_path, client, [document()]) == 1
    assert next(tmp_path.glob("*.txt")).read_text() == "Note\n\nReadable text."


def test_plain_text_preserves_angle_brackets(tmp_path):
    assert download(tmp_path, DocumentClient(b"Result <5 units"), [document(content_type="text/plain")]) == 1
    assert next(tmp_path.glob("*.txt")).read_text() == "Result <5 units"


@pytest.mark.parametrize("text", [b"[Final report]\nNormal result", b"{Draft report}\nNormal result"])
def test_bracketed_plain_text_is_not_mistaken_for_json(tmp_path, text):
    assert download(tmp_path, DocumentClient(text), [document(content_type="text/plain")]) == 1
    assert next(tmp_path.glob("*.txt")).read_bytes() == text


def test_supported_attachment_is_selected_over_unsupported_first_entry(tmp_path):
    doc = document(content=[
        {"attachment": {"url": "Binary/image", "contentType": "image/png"}},
        {"attachment": {"url": "Binary/pdf", "contentType": "application/pdf"}},
    ])

    class Client(DocumentClient):
        def fetch_binary(self, url):
            assert url == "Binary/pdf"
            return b"%PDF-1.4\nSynthetic PDF\n%%EOF"

    assert download(tmp_path, Client(), [doc]) == 1
    assert len(list(tmp_path.glob("*.pdf"))) == 1


@pytest.mark.parametrize("content", [
    b'{"resourceType":"OperationOutcome","diagnostics":"private detail"}',
    b'{"error":"private detail"}',
    b'{"resourceType":"Binary","data":"not base64"}',
    b'[]', b'', b'\xff\x00',
])
def test_invalid_document_fails_without_saving_or_exposing_body(tmp_path, content):
    with pytest.raises(DocumentDownloadError) as caught:
        download(tmp_path, DocumentClient(content), [document()])
    assert "private detail" not in str(caught.value)
    assert list(tmp_path.iterdir()) == []


def test_failures_attempt_remaining_documents_and_report_safe_error(tmp_path):
    client = DocumentClient(RuntimeError("private detail"))
    with pytest.raises(DocumentDownloadError, match=r"2 document\(s\)") as caught:
        download(tmp_path, client, [document("a"), document("b")])
    assert client.calls == 2
    assert "private detail" not in str(caught.value)


def test_expired_token_propagates(tmp_path):
    with pytest.raises(TokenExpiredError):
        download(tmp_path, DocumentClient(TokenExpiredError()), [document()])


def test_existing_and_legacy_files_are_not_overwritten(tmp_path):
    legacy = tmp_path / "2020-01-01_visit_note.txt"
    legacy.write_text("Original legacy file")
    client = DocumentClient()
    assert download(tmp_path, client, [document()]) == 1
    current = next(path for path in tmp_path.glob("*.txt") if path != legacy)
    current.write_text("User annotations")
    assert download(tmp_path, client, [document()]) == 0
    assert current.read_text() == "User annotations"
    assert legacy.read_text() == "Original legacy file"
    assert client.calls == 1
