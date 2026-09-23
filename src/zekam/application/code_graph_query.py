"""Read-only structural code-graph query service (G3).

Scope
-----
A pure read-only query layer over :class:`GraphReadPort`.  It never mutates a
store and never builds a graph.  Every function binds to the project's sole
*ready* generation; when no such generation exists (or the port raises) the
call degrades to an explicit ``unavailable``/empty result rather than raising a
cryptic error.  This mirrors the ranking layer's binding gate: query results are
always traced against generation state.

Read-only discipline
--------------------
- No builder path is reachable from this module (no ``plan_graph_build`` /
  ``apply_graph_build`` imports).
- The SQLite adapter opening used by CLI/MCP keeps ``read_only=True`` so the
  store stays in immutable ``mode=ro&immutable=1``; nothing here calls a
  writable store method.
- Dependency traversals use ``GraphReadPort.neighbors`` which already excludes
  ``contains`` (structural edge) via the ``DEPENDENCY_RELATIONS`` allow-list.

Functions
---------
- ``graph_find``      exact + lexical symbol/file resolution with confidence.
- ``graph_outline``   hierarchical module -> class -> function/method outline.
- ``graph_impact``    blast radius over dependency edges (direct + transitive).
- ``graph_map``       per-file repository map (hub/coupled/entry/test).
- ``graph_freshness`` generation binding state (ready/stale/unavailable).
"""

from __future__ import annotations

from zekam.application.code_graph_ranking import GraphReadPort
from zekam.domain.code_graph import GraphEdge, GraphFile, GraphGeneration, GraphSymbol

#: Bounded safety cap so a pathological graph cannot run away in one call.
_MAX_IMPACT_DEPTH = 100
#: Default result cap for ``graph_find``.
_DEFAULT_FIND_LIMIT = 50


def _generation_or_none(
    graph: GraphReadPort, project_id: str
) -> GraphGeneration | None:
    try:
        return graph.current_generation(project_id)
    except Exception:
        return None


def _symbol_result(symbol: GraphSymbol, confidence: str) -> dict[str, object]:
    return {
        "match_type": "symbol",
        "confidence": confidence,
        "name": symbol.qualified_name,
        "identity": symbol.symbol_id,
        "kind": str(symbol.kind),
        "relative_path": symbol.file_relative_path,
        "start_line": symbol.start_line,
        "end_line": symbol.end_line,
    }


def _file_result(file: GraphFile, confidence: str) -> dict[str, object]:
    return {
        "match_type": "file",
        "confidence": confidence,
        "name": file.relative_path,
        "identity": file.relative_path,
        "kind": "file",
        "relative_path": file.relative_path,
        "parse_state": file.parse_state,
    }


def graph_find(
    graph: GraphReadPort,
    project_id: str,
    query: str,
    *,
    limit: int = _DEFAULT_FIND_LIMIT,
) -> list[dict[str, object]]:
    """Resolve a symbol or file by exact then lexical match.

    Returns entries with confidence (``exact`` first, then ``fuzzy``), the
    resolved identity and human locator.  ``limit`` bounds the result length.
    """
    if _generation_or_none(graph, project_id) is None:
        return []
    needle = (query or "").strip().lower()
    if not needle:
        return []
    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for symbol in graph.symbols():
        lowered = symbol.qualified_name.lower()
        if lowered == needle:
            results.append(_symbol_result(symbol, "exact"))
        elif needle in lowered:
            results.append(_symbol_result(symbol, "fuzzy"))
    for file in graph.files():
        lowered = file.relative_path.lower()
        if lowered == needle:
            results.append(_file_result(file, "exact"))
        elif needle in lowered:
            results.append(_file_result(file, "fuzzy"))
    ordered = sorted(
        results, key=lambda entry: (0 if str(entry["confidence"]) == "exact" else 1)
    )
    bounded: list[dict[str, object]] = []
    for entry in ordered:
        identity = str(entry["identity"])
        if identity in seen:
            continue
        seen.add(identity)
        bounded.append(entry)
    return bounded[: max(1, limit)]


def _outline_flatten(
    by_id: dict[str, GraphSymbol],
    parent_id: str | None,
    *,
    level: int,
    ancestors: list[str],
    out: list[dict[str, object]],
) -> None:
    children = sorted(
        (symbol for symbol in by_id.values() if symbol.parent_symbol_id == parent_id),
        key=lambda symbol: (symbol.start_line, symbol.qualified_name),
    )
    for symbol in children:
        if symbol.symbol_id in ancestors:
            # Structural cycle guard (should not happen, but stay defensive).
            continue
        out.append(
            {
                "name": symbol.qualified_name,
                "kind": str(symbol.kind),
                "identity": symbol.symbol_id,
                "parent": (
                    by_id[symbol.parent_symbol_id].qualified_name
                    if symbol.parent_symbol_id in by_id
                    else None
                ),
                "level": level,
                "start_line": symbol.start_line,
                "end_line": symbol.end_line,
                "locator": f"{symbol.file_relative_path}:{symbol.start_line}-{symbol.end_line}",
            }
        )
        _outline_flatten(
            by_id,
            symbol.symbol_id,
            level=level + 1,
            ancestors=[*ancestors, symbol.symbol_id],
            out=out,
        )


