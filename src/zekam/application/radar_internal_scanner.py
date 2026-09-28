"""Read-only internal gap scanner over the Zekam code graph.

WP-05: RadarInternalScanner inspects the current local code graph generation
and produces authority-free gap cards.  It performs no network calls, no
provider calls, and no mutations.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import TYPE_CHECKING

from zekam.domain.canonical import digest
from zekam.domain.code_graph import (
    DEPENDENCY_RELATIONS,
    GraphEdge,
    GraphFile,
    GraphRelation,
    GraphSymbol,
)
from zekam.domain.errors import ValidationFailed
from zekam.domain.radar_candidate import RadarCandidateDecision, RadarCandidateKind, RadarGapCard

if TYPE_CHECKING:
    from zekam.application.code_graph_ranking import GraphReadPort

# Bounded safety caps for one scan invocation.
_MAX_SYMBOLS = 1_000
_MAX_FILES = 100
_MAX_EDGES = 10_000

# A practical denylist for the Python standard library and common third-party
# top-level packages.  The scanner only flags IMPORT edges whose unresolved
# target is *not* in this set, i.e. likely an internal import that should have
# resolved in the same graph generation.
_EXTERNAL_MODULE_DENYLIST: frozenset[str] = frozenset(
    {
        # stdlib modules commonly observed in Zekam
        "abc",
        "argparse",
        "ast",
        "asyncio",
        "base64",
        "binascii",
        "builtins",
        "collections",
        "concurrent",
        "contextlib",
        "copy",
        "csv",
        "dataclasses",
        "datetime",
        "decimal",
        "doctest",
        "email",
        "enum",
        "fnmatch",
        "ftplib",
        "functools",
        "gzip",
        "hashlib",
        "html",
        "http",
        "imaplib",
        "importlib",
        "inspect",
        "io",
        "itertools",
        "json",
        "logging",
        "math",
        "multiprocessing",
        "numbers",
        "nntplib",
        "os",
        "pathlib",
        "pickle",
        "pydoc",
        "poplib",
        "random",
        "re",
        "shutil",
        "signal",
        "smtplib",
        "sqlite3",
        "statistics",
        "string",
        "subprocess",
        "sys",
        "tarfile",
        "telnetlib",
        "tempfile",
        "textwrap",
        "threading",
        "time",
        "traceback",
        "types",
        "typing",
        "typing_extensions",
        "unittest",
        "urllib",
        "uuid",
        "warnings",
        "xml",
        "zipfile",
        # common third-party packages present in the environment
        "pytest",
        "typer",
        "pydantic",
        "requests",
        "httpx",
        "jinja2",
        "yaml",
        "numpy",
        "tiktoken",
    }
)


def _is_test_file(relative_path: str) -> bool:
    lowered = relative_path.lower()
    parts = lowered.replace("\\", "/").split("/")
    return (
        "tests" in parts
        or any(part.startswith("test_") for part in parts)
        or any(part.endswith("_test.py") for part in parts)
    )


def _is_entry_point_file(relative_path: str) -> bool:
    return relative_path.replace("\\", "/").endswith("/__main__.py")


def _is_private_api(symbol: GraphSymbol) -> bool:
    """Private API symbol: not a module and its local name starts with '_'."""
    if symbol.kind.value == "module":
        return False
    base = symbol.qualified_name.rsplit(".", 1)[-1]
    return base.startswith("_")


def _top_level_module(target: str) -> str:
    return target.split(".")[0]


class RadarInternalScanner:
    """Scan the current code-graph generation for internal gaps."""

    def __init__(self, graph: GraphReadPort) -> None:
        if graph is None:
            raise ValidationFailed("RadarInternalScanner graph bagimsizligi gerekli")
        self._graph = graph

    def scan_for_gaps(self, project_ref: str) -> list[RadarGapCard]:
        """Return gap cards for the current ready graph generation.

        ``project_ref`` is used as the source binding and campaign identity;
        no mutation is performed on the graph.
        """
        if not project_ref or not isinstance(project_ref, str):
            raise ValidationFailed("project_ref gecerli string olmali")

        try:
            self._graph.current_generation(project_ref)
        except Exception:
            return []

        files = self._graph.files()
        symbols = self._graph.symbols()

        # Apply bounded caps before allocating further work.
        coverage_gap: RadarGapCard | None = None
        if len(files) > _MAX_FILES or len(symbols) > _MAX_SYMBOLS:
            coverage_gap = self._coverage_gap(project_ref, files, symbols, edge_count=None)
            files = files[:_MAX_FILES]
            symbols = symbols[:_MAX_SYMBOLS]

        incoming_edges: Counter[str] = Counter()
        outgoing_edges: Counter[str] = Counter()
        all_edges: list[GraphEdge] = []
        for symbol in symbols:
            for edge in self._graph.neighbors(symbol.symbol_id):
                if edge.relation not in DEPENDENCY_RELATIONS:
                    continue
                all_edges.append(edge)
                if edge.source_symbol_id == symbol.symbol_id:
                    outgoing_edges[symbol.symbol_id] += 1
                if edge.target_symbol_id == symbol.symbol_id:
                    incoming_edges[symbol.symbol_id] += 1

        if len(all_edges) > _MAX_EDGES:
            coverage_gap = self._coverage_gap(
                project_ref, files, symbols, edge_count=len(all_edges)
            )
            all_edges = all_edges[:_MAX_EDGES]

        cards: list[RadarGapCard] = []
        cards.extend(self._duplicate_qualified_name(symbols, project_ref))
        cards.extend(self._parse_error_files(files, project_ref))
        cards.extend(self._import_orphans(all_edges, project_ref))
        cards.extend(self._unused_symbols(symbols, incoming_edges, project_ref))
        if coverage_gap is not None:
            cards.append(coverage_gap)
        return cards

    def _duplicate_qualified_name(
        self, symbols: Iterable[GraphSymbol], project_ref: str
    ) -> list[RadarGapCard]:
        name_to_files: dict[str, set[str]] = {}
        for symbol in symbols:
            name_to_files.setdefault(symbol.qualified_name, set()).add(symbol.file_relative_path)

        cards: list[RadarGapCard] = []
        for name, paths in name_to_files.items():
            if len(paths) <= 1:
                continue
            cards.append(
                RadarGapCard(
                    card_id=f"radar-gap:duplicate:{digest(name + project_ref)[:12]}",
                    campaign_id=project_ref,
                    kind=RadarCandidateKind.GAP,
                    current_baseline="Zekam main",
                    source_binding=project_ref,
                    production_call_path=(
                        f"duplicate_qualified_name: {name} in {','.join(sorted(paths))}"
                    ),
                    test_measurement_evidence="code-graph-only",
                    covered_areas="duplicate_qualified_name",
                    uncovered_areas="local measurement not run",
                    result=RadarCandidateDecision.GAP_DEMONSTRATED,
                )
            )
        return cards

    def _parse_error_files(
        self, files: Iterable[GraphFile], project_ref: str
    ) -> list[RadarGapCard]:
        cards: list[RadarGapCard] = []
        for file in files:
            if file.parse_state != "syntax-error" and file.error_count <= 0:
                continue
            cards.append(
                RadarGapCard(
                    card_id=f"radar-gap:parse:{digest(file.relative_path + project_ref)[:12]}",
                    campaign_id=project_ref,
                    kind=RadarCandidateKind.GAP,
                    current_baseline="Zekam main",
                    source_binding=project_ref,
                    production_call_path=(
                        f"parse_error_file: {file.relative_path} "
                        f"state={file.parse_state} errors={file.error_count}"
                    ),
                    test_measurement_evidence="code-graph-only",
                    covered_areas="parse_error_file",
                    uncovered_areas="local measurement not run",
                    result=RadarCandidateDecision.GAP_DEMONSTRATED,
                )
            )
        return cards

    def _import_orphans(self, edges: Iterable[GraphEdge], project_ref: str) -> list[RadarGapCard]:
        cards: list[RadarGapCard] = []
        seen: set[str] = set()
        for edge in edges:
            if edge.relation is not GraphRelation.IMPORTS:
                continue
            if edge.target_symbol_id is not None:
                continue
            target = edge.target_qualified_name
            top = _top_level_module(target)
            if top in _EXTERNAL_MODULE_DENYLIST:
                continue
            key = f"{edge.source_symbol_id}->{target}"
            if key in seen:
                continue
            seen.add(key)
            cards.append(
                RadarGapCard(
                    card_id=f"radar-gap:orphan:{digest(key + project_ref)[:12]}",
                    campaign_id=project_ref,
                    kind=RadarCandidateKind.GAP,
                    current_baseline="Zekam main",
                    source_binding=project_ref,
                    production_call_path=f"import_orphan: {target} from {edge.source_symbol_id}",
                    test_measurement_evidence="code-graph-only",
                    covered_areas="import_orphan",
                    uncovered_areas="local measurement not run",
                    result=RadarCandidateDecision.GAP_DEMONSTRATED,
                )
            )
        return cards

    def _unused_symbols(
        self,
        symbols: Iterable[GraphSymbol],
        incoming_edges: Counter[str],
        project_ref: str,
    ) -> list[RadarGapCard]:
        cards: list[RadarGapCard] = []
        for symbol in symbols:
            if symbol.kind.value not in {"function", "method"}:
                continue
            if _is_test_file(symbol.file_relative_path):
                continue
            if _is_entry_point_file(symbol.file_relative_path):
                continue
            if not _is_private_api(symbol):
                continue
            if incoming_edges.get(symbol.symbol_id, 0) > 0:
                continue
            cards.append(
                RadarGapCard(
                    card_id=f"radar-gap:unused:{symbol.symbol_id[:12]}",
                    campaign_id=project_ref,
                    kind=RadarCandidateKind.GAP,
                    current_baseline="Zekam main",
                    source_binding=project_ref,
                    production_call_path=(
                        f"unused_symbol: {symbol.qualified_name} "
                        f"in {symbol.file_relative_path}:{symbol.start_line}-{symbol.end_line}"
                    ),
                    test_measurement_evidence="code-graph-only",
                    covered_areas="unused_symbol",
                    uncovered_areas="local measurement not run",
                    result=RadarCandidateDecision.GAP_DEMONSTRATED,
                )
            )
        return cards

    def _coverage_gap(
        self,
        project_ref: str,
        files: Iterable[GraphFile],
        symbols: Iterable[GraphSymbol],
        edge_count: int | None,
    ) -> RadarGapCard:
        file_list = list(files)
        symbol_list = list(symbols)
        file_count = len(file_list)
        symbol_count = len(symbol_list)
        edge_text = f" edges={edge_count}" if edge_count is not None else ""
        return RadarGapCard(
            card_id=f"radar-gap:coverage:{digest(project_ref)[:12]}",
            campaign_id=project_ref,
            kind=RadarCandidateKind.GAP,
            current_baseline="Zekam main",
            source_binding=project_ref,
            production_call_path=(
                f"coverage_limit_exceeded: files={file_count} "
                f"symbols={symbol_count}{edge_text} "
                f"(caps: {_MAX_FILES} files, {_MAX_SYMBOLS} symbols, {_MAX_EDGES} edges)"
            ),
            test_measurement_evidence="code-graph-only",
            covered_areas="coverage_limit",
            uncovered_areas="local measurement not run",
            result=RadarCandidateDecision.PARTIAL,
        )
