"""Small read-only stdio MCP server exposing the structural code-graph queries.

The five tools simply forward onto the ``code_graph_query`` read-only functions.
They never build, mutate or maintain a graph; the graph store is always opened
in immutable ``read_only=True`` mode.  A project is resolved by slug/alias
through the local operational store, exactly like the ``project graph`` CLI.

Tools
-----
- ``zekam_code_find``      -> ``graph_find``
- ``zekam_code_outline``   -> ``graph_outline``
- ``zekam_code_impact``    -> ``graph_impact``
- ``zekam_code_map``       -> ``graph_map``
- ``zekam_code_freshness`` -> ``graph_freshness``
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from zekam.application.code_graph import graph_store_path
from zekam.application.code_graph_query import (
    graph_find,
    graph_freshness,
    graph_impact,
    graph_map,
    graph_outline,
)
from zekam.application.composition import build_context
from zekam.infrastructure.sqlite.code_graph import SQLiteCodeGraphStore
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "zekam_code_find",
        "description": "Salt-okunur graph symbol/dosya exact+lexical eslesmesi.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Proje slug veya alias"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["project", "query"],
        },
    },
    {
        "name": "zekam_code_outline",
        "description": "Salt-okunur dosya hiyerarsik outline'i.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Proje slug veya alias"},
                "relative_path": {"type": "string"},
            },
            "required": ["project", "relative_path"],
        },
    },
    {
        "name": "zekam_code_impact",
        "description": "Salt-okunur symbol blast radius'i (dependency, contains haric).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Proje slug veya alias"},
                "symbol_qualified": {"type": "string"},
                "max_depth": {"type": "integer", "default": 100},
            },
            "required": ["project", "symbol_qualified"],
        },
    },
    {
        "name": "zekam_code_map",
        "description": "Salt-okunur repository map (per-file aggregation).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Proje slug veya alias"},
            },
            "required": ["project"],
        },
    },
    {
        "name": "zekam_code_freshness",
        "description": "Graph generation binding durumu (ready/stale/unavailable).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Proje slug veya alias"},
                "source_revision": {"type": "string"},
                "tree_digest": {"type": "string"},
            },
            "required": ["project", "source_revision", "tree_digest"],
        },
    },
)


def _text_result(value: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}]}


def _graph_store(project_ref: str) -> tuple[SQLiteCodeGraphStore, str]:
    """Resolve a project (slug/alias), then open its graph store read-only."""
    context = build_context()
    with SQLiteOperationalStore(
        context.settings.database.sqlite_path(context.home)
    ).unit_of_work() as uow:
        record = uow.resolve_project(project_ref)
        project_id = record.id
        slug = record.slug
        uow.commit()
    store_path = graph_store_path(context.home, slug)
    if not store_path.is_file():
        raise RuntimeError("Proje graph store bulunamadi; once 'project graph build' calistirin")
    return SQLiteCodeGraphStore(store_path, read_only=True), project_id


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    project = str(args["project"])
    if name == "zekam_code_find":
        store, project_id = _graph_store(project)
        try:
            return _text_result(
                graph_find(
                    store, project_id, str(args["query"]), limit=int(args.get("limit", 50))
                )
            )
        finally:
            store.close()
    if name == "zekam_code_outline":
        store, project_id = _graph_store(project)
        try:
            return _text_result(
                graph_outline(store, project_id, str(args["relative_path"]))
            )
        finally:
            store.close()
    if name == "zekam_code_impact":
        store, project_id = _graph_store(project)
        try:
            return _text_result(
                graph_impact(
                    store,
                    project_id,
                    str(args["symbol_qualified"]),
                    max_depth=int(args.get("max_depth", 100)),
                )
            )
        finally:
            store.close()
    if name == "zekam_code_map":
        store, project_id = _graph_store(project)
        try:
            return _text_result(graph_map(store, project_id))
        finally:
            store.close()
    if name == "zekam_code_freshness":
        store, project_id = _graph_store(project)
        try:
            return _text_result(
                graph_freshness(
                    store,
                    project_id,
                    str(args["source_revision"]),
                    str(args["tree_digest"]),
                )
            )
        finally:
            store.close()
    raise RuntimeError(f"Bilinmeyen arac: {name}")


def response_for(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "zekam-code-graph-local", "version": "1.0.0"},
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