def graph_outline(
    graph: GraphReadPort, project_id: str, relative_path: str
) -> list[dict[str, object]]:
    """Hierarchical outline (module -> class -> function/method) of one file."""
    if _generation_or_none(graph, project_id) is None:
        return []
    file_symbols = [
        symbol
        for symbol in graph.symbols()
        if symbol.file_relative_path == relative_path
    ]
    if not file_symbols:
        return []
    by_id = {symbol.symbol_id: symbol for symbol in file_symbols}
    out: list[dict[str, object]] = []
    _outline_flatten(by_id, None, level=0, ancestors=[], out=out)
    # Safety: if the extractor produced a file with symbols but none root-anchored,
    # expose them at the top level so the outline is never silently empty.
    if not out:
        for symbol in file_symbols:
            out.append(
                {
                    "name": symbol.qualified_name,
                    "kind": str(symbol.kind),
                    "identity": symbol.symbol_id,
                    "parent": None,
                    "level": 0,
                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,
                    "locator": (
                        f"{symbol.file_relative_path}:{symbol.start_line}-{symbol.end_line}"
                    ),
                }
            )
    return out


def _edge_hit(
    edge: GraphEdge,
    *,
    symbol_id: str,
    direction: str,
    depth: int,
    names: dict[str, GraphSymbol],
) -> dict[str, object]:
    if edge.source_symbol_id == symbol_id:
        other_id = edge.target_symbol_id
    else:
        other_id = edge.source_symbol_id
    other = names.get(other_id) if other_id else None
    return {
        "direction": direction,
        "relation": str(edge.relation),
        "depth": depth,
        "other_symbol_id": other_id,
        "other_name": other.qualified_name if other else edge.target_qualified_name,
        "other_kind": str(other.kind) if other else None,
        "other_file": other.file_relative_path if other else None,
        "other_start_line": other.start_line if other else None,
        "confidence": str(edge.confidence),
    }


def _impact_bfs(
    graph: GraphReadPort,
    start_symbol_id: str,
    *,
    direction: str,
    names: dict[str, GraphSymbol],
    max_depth: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]], bool]:
    """Bounded dependency BFS for one direction.

    Returns (direct_hits, transitive_hits, cycle_detected).  ``direction`` is
    ``outgoing`` (follow source -> target) or ``incoming`` (follow target ->
    source).  Unresolved edges (target_symbol_id is None) cannot be traversed
    transitively; they are only reported as direct hits when adjacent.
    """
    direct: list[dict[str, object]] = []
    transitive: list[dict[str, object]] = []
    visited: set[str] = {start_symbol_id}
    frontier = [start_symbol_id]
    depth = 0
    cycle = False
    while frontier and depth < max_depth:
        depth += 1
        next_frontier: list[str] = []
        for node_id in frontier:
            for edge in graph.neighbors(node_id):
                if direction == "outgoing":
                    if edge.source_symbol_id != node_id:
                        continue
                    other_id = edge.target_symbol_id
                else:
                    if edge.target_symbol_id != node_id:
                        continue
                    other_id = edge.source_symbol_id
                # Adjacent unresolved edge -> report once as a direct hit.
                if other_id is None:
                    direct.append(_edge_hit(edge, symbol_id=node_id, direction=direction,
                                            depth=1, names=names))
                    continue
                if other_id in visited:
                    cycle = True
                    continue
                visited.add(other_id)
                next_frontier.append(other_id)
                if depth == 1:
                    direct.append(_edge_hit(edge, symbol_id=node_id, direction=direction,
                                            depth=1, names=names))
                else:
                    transitive.append(_edge_hit(edge, symbol_id=node_id, direction=direction,
                                                depth=depth, names=names))
        frontier = next_frontier
    return direct, transitive, cycle


