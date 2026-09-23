"""Twenty-case Context Graph golden registry (WP-08, chunk-id based).

Scope
-----
A bounded, deterministic golden corpus for the context-graph retrieval
benchmark.  Each case records a natural-language or exact-identifier query plus
the **project-relative source file paths** that a correct retrieval must return.
Unlike the WP-07 corpus (which keyed on a single ``expected_source`` document),
WP-08 is **chunk-id based**: the benchmark runner resolves every expected source
path to the deterministic chunk ids of the real Zekam knowledge index before it
feeds ``retrieval_service.evaluate`` (see the runner in
``tests/unit/test_wp08_context_graph_benchmark.py``).

Rehearsal discipline
--------------------
This module is repo-local and has almost no runtime coupling.  It never calls a
provider, never builds a knowledge index and never resolves chunk ids itself;
path -> chunk-id resolution happens inside the runner so the same registry can
also drive the (separately authorised) dense-open full run.

Categories (matching task #17):
- ``exact-identifier``  (5): exact code symbols / graph concepts.
- ``semantic``          (10): natural-language questions answered by real files.
- ``graph-connectivity``(5) : dependency/promotion questions that only the graph
  layer can answer.
"""

from __future__ import annotations

from dataclasses import dataclass

EXACT_IDENTIFIER = "exact-identifier"
SEMANTIC = "semantic"
GRAPH_CONNECTIVITY = "graph-connectivity"

#: Real Zekam sources the corpus targets.  Paths are project-relative so the
#: registry is portable (no absolute path is ever persisted).
RANKING = "src/zekam/application/code_graph_ranking.py"
QUERY = "src/zekam/application/code_graph_query.py"
DOMAIN = "src/zekam/domain/code_graph.py"
SQLITE = "src/zekam/infrastructure/sqlite/code_graph.py"
RAG = "src/zekam/application/embedded_project_rag.py"
RETRIEVAL = "src/zekam/application/retrieval_service.py"


@dataclass(frozen=True, slots=True)
class ContextGraphGoldenCase:
    """A single golden case: a query and the set of files that answer it."""

    case_id: str
    category: str
    query: str
    expected_source: tuple[str, ...]


def _cases(
    category: str,
    values: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[ContextGraphGoldenCase, ...]:
    return tuple(
        ContextGraphGoldenCase(
            case_id=f"{category}-{index:02d}",
            category=category,
            query=query,
            expected_source=expected_source,
        )
        for index, (query, expected_source) in enumerate(values, start=1)
    )


EXACT_CASES = _cases(
    EXACT_IDENTIFIER,
    (
        ("ProjectGraphReranker", (RANKING,)),
        ("approximate_pagerank", (RANKING,)),
        ("current_generation", (RANKING, SQLITE)),
        ("graph_chunk_link", (SQLITE,)),
        ("symbols_for_file", (RANKING, SQLITE)),
    ),
)

SEMANTIC_CASES = _cases(
    SEMANTIC,
    (
        (
            "RRF sonucunu graph seed'ine map eden sinif hangisi?",
            (RANKING,),
        ),
        (
            "contains neden ranking'te dependency edge'i olarak kullanilmaz?",
            (DOMAIN, RANKING),
        ),
        (
            "Reranker fused hit kumesinin kimligini hangi mekanizmayla korur?",
            (RANKING,),
        ),
        (
            "Bir dosyanin module->class->fonksiyon outline'i hangi islevle uretilir?",
            (QUERY,),
        ),
        (
            "Bir sembolun incoming/outgoing dependency patlamasini hangi islev hesaplar?",
            (QUERY,),
        ),
        (
            "Graph generasyonunun ready/stale durumu nasil raporlanir?",
            (QUERY,),
        ),
        (
            "Chunk ve sembol arasindaki bag koprusunu hangi katman olusturur?",
            (SQLITE,),
        ),
        (
            "Exact, lexical ve dense kanallarini hangi sinif birlestirir?",
            (RETRIEVAL,),
        ),
        (
            "Ayni dosyanin sibling seed'i neden sinirli agirlik alir?",
            (RANKING,),
        ),
        (
            "Hub ve highly-coupled dosya haritasini hangi islev uretir?",
            (QUERY,),
        ),
    ),
)

GRAPH_CASES = _cases(
    GRAPH_CONNECTIVITY,
    (
        (
            "Seed'den komurluk graph'i boyunca yayilan skor nasil hesaplanir?",
            (RANKING,),
        ),
        (
            "Bagli bir dosya, izole ama lexical olarak guclu dosyadan neden one gecer?",
            (RANKING,),
        ),
        (
            "Sembol grafiginde contains disi dependency alan siniri nerede tanimli?",
            (DOMAIN,),
        ),
        (
            "Bir sembolun transitif incoming reachability'si hangi islevde doner?",
            (QUERY,),
        ),
        (
            "Bir edge'in target sembolu cozulemezse graph BFS'i nasil davranir?",
            (QUERY,),
        ),
    ),
)

GOLDEN_CASES = EXACT_CASES + SEMANTIC_CASES + GRAPH_CASES

if len(GOLDEN_CASES) != 20 or len({case.case_id for case in GOLDEN_CASES}) != 20:
    raise RuntimeError("WP-08 Context Graph golden registry exact 20 unique case olmali")
