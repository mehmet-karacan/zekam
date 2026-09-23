"""Graph-aware reranker for post-fusion RRF ranking (G2).

Scope
-----
A **reranker**, not a fourth retrieval channel.  It consumes the fused
``FusedHit`` tuple already produced by ``ReciprocalRankFusion`` and returns a
re-ordered tuple with the **same chunk-id set**.  ``RetrievalService._rerank``
independently re-checks set equality and falls back to the fused order if the
reranker ever drops a hit.

Design constraints (task #9, report s3.2)
-----------------------------------------
- ``RetrievalChannel`` stays untouched (EXACT/LEXICAL/DENSE); graph ranking is
  applied *after* fusion so channel semantics and the default RAG path are
  preserved.
- The reranker is **default-off** (task #17): it only runs when explicitly
  constructed with an enabler flag and a matching graph binding.  With the flag
  off, ``EmbeddedProjectRAG.reranker`` stays ``None`` and the baseline RAG
  behaviour is byte-for-byte preserved.
- ``contains`` is excluded from the dependency edge set (task #9).  We rely on
  the domain ``DEPENDENCY_RELATIONS`` allow-list which already drops it.

Binding gate (task s9)
----------------------
The graph generation must match the knowledge generation identity
(``project_id``, ``source_revision``, ``tree_digest``) and be ``ready``.
On any mismatch or degenerate graph the reranker is disabled for that call and
the fused result is returned unchanged, recording ``graph_state`` + a bypass
reason for trace.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from zekam.domain.code_graph import (
    ALL_RELATIONS,
    DEPENDENCY_RELATIONS,
    GraphEdge,
    GraphFile,
    GraphGeneration,
    GraphRelation,
    GraphSymbol,
)
from zekam.domain.retrieval import FusedHit


class GraphReadPort(Protocol):
    """Read-only view over a code graph generation.

    A caller-supplied adapter binds this port to a concrete store (e.g. the
    SQLite graph store) without letting the ranking core couple to SQLite.
    ``current_generation`` raises ``ValidationFailed`` when no ready generation
    exists for the project.
    """

    def current_generation(self, project_id: str) -> GraphGeneration: ...

    def neighbors(self, symbol_id: str) -> tuple[GraphEdge, ...]: ...

    def file_for_symbol(self, symbol_id: str) -> str: ...

    def symbols_for_file(self, relative_path: str) -> tuple[str, ...]: ...

    def symbols(self) -> tuple[GraphSymbol, ...]:
        """All symbols of the current generation (read-only, for queries/map)."""

    def files(self) -> tuple[GraphFile, ...]:
        """All files of the current generation (read-only, for find/map/outline)."""


@dataclass(frozen=True, slots=True)
class GraphRankingConfig:
    """Tunable, frozen configuration.  None of these are "recommended truths",
    they are explicit knobs an operator may change."""

    alpha: float = 0.25
    max_iterations: int = 25
    max_file_leaders: int = 50
    sibling_rounds: int = 2
    max_seed_hits: int = 40
    dependency_relations: frozenset[GraphRelation] = DEPENDENCY_RELATIONS

    def __post_init__(self) -> None:
        if not 0.0 < self.alpha < 1.0:
            raise ValueError("alpha (0,1) araliginda olmali")
        if self.max_iterations < 1:
            raise ValueError("max_iterations pozitif olmali")
        if self.max_file_leaders < 1:
            raise ValueError("max_file_leaders pozitif olmali")
        if self.sibling_rounds < 1:
            raise ValueError("sibling_rounds pozitif olmali")
        if self.max_seed_hits < 1:
            raise ValueError("max_seed_hits pozitif olmali")
        if not self.dependency_relations:
            raise ValueError("dependency_relations bos olamaz")
        if not self.dependency_relations <= ALL_RELATIONS:
            raise ValueError("dependency_relations bilinmeyen relation iceriyor")
        if GraphRelation.CONTAINS in self.dependency_relations:
            raise ValueError("contains dependency edge setine girmez")


def approximate_pagerank(
    seeds: Mapping[str, float],
    adjacency: Mapping[str, frozenset[str]],
    *,
    alpha: float,
    iterations: int,
) -> dict[str, float]:
    """Deterministic, bounded personalized PageRank.

    Each node carries its normalized seed preference; a fixed ``alpha`` teleport
    plus ``(1 - alpha)`` neighbour-averaged inflow converges over a bounded
    number of iterations.  ``adjacency`` maps a node to its successors.  Nodes
    are iterated in sorted order so results are reproducible across runs.

    The caller is responsible for feeding only dependency relations into
    ``adjacency``; ``contains`` never appears here.
    """
    nodes = sorted(set(seeds) | set(adjacency) | {n for vs in adjacency.values() for n in vs})
    total = sum(seeds.values())
    if total <= 0.0:
        return {}
    preference = {node: seeds.get(node, 0.0) / total for node in nodes}
    successors = {node: adjacency.get(node, frozenset()) for node in nodes}
    rank = {node: preference[node] for node in nodes}
    for _ in range(iterations):
        updated: dict[str, float] = {}
        for node in nodes:
            base = (1.0 - alpha) * preference[node]
            inflow = 0.0
            for predecessor in nodes:
                out = successors[predecessor]
                if node in out:
                    inflow += rank[predecessor] / max(1, len(out))
            updated[node] = base + alpha * inflow
        rank = updated
    return rank


@dataclass(frozen=True, slots=True)
class GraphSeedMap:
    """Output of :class:`GraphSeedMapper`.

    ``weights`` maps a graph symbol id to its seed weight; ``file_symbols`` maps
    a project-relative file path to the symbol ids of that file that were
    seeded.  Both are used to compute per-file ranking scores.
    """

    weights: dict[str, float]
    file_symbols: dict[str, tuple[str, ...]]


class GraphSeedMapper:
    """Map fused RRF hits onto graph symbol nodes.

    Seed weighting rules:
    - exact matches outweight lexical/dense hits;
    - the top-ranked hit carries more weight than lower-ranked ones;
    - siblings inside the same file cannot accumulate unbounded weight (a
      per-file sibling decay is applied and the file mass is spread among its
      symbols).
    """

    def __init__(self, graph: GraphReadPort, *, config: GraphRankingConfig) -> None:
        self._graph = graph
        self._config = config

    def map_hits(
        self, fused: tuple[FusedHit, ...], chunk_file: Callable[[str], str | None]
    ) -> GraphSeedMap:
        weights: dict[str, float] = {}
        file_symbols: dict[str, list[str]] = {}
        file_seen: dict[str, int] = {}
        for index, hit in enumerate(fused):
            if index >= self._config.max_seed_hits:
                break
            path = chunk_file(hit.chunk_id)
            if not path:
                continue
            symbols = self._graph.symbols_for_file(path)
            if not symbols:
                continue
            base = 1.0 / (1.0 + index)
            if hit.exact_match:
                base *= 2.0
            sibling_index = file_seen.get(path, 0)
            file_seen[path] = sibling_index + 1
            # Per-file sibling decay prevents one dominating file from
            # accumulating unbounded seed mass across repeated sibling hits.
            weight = base / (1.0 + sibling_index)
            known = file_symbols.setdefault(path, [])
            for symbol in symbols:
                weights[symbol] = weights.get(symbol, 0.0) + weight
                if symbol not in known:
                    known.append(symbol)
        return GraphSeedMap(
            weights=weights,
            file_symbols={path: tuple(symbols) for path, symbols in file_symbols.items()},
        )


@dataclass(frozen=True, slots=True)
class GraphRankState:
    """Trace metadata describing the last reranker invocation.

    ``graph_state`` reports the graph generation state observed; ``bypass`` gives
    the reason the graph was not applied.  When the graph was applied, ``used``
    is true and ``seed_count``/``promoted`` describe the outcome.
    """

    graph_state: str
    bypass: str | None = None
    used: bool = False
    seed_count: int = 0
    promoted: int = 0


class ProjectGraphReranker:
    """Reranker that honours the ``RetrievalService.Reranker`` callable shape.

    ``__call__(query, fused) -> tuple[FusedHit, ...]``.  The graph port, binding
    identity and chunk->file resolver are captured at construction; the enabler
    flag lives in the composition layer.  On any binding or degeneracy problem
    the fused order is returned unchanged and ``last_state`` reports the reason.
    """

    def __init__(
        self,
        graph: GraphReadPort,
        *,
        project_id: str,
        source_revision: str,
        tree_digest: str,
        chunk_file: Callable[[str], str | None],
        config: GraphRankingConfig | None = None,
    ) -> None:
        self._graph = graph
        self._project_id = project_id
        self._source_revision = source_revision
        self._tree_digest = tree_digest
        self._chunk_file = chunk_file
        self._config = config or GraphRankingConfig()
        self.last_state = GraphRankState(graph_state="idle")

    def _check_binding(self) -> GraphGeneration | None:
        try:
            generation = self._graph.current_generation(self._project_id)
        except Exception as exc:
            self.last_state = GraphRankState(
                graph_state="unavailable", bypass=f"generation-error:{type(exc).__name__}"
            )
            return None
        if generation.state != "ready":
            self.last_state = GraphRankState(graph_state=generation.state, bypass="graph-not-ready")
            return None
        if (
            generation.project_id != self._project_id
            or generation.source_revision != self._source_revision
            or generation.tree_digest != self._tree_digest
        ):
            self.last_state = GraphRankState(
                graph_state=generation.state, bypass="binding-mismatch"
            )
            return None
        return generation

    def _collect_adjacency(self, seed_symbols: set[str]) -> dict[str, frozenset[str]]:
        adjacency: dict[str, set[str]] = {}
        for symbol in seed_symbols:
            for edge in self._graph.neighbors(symbol):
                if edge.relation not in self._config.dependency_relations:
                    continue
                if edge.source_symbol_id == symbol:
                    other = edge.target_symbol_id
                elif edge.target_symbol_id == symbol and edge.source_symbol_id != symbol:
                    other = edge.source_symbol_id
                else:
                    continue
                if other is None:
                    continue
                adjacency.setdefault(symbol, set()).add(other)
                adjacency.setdefault(other, set()).add(symbol)
        return {node: frozenset(edges) for node, edges in adjacency.items()}

    def _promoted_count(self, before: tuple[FusedHit, ...], after: tuple[FusedHit, ...]) -> int:
        rank_before = {hit.chunk_id: index for index, hit in enumerate(before)}
        return sum(1 for index, hit in enumerate(after) if rank_before[hit.chunk_id] > index)

    def _reorder(
        self,
        fused: tuple[FusedHit, ...],
        file_assign: Mapping[str, str | None],
        file_scores: Mapping[str, float],
    ) -> tuple[FusedHit, ...]:
        exact = [hit for hit in fused if hit.exact_match]
        others = [hit for hit in fused if not hit.exact_match]
        if not others:
            return tuple(exact)

        def bucket(hit: FusedHit) -> str:
            path = file_assign.get(hit.chunk_id)
            return path if path else ""

        # Distinct files in order of first appearance.
        distinct: list[str] = []
        seen: set[str] = set()
        for hit in others:
            path = bucket(hit)
            if path not in seen:
                seen.add(path)
                distinct.append(path)

        # Preserve the original top hit: its file leads, remaining distinct
        # files are ordered by aggregate PPR score.
        top_file = bucket(others[0])
        rest = [path for path in distinct if path != top_file]
        rest_sorted = sorted(rest, key=lambda path: (-file_scores.get(path, 0.0), path))
        ordered_files = [top_file, *rest_sorted]
        ordered_files = ordered_files[: self._config.max_file_leaders]

        emitted: set[str] = set()
        result: list[FusedHit] = list(exact)

        # Leaders: the first hit of each distinct file.
        for path in ordered_files:
            for hit in others:
                if bucket(hit) == path and hit.chunk_id not in emitted:
                    result.append(hit)
                    emitted.add(hit.chunk_id)
                    break

        # Sibling rounds: one sibling per file per round, file order preserved.
        sibling_lists: dict[str, list[FusedHit]] = {}
        for hit in others:
            if hit.chunk_id in emitted:
                continue
            sibling_lists.setdefault(bucket(hit), []).append(hit)
        for round_index in range(self._config.sibling_rounds):
            for path in ordered_files:
                sibling = sibling_lists.get(path)
                if sibling and round_index < len(sibling):
                    candidate = sibling[round_index]
                    if candidate.chunk_id not in emitted:
                        result.append(candidate)
                        emitted.add(candidate.chunk_id)

        # Any remaining siblings, file order preserved.
        for path in ordered_files:
            for hit in sibling_lists.get(path, []):
                if hit.chunk_id not in emitted:
                    result.append(hit)
                    emitted.add(hit.chunk_id)

        return tuple(result)

    def __call__(self, query: str, fused: tuple[FusedHit, ...]) -> tuple[FusedHit, ...]:
        generation = self._check_binding()
        if generation is None:
            return tuple(fused)

        seed_map = GraphSeedMapper(self._graph, config=self._config).map_hits(
            fused, self._chunk_file
        )
        if not seed_map.weights:
            # Every hit mapped to a file without graph symbols: nothing to promote.
            self.last_state = GraphRankState(
                graph_state="ready", bypass="no-seed-symbols", used=True
            )
            return tuple(fused)

        adjacency = self._collect_adjacency(set(seed_map.weights))
        scores = approximate_pagerank(
            seed_map.weights,
            adjacency,
            alpha=self._config.alpha,
            iterations=self._config.max_iterations,
        )
        file_scores: dict[str, float] = {}
        for path, symbols in seed_map.file_symbols.items():
            file_scores[path] = sum(scores.get(symbol, 0.0) for symbol in symbols)

        file_assign = {hit.chunk_id: self._chunk_file(hit.chunk_id) for hit in fused}
        reordered = self._reorder(fused, file_assign, file_scores)
        self.last_state = GraphRankState(
            graph_state="ready",
            used=True,
            seed_count=len(seed_map.weights),
            promoted=self._promoted_count(fused, reordered),
        )
        return reordered