def graph_impact(
    graph: GraphReadPort,
    project_id: str,
    symbol_qualified: str,
    *,
    max_depth: int = _MAX_IMPACT_DEPTH,
) -> dict[str, object]:
    """Blast radius of a symbol over dependency edges (``contains`` excluded).

    Computes direct (depth 1) and transitive (depth > 1) incoming and outgoing
    reachability, keeps node dedupe (visited set), and reports whether a cycle
    was observed.  Unknown symbols return ``found: False`` without erroring.
    """
    if _generation_or_none(graph, project_id) is None:
        return {"found": False, "symbol": symbol_qualified}
    names = {symbol.symbol_id: symbol for symbol in graph.symbols()}
    target = next(
        (symbol for symbol in names.values() if symbol.qualified_name == symbol_qualified),
        None,
    )
    if target is None:
        return {
            "found": False,
            "symbol": symbol_qualified,
            "candidates": sorted(
                {
                    symbol.qualified_name
                    for symbol in names.values()
                    if symbol_qualified in symbol.qualified_name
                }
            )[:10],
        }
    out_direct, out_transitive, out_cycle = _impact_bfs(
        graph, target.symbol_id, direction="outgoing", names=names, max_depth=max(1, max_depth)
    )
    in_direct, in_transitive, in_cycle = _impact_bfs(
        graph, target.symbol_id, direction="incoming", names=names, max_depth=max(1, max_depth)
    )
    return {
        "found": True,
        "symbol": symbol_qualified,
        "symbol_id": target.symbol_id,
        "relative_path": target.file_relative_path,
        "start_line": target.start_line,
        "end_line": target.end_line,
        "direct_incoming": in_direct,
        "direct_outgoing": out_direct,
        "transitive_incoming": in_transitive,
        "transitive_outgoing": out_transitive,
        "cycle_detected": bool(out_cycle or in_cycle),
        "max_depth": max(1, max_depth),
    }


def _scope_for(path: str) -> str:
    parts = path.split("/")
    return parts[0] if len(parts) > 1 else "."


def _is_test_file(path: str) -> bool:
    lowered = path.lower()
    parts = lowered.split("/")
    return any(part == "test" for part in parts) or any(
        part.startswith("test_") or part.endswith("_test.py") for part in parts
    )


def graph_map(graph: GraphReadPort, project_id: str) -> list[dict[str, object]]:
    """Repository map: per-file symbol + incoming/outgoing edge aggregation."""
    if _generation_or_none(graph, project_id) is None:
        return []
    symbols = graph.symbols()
    files = graph.files()
    by_id = {symbol.symbol_id: symbol for symbol in symbols}
    file_symbols: dict[str, list[GraphSymbol]] = {}
    for symbol in symbols:
        file_symbols.setdefault(symbol.file_relative_path, []).append(symbol)

    file_edges: dict[str, dict[str, int]] = {}
    seen_edges: set[tuple[str, str]] = set()
    for symbol in symbols:
        for edge in graph.neighbors(symbol.symbol_id):
            if edge.source_symbol_id == symbol.symbol_id:
                other = edge.target_symbol_id
            else:
                other = edge.source_symbol_id
            if other is None:
                continue
            other_symbol = by_id.get(other)
            if other_symbol is None:
                continue
            other_file = other_symbol.file_relative_path
            if other_file == symbol.file_relative_path:
                continue
            a_path = symbol.file_relative_path
            key: tuple[str, str] = (
                (a_path, other_file) if a_path <= other_file else (other_file, a_path)
            )
            if key in seen_edges:
                continue
            seen_edges.add(key)
            bucket = file_edges.setdefault(symbol.file_relative_path, {"in": 0, "out": 0})
            bucket["out"] += 1
            target_bucket = file_edges.setdefault(other_file, {"in": 0, "out": 0})
            target_bucket["in"] += 1

    rows: list[dict[str, object]] = []
    for file in sorted(files, key=lambda item: item.relative_path):
        path = file.relative_path
        edges = file_edges.get(path, {"in": 0, "out": 0})
        in_count = edges["in"]
        out_count = edges["out"]
        total = in_count + out_count
        rows.append(
            {
                "relative_path": path,
                "scope": _scope_for(path),
                "symbol_count": len(file_symbols.get(path, [])),
                "incoming_edges": in_count,
                "outgoing_edges": out_count,
                "total_edges": total,
                "hub": total >= 5,
                "highly_coupled": in_count >= 2 and out_count >= 2,
                "entry_point": in_count == 0 and out_count > 0,
                "category": "test" if _is_test_file(path) else "source",
                "parse_state": file.parse_state,
            }
        )
    return rows


def graph_freshness(
    graph: GraphReadPort,
    project_id: str,
    source_revision: str,
    tree_digest: str,
) -> dict[str, object]:
    """Current generation binding state for ``(project_id, revision, tree)``."""
    generation = _generation_or_none(graph, project_id)
    if generation is None:
        return {
            "project_id": project_id,
            "source_revision": source_revision,
            "tree_digest": tree_digest,
            "state": "unavailable",
            "binding_match": False,
            "current_generation_state": None,
            "generation_digest": None,
        }
    binding_match = (
        generation.project_id == project_id
        and generation.source_revision == source_revision
        and generation.tree_digest == tree_digest
    )
    if generation.state == "ready" and binding_match:
        state = "ready"
    elif generation.state == "ready":
        state = "stale"
    else:
        state = generation.state
    return {
        "project_id": project_id,
        "source_revision": source_revision,
        "tree_digest": tree_digest,
        "state": state,
        "binding_match": binding_match,
        "current_generation_state": generation.state,
        "current_project_id": generation.project_id,
        "current_source_revision": generation.source_revision,
        "current_tree_digest": generation.tree_digest,
        "generation_digest": generation.generation_digest,
    }
