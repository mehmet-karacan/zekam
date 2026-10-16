"""Offline attachment transport/parser fixtures; no corporate service call."""

import copy
import io
import json
import urllib.error
import urllib.request

import pytest

from zekam.application import atlassian_mcp_server as subject
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import PolicyViolation

BASE = "https://jira.example.test/jira"
ENV = {"JIRA_BASE_URL": BASE, "JIRA_API_TOKEN": "fixture-token"}


def _fixture(payload=b"Example specification.\n\nSecond paragraph."):
    row = {
        "id": "10042",
        "key": "DEMO-42",
        "fields": {
            "updated": "2026-10-16T12:00:00+0300",
            "attachment": [
                {
                    "id": "123",
                    "filename": "example.txt",
                    "size": len(payload),
                    "mimeType": "text/plain",
                    "content": BASE + "/secure/attachment/123/example.txt",
                }
            ],
        },
    }
    metadata = subject._jira_issue_snapshot(BASE, row, key="DEMO-42")["_zekam"]["attachments"][0]
    args = {
        "issue_key": "DEMO-42",
        "attachment_id": "123",
        "expected_metadata_digest": metadata["metadata_digest"],
    }
    return payload, row, args


def _transport(monkeypatch, payload, row, *, changed=None, headers=None):
    reads, downloads = [], []

    def get(*args, **kwargs):
        reads.append(args)
        result = copy.deepcopy(row)
        if len(reads) > 1 and changed is not None:
            changed(result)
        return result

    class Response(io.BytesIO):
        def __init__(self):
            super().__init__(payload)
            self.headers = headers or {
                "Content-Type": "text/plain",
                "Content-Length": str(len(payload)),
            }

        def read(self, size=-1):
            assert size == len(payload) + 1
            return super().read(size)

    class Opener:
        def open(self, request, **kwargs):
            downloads.append(request)
            assert request.full_url == row["fields"]["attachment"][0]["content"]
            assert kwargs["timeout"] == 45
            return Response()

    monkeypatch.setattr(subject, "api_get", get)
    monkeypatch.setattr(subject.urllib.request, "build_opener", lambda *_: Opener())
    return reads, downloads


def test_attachment_read_preserves_content_hash_and_locators_without_import(monkeypatch):
    payload, row, args = _fixture()
    reads, downloads = _transport(monkeypatch, payload, row)
    result = subject.call_tool("jira_read_attachment", args, environ=ENV)
    document = json.loads(result["content"][0]["text"])
    assert len(reads) == 2 and len(downloads) == 1
    assert document["attachment"]["content_digest"] == digest_of_bytes(payload)
    assert document["attachment"]["content_read"] is True
    assert document["knowledge_imported"] is False and document["grants_authority"] is False
    assert document["units"][0]["locator"]["block_index"] == 0
    assert len(document["units"]) == 2
    assert "fixture-token" not in result["content"][0]["text"]


@pytest.mark.parametrize("mutation", ["updated", "metadata", "removed"])
def test_changed_attachment_or_issue_is_rejected_after_download(monkeypatch, mutation):
    payload, row, args = _fixture()

    def change(result):
        if mutation == "updated":
            result["fields"]["updated"] = "2026-10-16T12:00:01+0300"
        elif mutation == "metadata":
            result["fields"]["attachment"][0]["filename"] = "changed.txt"
        else:
            result["fields"]["attachment"] = []

    _, downloads = _transport(monkeypatch, payload, row, changed=change)
    with pytest.raises(RuntimeError):
        subject.call_tool("jira_read_attachment", args, environ=ENV)
    assert len(downloads) == 1


def test_wrong_reviewed_metadata_blocks_download(monkeypatch):
    payload, row, args = _fixture()
    args["expected_metadata_digest"] = digest_of_bytes(b"other")
    _, downloads = _transport(monkeypatch, payload, row)
    with pytest.raises(RuntimeError, match="metadata drift"):
        subject.call_tool("jira_read_attachment", args, environ=ENV)
    assert downloads == []


@pytest.mark.parametrize(
    "content",
    [
        "https://outside.example.test/jira/secure/attachment/123/file.txt",
        BASE + "/secure/attachment/1234/file.txt",
        BASE + "/rest/api/2/user",
        BASE + "/secure/attachment/123/../other",
        BASE + "/secure/attachment/123/file?token=x",
    ],
)
def test_download_locator_cannot_escape_issue_attachment(monkeypatch, content):
    payload, row, args = _fixture()
    row["fields"]["attachment"][0]["content"] = content
    _, downloads = _transport(monkeypatch, payload, row)
    with pytest.raises(RuntimeError, match="locator"):
        subject.call_tool("jira_read_attachment", args, environ=ENV)
    assert downloads == []


@pytest.mark.parametrize("payload", [b'password="private-literal"', b"person@example.test"])
def test_secret_or_pii_content_never_reaches_mcp_result(monkeypatch, payload):
    payload, row, args = _fixture(payload)
    _transport(monkeypatch, payload, row)
    with pytest.raises((RuntimeError, PolicyViolation)):
        subject.call_tool("jira_read_attachment", args, environ=ENV)


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "text/html"},
        {"Content-Type": "text/plain", "Content-Length": "999999"},
        {"Content-Type": "text/plain", "Content-Encoding": "gzip"},
    ],
)
def test_wrong_http_content_or_size_rejected(monkeypatch, headers):
    payload, row, args = _fixture()
    _transport(monkeypatch, payload, row, headers=headers)
    with pytest.raises(RuntimeError, match="read basarisiz"):
        subject.call_tool("jira_read_attachment", args, environ=ENV)


def test_attachment_redirect_cannot_become_other_same_site_api():
    handler = subject._AttachmentRedirectHandler(("/jira/secure/attachment/123",))
    request = urllib.request.Request(BASE + "/secure/attachment/123/file.txt")
    for url in (
        BASE + "/rest/api/2/user",
        "https://outside.example.test/jira/secure/attachment/123/file.txt",
    ):
        with pytest.raises(urllib.error.HTTPError):
            handler.redirect_request(request, None, 302, "redirect", {}, url)


def test_pdf_read_only_profile_refuses_before_render_or_ocr(monkeypatch):
    from zekam.infrastructure.knowledge import document_parsers as parsers

    closed = []

    class Page:
        def get_textpage(self):
            return self

        def get_text_bounded(self):
            return ""

        def render(self, **kwargs):
            pytest.fail("read-only PDF must not render/start OCR")

    class Document:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return Page()

        def close(self):
            closed.append(True)

    class Module:
        PdfDocument = staticmethod(lambda _: Document())

    monkeypatch.setattr(parsers.PdfParser, "_module", staticmethod(lambda: Module()))
    monkeypatch.setattr(parsers, "_pdfium_build_profile", lambda _: {})
    with pytest.raises(PolicyViolation, match="OCR"):
        parsers.PdfParser(allow_ocr=False).parse(b"%PDF-fixture")
    assert closed == [True]
