"""Small read-only stdio MCP server for the configured Jira and Confluence APIs."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, TextIO

from zekam.domain.canonical import digest

DEFAULT_JIRA_BASE_URL = "https://itrack.innova.com.tr"
DEFAULT_CONFLUENCE_BASE_URL = "https://ftaconfluence.innova.com.tr"
MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PAGE_START = 1_000_000

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
            return json.loads(payload.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Never include response body, URL, token or upstream exception text.
        raise RuntimeError(f"API HTTP {exc.code}; retry_allowed=false") from None
    except Exception as exc:
        raise RuntimeError(f"API istegi basarisiz: {type(exc).__name__}") from exc


def _text_result(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}]}


def _bounded_int(
    args: dict[str, Any], name: str, default: int, maximum: int, minimum: int = 0
) -> int:
    value = args.get(name, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise RuntimeError(f"{name} bounded tamsayi olmali")
    return value


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
        return _text_result(
            api_get(
                jira,
                "/rest/api/2/search",
                "JIRA_API_TOKEN",
                {
                    "jql": str(args["jql"]),
                    "maxResults": _bounded_int(args, "max_results", 10, 50, 1),
                    "startAt": _bounded_int(args, "start_at", 0, MAX_PAGE_START),
                    "fields": "summary,status,assignee,issuetype,project,updated",
                },
                environ=env,
            )
        )
    if name == "jira_get_issue":
        key = args.get("issue_key")
        if not isinstance(key, str) or re.fullmatch(r"[A-Z][A-Z0-9]{1,15}-[0-9]{1,7}", key) is None:
            raise RuntimeError("Exact Jira issue key gerekli")
        return _text_result(
            api_get(
                jira,
                f"/rest/api/2/issue/{key}",
                "JIRA_API_TOKEN",
                {
                    "fields": "summary,status,description,assignee,reporter,issuetype,"
                    "project,priority,labels,comment,updated"
                },
                environ=env,
            )
        )
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
            "serverInfo": {"name": "innova-atlassian-local", "version": "1.1.0"},
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
