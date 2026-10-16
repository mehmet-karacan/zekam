"""Bounded remote read contracts; all transports are offline fixtures."""

from __future__ import annotations

import copy
import io
import json
import urllib.error

import pytest

from zekam.application import atlassian_mcp_server as subject

BASE = "https://docs.example.test/confluence"
ENV = {"CONFLUENCE_BASE_URL": BASE, "CONFLUENCE_API_TOKEN": "fixture-token"}


def _page():
    return {"id": "42", "version": {"number": 3}, "body": {"storage": {"value": "fixture"}}}


def _search():
    return {
        "start": 0,
        "size": 1,
        "results": [_page()],
        "_links": {"next": "/confluence/rest/api/content/search?start=1&limit=1"},
    }


def test_search_preserves_version_locator_and_explicit_next_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def get(base, path, token, params, **kwargs):
        calls.append((base, path, params))
        return _search()

    monkeypatch.setattr(subject, "api_get", get)
    result = subject.call_tool("confluence_search", {"cql": "type=page", "limit": 1}, environ=ENV)
    value = json.loads(result["content"][0]["text"])
    evidence = value["_zekam"]
    assert calls == [
        (
            BASE,
            "/rest/api/content/search",
            {"cql": "type=page", "start": 0, "limit": 1, "expand": "space,version"},
        )
    ]
    assert evidence["pagination"]["has_more"] is True
    assert evidence["pagination"]["next_start"] == 1
    assert evidence["provenance"][0]["source_ref"].endswith(":content:42:version:3")
    assert evidence["provenance"][0]["locator"] == BASE + "/rest/api/content/42"
    assert evidence["provenance"][0]["permission_evidence"] == "authenticated-response-only"
    assert evidence["grants_authority"] is False
    assert len(calls) == 1  # No automatic request to server-provided next URL.


@pytest.mark.parametrize(
    "next_url",
    [
        "https://outside.example.test/confluence/rest/api/content/search?start=1",
        "/confluence/rest/api/other?start=1",
        "/confluence/rest/api/content/search?start=0",
        "/confluence/rest/api/content/search?start=2",
        "/confluence/rest/api/content/search?start=1&start=2",
        "/confluence/rest/api/content/search?start=true",
    ],
)
def test_search_rejects_untrusted_or_nonprogressing_next(next_url: str) -> None:
    value = _search()
    value["_links"]["next"] = next_url
    with pytest.raises(RuntimeError, match="pagination"):
        subject._confluence_page(BASE, value, start=0, limit=1)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(start=1),
        lambda p: p.update(size=True),
        lambda p: p.update(size=2),
        lambda p: p["results"][0].update(version={"number": True}),
        lambda p: p["results"][0].update(version={}),
        lambda p: p["results"][0].update(id="../../other"),
    ],
)
def test_search_rejects_drift_and_missing_version(mutate) -> None:
    value = copy.deepcopy(_search())
    mutate(value)
    with pytest.raises(RuntimeError):
        subject._confluence_page(BASE, value, start=0, limit=1)


def test_page_identity_drift_and_version_change(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _page()
    monkeypatch.setattr(subject, "api_get", lambda *_args, **_kwargs: row)
    first = json.loads(
        subject.call_tool("confluence_get_page", {"page_id": "42"}, environ=ENV)["content"][0][
            "text"
        ]
    )
    row["version"]["number"] = 4
    second = json.loads(
        subject.call_tool("confluence_get_page", {"page_id": "42"}, environ=ENV)["content"][0][
            "text"
        ]
    )
    assert (
        first["_zekam"]["provenance"]["response_digest"]
        != second["_zekam"]["provenance"]["response_digest"]
    )
    with pytest.raises(RuntimeError, match="identity drift"):
        subject.call_tool("confluence_get_page", {"page_id": "43"}, environ=ENV)


@pytest.mark.parametrize("start", [True, -1, 1_000_001, "1"])
def test_search_invalid_pagination_rejected_before_transport(
    start, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subject, "api_get", lambda *_args, **_kwargs: pytest.fail("unexpected network")
    )
    with pytest.raises(RuntimeError):
        subject.call_tool("confluence_search", {"cql": "type=page", "start": start}, environ=ENV)


def test_api_response_read_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response(io.BytesIO):
        def read(self, size=-1):
            assert size == subject.MAX_API_RESPONSE_BYTES + 1
            return super().read(size)

    class Opener:
        def open(self, *_args, **_kwargs):
            return Response(b" " * (subject.MAX_API_RESPONSE_BYTES + 1))

    monkeypatch.setattr(subject.urllib.request, "build_opener", lambda *_: Opener())
    with pytest.raises(RuntimeError, match="API istegi basarisiz"):
        subject.api_get(BASE, "/rest/api/content/search", "CONFLUENCE_API_TOKEN", environ=ENV)


@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_http_status_preserved_without_body_or_credential(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Opener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.HTTPError(
                "https://private.example.test/secret",
                status,
                "fixture-token",
                {},
                io.BytesIO(b"private response"),
            )

    monkeypatch.setattr(subject.urllib.request, "build_opener", lambda *_: Opener())
    with pytest.raises(RuntimeError) as error:
        subject.api_get(BASE, "/rest/api/content/search", "CONFLUENCE_API_TOKEN", environ=ENV)
    assert str(error.value) == f"API HTTP {status}; retry_allowed=false"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@example.test",
        "https://example.test/?token=x",
        "https://example.test/#token",
        "https://example.test/a/../b",
    ],
)
def test_api_base_rejects_embedded_authority_and_path_traversal(url: str) -> None:
    with pytest.raises(RuntimeError, match="gecersiz"):
        subject._base_url({"API": url}, "API", BASE)


