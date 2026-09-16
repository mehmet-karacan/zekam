"""Small read-only stdio MCP server for the configured Jira and Confluence APIs."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, TextIO

DEFAULT_JIRA_BASE_URL = "https://itrack.innova.com.tr"
DEFAULT_CONFLUENCE_BASE_URL = "https://ftaconfluence.innova.com.tr"

TOOLS = (
    {
        "name": "jira_search",
        "description": "Jira'da JQL ile salt-okunur issue aramasi yapar.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "jql": {"type": "string"},
                "max_results": {"type": "integer", "default": 10},
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
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError(f"{name} gecersiz")
    return value


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        original = urllib.parse.urlsplit(req.full_url)
        redirected = urllib.parse.urlsplit(newurl)
        if redirected.scheme != "https" or (original.scheme, original.hostname, original.port) != (
            redirected.scheme,
            redirected.hostname,
            redirected.port,
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
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"API istegi basarisiz: {type(exc).__name__}") from exc


def _text_result(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}]}


def call_tool(
    name: str, args: dict[str, Any], *, environ: dict[str, str] | None = None
) -> dict[str, Any]:
    env = dict(os.environ) if environ is None else environ
    jira = _base_url(env, "JIRA_BASE_URL", DEFAULT_JIRA_BASE_URL)
    confluence = _base_url(env, "CONFLUENCE_BASE_URL", DEFAULT_CONFLUENCE_BASE_URL)
    if name == "jira_search":
        return _text_result(
            api_get(
                jira,
                "/rest/api/2/search",
                "JIRA_API_TOKEN",
                {
                    "jql": str(args["jql"]),
                    "maxResults": min(max(int(args.get("max_results", 10)), 1), 50),
                    "fields": "summary,status,assignee,issuetype,project,updated",
                },
                environ=env,
            )
        )
    if name == "jira_get_issue":
        key = urllib.parse.quote(str(args["issue_key"]), safe="")
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
        return _text_result(
            api_get(
                confluence,
                "/rest/api/content/search",
                "CONFLUENCE_API_TOKEN",
                {
                    "cql": str(args["cql"]),
                    "limit": min(max(int(args.get("limit", 10)), 1), 50),
                    "expand": "space,version",
                },
                environ=env,
            )
        )
    if name == "confluence_get_page":
        page_id = urllib.parse.quote(str(args["page_id"]), safe="")
        return _text_result(
            api_get(
                confluence,
                f"/rest/api/content/{page_id}",
                "CONFLUENCE_API_TOKEN",
                {"expand": "body.storage,space,version"},
                environ=env,
            )
        )
    raise RuntimeError(f"Bilinmeyen arac: {name}")


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
