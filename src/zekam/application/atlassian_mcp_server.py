"""Small read-only stdio MCP server for the configured Jira and Confluence APIs."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, TextIO

from zekam.domain.canonical import canonical_bytes, digest, digest_of_bytes, parse_digest

DEFAULT_JIRA_BASE_URL = "https://itrack.innova.com.tr"
DEFAULT_CONFLUENCE_BASE_URL = "https://ftaconfluence.innova.com.tr"
MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PAGE_START = 1_000_000
MAX_ISSUE_ATTACHMENTS = 200
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_ATTACHMENT_UNITS = 256
MAX_ATTACHMENT_TEXT_BYTES = 384 * 1024

TOOLS = (
    {
        "name": "jira_search",
        "description": "Jira'da JQL ile salt-okunur issue aramasi yapar.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "jql": {"type": "string"},
                "max_results": {"type": "integer", "default": 10},
                "start_at": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_PAGE_START,
                    "default": 0,
                },
            },
            "required": ["jql"],
        },
    },
    {
        "name": "jira_get_issue",
        "description": "Jira issue ayrintilarini getirir.",
        "inputSchema": {
            "type": "object",
            "properties": {"issue_key": {"type": "string"}},
            "required": ["issue_key"],
        },
    },
    {
        "name": "jira_read_attachment",
        "description": "Exact issue/ek metadata digest'iyle eki bounded okur; parser/secret/PII kontrolunden gecen locator'li metni verir. Knowledge yazmaz.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string"},
                "attachment_id": {"type": "string"},
                "expected_metadata_digest": {"type": "string"},
            },
            "required": ["issue_key", "attachment_id", "expected_metadata_digest"],
            "additionalProperties": False,
        },
    },
    {
        "name": "confluence_search",
        "description": "Confluence'ta CQL ile salt-okunur icerik aramasi yapar.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cql": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
                "start": {"type": "integer", "minimum": 0, "maximum": MAX_PAGE_START, "default": 0},
            },
            "required": ["cql"],
        },
    },
    {
        "name": "confluence_get_page",
        "description": "Confluence sayfasini icerik ve surum bilgisiyle getirir.",
        "inputSchema": {
            "type": "object",
            "properties": {"page_id": {"type": "string"}},
            "required": ["page_id"],
        },
    },
)


def _base_url(environ: dict[str, str], name: str, default: str) -> str:
    value = environ.get(name, default).rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(part in {".", ".."} for part in urllib.parse.unquote(parsed.path).split("/"))
    ):
        raise RuntimeError(f"{name} gecersiz")
    try:
        parsed.port
    except ValueError as exc:
        raise RuntimeError(f"{name} gecersiz") from exc
    return value


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        original = urllib.parse.urlsplit(req.full_url)
        redirected = urllib.parse.urlsplit(newurl)
        if (
            redirected.username is not None
            or redirected.password is not None
            or redirected.fragment
            or redirected.scheme != "https"
            or (original.scheme, original.hostname, original.port)
            != (
                redirected.scheme,
                redirected.hostname,
                redirected.port,
            )
        ):
            raise urllib.error.HTTPError(newurl, code, "cross-origin redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document = {}
    for key, value in pairs:
        if key in document:
            raise RuntimeError("API duplicate JSON key")
        document[key] = value
    return document


def _reject_json_constant(_: str) -> None:
    raise RuntimeError("API nonfinite JSON")


class _AttachmentRedirectHandler(_SameOriginRedirectHandler):
    def __init__(self, allowed_paths: tuple[str, ...]):
        super().__init__()
        self.allowed_paths = allowed_paths

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        path = target.path
        if target.query or target.fragment:
            raise urllib.error.HTTPError(
                newurl, code, "attachment redirect query refused", headers, fp
            )
        if not any(path == root or path.startswith(root + "/") for root in self.allowed_paths):
            raise urllib.error.HTTPError(
                newurl, code, "attachment redirect scope refused", headers, fp
            )
        if any(part in {".", ".."} for part in urllib.parse.unquote(path).split("/")):
            raise urllib.error.HTTPError(
                newurl, code, "attachment redirect path refused", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def api_get(
    base_url: str,
    path: str,
    token_name: str,
    params: dict[str, Any] | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> Any:
    env = dict(os.environ) if environ is None else environ
    token = env.get(token_name)
    if not token:
        raise RuntimeError(f"{token_name} ortam degiskeni bulunamadi")
    query = "?" + urllib.parse.urlencode(params) if params else ""
    request = urllib.request.Request(
        base_url + path + query,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(_SameOriginRedirectHandler())
        with opener.open(request, timeout=45) as response:
            payload = response.read(MAX_API_RESPONSE_BYTES + 1)
            if len(payload) > MAX_API_RESPONSE_BYTES:
                raise RuntimeError("API response bounded limit exceeded")
            return json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
    except urllib.error.HTTPError as exc:
        # Never include response body, URL, token or upstream exception text.
        raise RuntimeError(f"API HTTP {exc.code}; retry_allowed=false") from None
    except Exception as exc:
        raise RuntimeError(f"API istegi basarisiz: {type(exc).__name__}") from None


def _text_result(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}]}


def _bounded_int(
    args: dict[str, Any], name: str, default: int, maximum: int, minimum: int = 0
) -> int:
    value = args.get(name, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise RuntimeError(f"{name} bounded tamsayi olmali")
    return value


def _jira_key(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z][A-Z0-9]{1,15}-[0-9]{1,7}", value) is None:
        raise RuntimeError("Exact Jira issue key gerekli")
    return value


def _numeric_id(value: Any) -> str:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 32:
        raise RuntimeError("Jira bounded numeric identity gerekli")
    return value


def _jira_provenance(base: str, row: Any) -> dict[str, Any]:
    if not isinstance(row, dict) or not isinstance(row.get("fields"), dict):
        raise RuntimeError("Jira issue response gecersiz")
    key, issue_id = _jira_key(row.get("key")), _numeric_id(row.get("id"))
    updated = row["fields"].get("updated")
    if not isinstance(updated, str) or len(updated) > 64:
        raise RuntimeError("Jira updated revision evidence gerekli")
    try:
        timestamp = dt.datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("Jira updated revision evidence gecersiz") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise RuntimeError("Jira updated timezone gerekli")
    response_digest = digest(row)
    return {
        "source_ref": f"jira:{digest(base)[7:]}:issue:{issue_id}:response:{response_digest[7:]}",
        "issue_id": issue_id,
        "issue_key": key,
        "updated": updated,
        "locator": base + "/browse/" + key,
        "response_digest": response_digest,
        "revision_kind": "observed-updated-and-response-digest",
        "permission_evidence": "authenticated-response-only",
        "grants_authority": False,
    }


def _jira_search_page(base: str, value: Any, *, start: int, limit: int, jql: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("issues"), list):
        raise RuntimeError("Jira search response gecersiz")
    rows = value["issues"]
    total, returned_limit = value.get("total"), value.get("maxResults")
    if (
        type(value.get("startAt")) is not int
        or value["startAt"] != start
        or type(total) is not int
        or total < 0
        or type(returned_limit) is not int
        or not 1 <= returned_limit <= limit
        or len(rows) > returned_limit
        or start + len(rows) > max(start, total)
    ):
        raise RuntimeError("Jira pagination scope/size drift")
    provenance = [_jira_provenance(base, row) for row in rows]
    if len({item["issue_id"] for item in provenance}) != len(rows) or len(
        {item["issue_key"] for item in provenance}
    ) != len(rows):
        raise RuntimeError("Jira pagination duplicate issue identity")
    next_start = start + len(rows)
    has_more = next_start < total
    if has_more and not rows:
        raise RuntimeError("Jira pagination ilerlemiyor")
    cursor_blocked = has_more and next_start > MAX_PAGE_START
    return value | {
        "_zekam": {
            "read_only": True,
            "grants_authority": False,
            "query_digest": digest({"site": base, "jql": jql}),
            "provenance": provenance,
            "pagination": {
                "start": start,
                "limit": limit,
                "returned": len(rows),
                "total_visible": total,
                "has_more": has_more,
                "next_start": next_start if has_more and not cursor_blocked else None,
                "continuation_blocked": "cursor-limit" if cursor_blocked else None,
                "consistent_snapshot": False,
            },
        }
    }


def _jira_issue_snapshot(base: str, value: Any, *, key: str) -> dict[str, Any]:
    provenance = _jira_provenance(base, value)
    if provenance["issue_key"] != key:
        raise RuntimeError("Jira issue identity drift")
    attachments = value["fields"].get("attachment")
    if not isinstance(attachments, list) or len(attachments) > MAX_ISSUE_ATTACHMENTS:
        raise RuntimeError("Jira attachment metadata eksik veya bounded sinir disi")
    items = []
    seen = set()
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise RuntimeError("Jira attachment metadata gecersiz")
        attachment_id = _numeric_id(attachment.get("id"))
        size = attachment.get("size")
        filename, media_type = attachment.get("filename"), attachment.get("mimeType")
        if (
            attachment_id in seen
            or type(size) is not int
            or size < 0
            or not isinstance(filename, str)
            or not 1 <= len(filename) <= 512
            or filename in {".", ".."}
            or any(ord(char) < 32 or char in "/\\" for char in filename)
            or not isinstance(media_type, str)
            or not 1 <= len(media_type) <= 128
            or any(ord(char) < 32 for char in media_type)
        ):
            raise RuntimeError("Jira attachment metadata identity/size drift")
        seen.add(attachment_id)
        # This digest describes metadata, never downloaded bytes. Avoid author,
        # thumbnail and arbitrary server-provided download URL in the projection.
        metadata = {
            "id": attachment_id,
            "filename": filename,
            "size": size,
            "media_type": media_type,
        }
        metadata_digest = digest(metadata)
        items.append(
            metadata
            | {
                "source_ref": f"jira:{digest(base)[7:]}:issue:{provenance['issue_id']}:attachment:{attachment_id}:metadata:{metadata_digest[7:]}",
                "locator": base + "/rest/api/2/attachment/" + attachment_id,
                "metadata_digest": metadata_digest,
                "content_digest": None,
                "content_read": False,
                "grants_authority": False,
            }
        )
    projection = value | {
        "fields": value["fields"]
        | {
            "attachment": [
                {
                    "id": item["id"],
                    "filename": item["filename"],
                    "size": item["size"],
                    "mimeType": item["media_type"],
                }
                for item in items
            ]
        }
    }
    return projection | {
        "_zekam": {
            "read_only": True,
            "grants_authority": False,
            "provenance": provenance,
            "attachments": items,
            "comments_complete": False,
            "attachment_content_imported": False,
            "projection_digest": digest(projection),
        }
    }


def _attachment_payload(base: str, attachment: dict[str, Any], *, environ: dict[str, str]) -> bytes:
    """Download only the exact same-site attachment URL bound by issue metadata."""
    content = attachment.get("content")
    if not isinstance(content, str) or not content or len(content) > 8192:
        raise RuntimeError("Jira attachment content locator gerekli")
    try:
        source, target = urllib.parse.urlsplit(base), urllib.parse.urlsplit(content)
        same_origin = (target.scheme, target.hostname, target.port) == (
            source.scheme,
            source.hostname,
            source.port,
        )
    except ValueError:
        raise RuntimeError("Jira attachment content locator gecersiz") from None
    attachment_id = _numeric_id(attachment.get("id"))
    expected_paths = (
        source.path + "/secure/attachment/" + attachment_id,
        source.path + "/rest/api/2/attachment/content/" + attachment_id,
    )
    if (
        not same_origin
        or target.scheme != "https"
        or target.username is not None
        or target.password is not None
        or target.query
        or target.fragment
        or not any(
            target.path == path or target.path.startswith(path + "/") for path in expected_paths
        )
        or any(part in {".", ".."} for part in urllib.parse.unquote(target.path).split("/"))
    ):
        raise RuntimeError("Jira attachment content locator scope drift")
    token = environ.get("JIRA_API_TOKEN")
    if not token:
        raise RuntimeError("JIRA_API_TOKEN ortam degiskeni bulunamadi")
    size = attachment["size"]
    if type(size) is not int or not 1 <= size <= MAX_ATTACHMENT_BYTES:
        raise RuntimeError("Jira attachment boyut siniri asildi veya ek bos")
    request = urllib.request.Request(
        content,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/octet-stream",
            "Accept-Encoding": "identity",
        },
    )
    try:
        with urllib.request.build_opener(_AttachmentRedirectHandler(expected_paths)).open(
            request, timeout=45
        ) as response:
            encoding = response.headers.get("Content-Encoding", "identity")
            if encoding.casefold() != "identity":
                raise RuntimeError("Jira attachment HTTP encoding desteklenmiyor")
            media_type = (
                response.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
            )
            if media_type not in {attachment["mimeType"].casefold(), "application/octet-stream"}:
                raise RuntimeError("Jira attachment HTTP content type drift")
            length = response.headers.get("Content-Length")
            if length is not None and (
                not length.isascii() or not length.isdigit() or int(length) != size
            ):
                raise RuntimeError("Jira attachment HTTP length drift")
            payload = response.read(min(size, MAX_ATTACHMENT_BYTES) + 1)
            if len(payload) != size:
                raise RuntimeError("Jira attachment byte size drift")
            return payload
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Attachment HTTP {exc.code}; retry_allowed=false") from None
    except Exception as exc:
        raise RuntimeError(f"Jira attachment read basarisiz: {type(exc).__name__}") from None


def _read_jira_attachment(
    base: str, args: dict[str, Any], *, environ: dict[str, str]
) -> dict[str, Any]:
    from zekam.application.knowledge_file_plane import assert_public_safe_projection
    from zekam.application.knowledge_parsers import default_router
    from zekam.application.unit_test_secret_scan import find_secret_value
    from zekam.domain.knowledge import SourceFormat
    from zekam.infrastructure.knowledge.document_parsers import PdfParser

    if set(args) != {"issue_key", "attachment_id", "expected_metadata_digest"}:
        raise RuntimeError("Jira attachment read exact argumanlar ister")
    key, attachment_id = _jira_key(args["issue_key"]), _numeric_id(args["attachment_id"])
    expected = args["expected_metadata_digest"]
    parse_digest(expected)

    def snapshot():
        row = api_get(
            base,
            f"/rest/api/2/issue/{key}",
            "JIRA_API_TOKEN",
            {"fields": "updated,attachment"},
            environ=environ,
        )
        view = _jira_issue_snapshot(base, row, key=key)
        matches = [item for item in view["_zekam"]["attachments"] if item["id"] == attachment_id]
        if len(matches) != 1 or matches[0]["metadata_digest"] != expected:
            raise RuntimeError("Jira attachment issue ownership/metadata drift")
        return row, view, matches[0]

    before, _, metadata = snapshot()
    original = next(item for item in before["fields"]["attachment"] if item["id"] == attachment_id)
    extension = metadata["filename"].rsplit(".", 1)[-1].casefold()
    formats = {
        "txt": (SourceFormat.TXT, {"text/plain"}),
        "md": (SourceFormat.MARKDOWN, {"text/plain", "text/markdown"}),
        "docx": (
            SourceFormat.DOCX,
            {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        ),
        "pdf": (SourceFormat.PDF, {"application/pdf"}),
    }
    if extension not in formats or metadata["media_type"].casefold() not in formats[extension][1]:
        raise RuntimeError("Jira attachment format/mime desteklenmiyor")
    payload = _attachment_payload(base, original, environ=environ)
    after, after_view, _ = snapshot()
    if before["id"] != after["id"] or before["fields"]["updated"] != after["fields"]["updated"]:
        raise RuntimeError("Jira issue attachment read sirasinda degisti")
    router = default_router()
    router.parsers[SourceFormat.PDF] = PdfParser(allow_ocr=False)
    parser = router.resolve(formats[extension][0])
    units = parser.parse(payload)
    if not 1 <= len(units) <= MAX_ATTACHMENT_UNITS:
        raise RuntimeError("Jira attachment normalized unit siniri asildi")
    normalized = [item.body() for item in units]
    normalized_bytes = canonical_bytes(normalized)
    if (
        len(normalized_bytes) > MAX_ATTACHMENT_TEXT_BYTES
        or find_secret_value(normalized) is not None
    ):
        raise RuntimeError("Jira attachment text bounded/secret kontrolunu gecemedi")
    assert_public_safe_projection(normalized_bytes, relative_path="jira/attachment-normalized.json")
    content_digest = digest_of_bytes(payload)
    parser_profile = parser.parser_profile
    result = {
        "schema": "zekam-jira-attachment-read/v1",
        "issue_key": key,
        "issue_provenance": after_view["_zekam"]["provenance"],
        "attachment": metadata
        | {
            "content_digest": content_digest,
            "content_read": True,
            "source_ref": f"jira:{digest(base)[7:]}:issue:{before['id']}:attachment:{attachment_id}:content:{content_digest[7:]}",
        },
        "parser_ref": parser.parser_ref,
        "parser_version": parser.parser_version,
        "parser_profile_digest": digest(parser_profile),
        "units": normalized,
        "normalized_digest": digest_of_bytes(normalized_bytes),
        "source_kind": "untrusted-reference",
        "knowledge_imported": False,
        "read_only": True,
        "grants_authority": False,
    }
    assert_public_safe_projection(
        canonical_bytes(result), relative_path="jira/attachment-read.json"
    )
    return result


def _confluence_provenance(base: str, row: Any) -> dict[str, Any]:
    if (
        not isinstance(row, dict)
        or not isinstance(row.get("id"), str)
        or not row["id"].isascii()
        or not row["id"].isdigit()
        or len(row["id"]) > 32
    ):
        raise RuntimeError("Confluence content identity gecersiz")
    version = row.get("version")
    number = version.get("number") if isinstance(version, dict) else None
    if type(number) is not int or number < 1:
        raise RuntimeError("Confluence version evidence gerekli")
    return {
        "source_ref": f"confluence:{digest(base)[7:]}:content:{row['id']}:version:{number}",
        "locator": base + "/rest/api/content/" + row["id"],
        "version": number,
        "response_digest": digest(row),
        "permission_evidence": "authenticated-response-only",
        "grants_authority": False,
    }


def _confluence_page(base: str, value: Any, *, start: int, limit: int) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise RuntimeError("Confluence search response gecersiz")
    rows = value["results"]
    identities = [row.get("id") for row in rows if isinstance(row, dict)]
    if any(not isinstance(item, str) for item in identities) or len(set(identities)) != len(rows):
        raise RuntimeError("Confluence pagination duplicate veya gecersiz content identity")
    if len(rows) > limit or type(value.get("start")) is not int or value["start"] != start:
        raise RuntimeError("Confluence pagination scope drift")
    size = value.get("size")
    if type(size) is not int or size != len(rows):
        raise RuntimeError("Confluence pagination size drift")
    links = value.get("_links", {})
    if not isinstance(links, dict):
        raise RuntimeError("Confluence pagination links gecersiz")
    following = links.get("next")
    next_start = None
    if following is not None:
        if not isinstance(following, str) or not following or len(following) > 8192:
            raise RuntimeError("Confluence pagination next gecersiz")
        next_url = urllib.parse.urlsplit(urllib.parse.urljoin(base + "/", following))
        origin = urllib.parse.urlsplit(base)
        expected_path = origin.path + "/rest/api/content/search"
        if (
            (next_url.scheme, next_url.hostname, next_url.port)
            != (origin.scheme, origin.hostname, origin.port)
            or next_url.username is not None
            or next_url.password is not None
            or next_url.fragment
            or next_url.path != expected_path
        ):
            raise RuntimeError("Confluence pagination next scope drift")
        query = urllib.parse.parse_qs(next_url.query, keep_blank_values=True)
        positions = query.get("start", [])
        if len(positions) != 1 or not positions[0].isascii() or not positions[0].isdigit():
            raise RuntimeError("Confluence pagination next start gecersiz")
        next_start = int(positions[0])
        if not start < next_start <= MAX_PAGE_START or next_start != start + size or size == 0:
            raise RuntimeError("Confluence pagination next ilerlemiyor")
    # Do not follow server-provided URLs. The caller repeats its own CQL with start.
    return value | {
        "_zekam": {
            "read_only": True,
            "grants_authority": False,
            "pagination": {
                "start": start,
                "limit": limit,
                "returned": size,
                "has_more": next_start is not None,
                "next_start": next_start,
                "end_of_visible_page_chain": next_start is None,
            },
            "provenance": [_confluence_provenance(base, row) for row in rows],
        }
    }


def call_tool(
    name: str, args: dict[str, Any], *, environ: dict[str, str] | None = None
) -> dict[str, Any]:
    env = dict(os.environ) if environ is None else environ
    jira = _base_url(env, "JIRA_BASE_URL", DEFAULT_JIRA_BASE_URL)
    confluence = _base_url(env, "CONFLUENCE_BASE_URL", DEFAULT_CONFLUENCE_BASE_URL)
    if name in {"jira_search", "confluence_search"}:
        field = "jql" if name == "jira_search" else "cql"
        value = args.get(field)
        if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 8192:
            raise RuntimeError(f"{field} bounded metin olmali")
    if name == "jira_search":
        start = _bounded_int(args, "start_at", 0, MAX_PAGE_START)
        limit = _bounded_int(args, "max_results", 10, 50, 1)
        return _text_result(
            _jira_search_page(
                jira,
                api_get(
                    jira,
                    "/rest/api/2/search",
                    "JIRA_API_TOKEN",
                    {
                        "jql": str(args["jql"]),
                        "maxResults": limit,
                        "startAt": start,
                        "fields": "summary,status,assignee,issuetype,project,updated",
                    },
                    environ=env,
                ),
                start=start,
                limit=limit,
                jql=args["jql"],
            )
        )
    if name == "jira_get_issue":
        key = _jira_key(args.get("issue_key"))
        return _text_result(
            _jira_issue_snapshot(
                jira,
                api_get(
                    jira,
                    f"/rest/api/2/issue/{key}",
                    "JIRA_API_TOKEN",
                    {
                        "fields": "summary,status,description,assignee,reporter,issuetype,"
                        "project,priority,labels,comment,updated,attachment"
                    },
                    environ=env,
                ),
                key=key,
            )
        )
    if name == "jira_read_attachment":
        return _text_result(_read_jira_attachment(jira, args, environ=env))
    if name == "confluence_search":
        start = _bounded_int(args, "start", 0, MAX_PAGE_START)
        limit = _bounded_int(args, "limit", 10, 50, 1)
        return _text_result(
            _confluence_page(
                confluence,
                api_get(
                    confluence,
                    "/rest/api/content/search",
                    "CONFLUENCE_API_TOKEN",
                    {
                        "cql": str(args["cql"]),
                        "limit": limit,
                        "start": start,
                        "expand": "space,version",
                    },
                    environ=env,
                ),
                start=start,
                limit=limit,
            )
        )
    if name == "confluence_get_page":
        page_id = args.get("page_id")
        if (
            not isinstance(page_id, str)
            or not page_id.isascii()
            or not page_id.isdigit()
            or len(page_id) > 32
        ):
            raise RuntimeError("Confluence page_id bounded sayisal kimlik olmali")
        value = api_get(
            confluence,
            f"/rest/api/content/{page_id}",
            "CONFLUENCE_API_TOKEN",
            {"expand": "body.storage,space,version"},
            environ=env,
        )
        provenance = _confluence_provenance(confluence, value)
        if value["id"] != page_id:
            raise RuntimeError("Confluence page identity drift")
        return _text_result(
            value
            | {"_zekam": {"read_only": True, "grants_authority": False, "provenance": provenance}}
        )
    raise RuntimeError("Bilinmeyen salt-okunur arac")


def response_for(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "innova-atlassian-local", "version": "1.2.0"},
        }
    elif method == "notifications/initialized":
        return None
    elif method == "tools/list":
        result = {"tools": list(TOOLS)}
    elif method == "tools/call":
        try:
            params = request.get("params") or {}
            result = call_tool(str(params["name"]), dict(params.get("arguments") or {}))
        except Exception as exc:
            result = {
                "isError": True,
                "content": [{"type": "text", "text": str(exc)}],
            }
    else:
        if request_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "Method not found"},
        }
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve(input_stream: TextIO = sys.stdin, output_stream: TextIO = sys.stdout) -> None:
    for line in input_stream:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("JSON-RPC request object olmali")
            response = response_for(request)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), file=output_stream, flush=True)
        except Exception as exc:
            error = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(exc)},
            }
            print(json.dumps(error, ensure_ascii=False), file=output_stream, flush=True)