def _issue():
    return {
        "id": "10042",
        "key": "DEMO-42",
        "fields": {
            "summary": "Example",
            "updated": "2026-10-16T12:00:00.000+0300",
            "attachment": [
                {
                    "id": "123",
                    "filename": "example.pdf",
                    "size": 2048,
                    "mimeType": "application/pdf",
                    "author": {"emailAddress": "private@example.test"},
                    "content": "https://untrusted.example.test/arbitrary",
                }
            ],
        },
    }


def test_jira_issue_exact_identity_attachment_metadata_and_site_provenance(monkeypatch):
    calls = []

    def get(base, path, token, params, **kwargs):
        calls.append((base, path, params))
        return _issue()

    monkeypatch.setattr(subject, "api_get", get)
    env = {"JIRA_BASE_URL": "https://jira.example.test/jira"}
    result = subject.call_tool("jira_get_issue", {"issue_key": "DEMO-42"}, environ=env)
    document = json.loads(result["content"][0]["text"])
    evidence = document["_zekam"]
    assert len(calls) == 1 and calls[0][1] == "/rest/api/2/issue/DEMO-42"
    assert "attachment" in calls[0][2]["fields"]
    assert evidence["provenance"]["locator"] == "https://jira.example.test/jira/browse/DEMO-42"
    attachment = evidence["attachments"][0]
    assert attachment["locator"] == "https://jira.example.test/jira/rest/api/2/attachment/123"
    assert attachment["content_digest"] is None and attachment["content_read"] is False
    assert evidence["attachment_content_imported"] is False
    assert "private@example.test" not in result["content"][0]["text"]
    assert "untrusted.example.test" not in result["content"][0]["text"]
    original = _issue()
    changed_site = subject._jira_issue_snapshot(
        "https://other.example.test", original, key="DEMO-42"
    )
    assert (
        changed_site["_zekam"]["provenance"]["source_ref"] != evidence["provenance"]["source_ref"]
    )
    original["fields"]["attachment"][0]["size"] = 4096
    changed = subject._jira_issue_snapshot(calls[0][0], original, key="DEMO-42")
    assert changed["_zekam"]["attachments"][0]["metadata_digest"] != attachment["metadata_digest"]


@pytest.mark.parametrize(
    "mutation",
    [
        "key",
        "id",
        "updated",
        "timezone",
        "missing-attachments",
        "duplicate-attachment",
        "filename",
        "size",
    ],
)
def test_jira_issue_metadata_drift_rejected(mutation):
    issue = _issue()
    if mutation == "key":
        issue["key"] = "OTHER-42"
    elif mutation == "id":
        issue["id"] = "../../other"
    elif mutation == "updated":
        issue["fields"].pop("updated")
    elif mutation == "timezone":
        issue["fields"]["updated"] = "2026-10-16T12:00:00"
    elif mutation == "missing-attachments":
        issue["fields"].pop("attachment")
    elif mutation == "duplicate-attachment":
        issue["fields"]["attachment"] *= 2
    elif mutation == "filename":
        issue["fields"]["attachment"][0]["filename"] = "../outside.pdf"
    elif mutation == "size":
        issue["fields"]["attachment"][0]["size"] = True
    with pytest.raises(RuntimeError):
        subject._jira_issue_snapshot("https://jira.example.test", issue, key="DEMO-42")


def test_jira_search_bound_offset_and_query_identity(monkeypatch):
    calls = []

    def get(base, path, token, params, **kwargs):
        calls.append(params)
        return {"startAt": 2, "maxResults": 1, "total": 5, "issues": [_issue()]}

    monkeypatch.setattr(subject, "api_get", get)
    result = subject.call_tool(
        "jira_search", {"jql": "project=DEMO", "start_at": 2, "max_results": 1}
    )
    document = json.loads(result["content"][0]["text"])
    page = document["_zekam"]["pagination"]
    assert page["next_start"] == 3 and page["has_more"] is True
    assert page["consistent_snapshot"] is False
    assert len(calls) == 1 and calls[0]["startAt"] == 2
    changed = subject._jira_search_page(
        subject.DEFAULT_JIRA_BASE_URL,
        {"startAt": 2, "maxResults": 1, "total": 5, "issues": [_issue()]},
        start=2,
        limit=1,
        jql="project=OTHER",
    )
    assert changed["_zekam"]["query_digest"] != document["_zekam"]["query_digest"]


@pytest.mark.parametrize("mutation", ["offset", "total", "limit", "empty", "duplicate"])
def test_jira_search_rejects_nonprogressing_or_forged_page(mutation):
    page = {"startAt": 0, "maxResults": 2, "total": 3, "issues": [_issue()]}
    if mutation == "offset":
        page["startAt"] = 1
    elif mutation == "total":
        page["total"] = True
    elif mutation == "limit":
        page["maxResults"] = 3
    elif mutation == "empty":
        page["issues"] = []
    elif mutation == "duplicate":
        page["issues"] *= 2
    with pytest.raises(RuntimeError, match="pagination"):
        subject._jira_search_page(
            "https://jira.example.test", page, start=0, limit=2, jql="project=DEMO"
        )


@pytest.mark.parametrize("raw", [b'{"id":"1","id":"2"}', b'{"total":NaN}', b"\xff"])
def test_http_json_ambiguity_rejected_without_raw_response(monkeypatch, raw):
    class Opener:
        def open(self, *args, **kwargs):
            return io.BytesIO(raw)

    monkeypatch.setattr(subject.urllib.request, "build_opener", lambda *_: Opener())
    with pytest.raises(RuntimeError, match="API istegi basarisiz") as error:
        subject.api_get(BASE, "/rest/api/content/search", "CONFLUENCE_API_TOKEN", environ=ENV)
    assert error.value.__suppress_context__ is True
