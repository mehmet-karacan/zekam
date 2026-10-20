"""Fresh-source embedded project indexing and evidence-gated hybrid retrieval."""

from __future__ import annotations

import datetime as dt
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from zekam.application.code_graph_ranking import (
    GraphRankingConfig,
    GraphReadPort,
    ProjectGraphReranker,
)
from zekam.application.embedding_provider import EmbeddingPolicy, EmbeddingProvider
from zekam.application.knowledge_index import (
    KnowledgeGeneration,
    KnowledgeIndexPort,
    KnowledgeIndexRecord,
)
from zekam.application.project_knowledge_index import (
    MAX_PROJECT_EMBED_BATCH_SIZE,
    ProjectIndexPlan,
    _embed_project_batch_with_retry,
    _project_embedding_batches,
)
from zekam.application.provider_adapter import (
    ProviderCallCancelled,
    ProviderCallQuotaExceeded,
    ProviderDeadlineExceeded,
)
from zekam.application.retrieval_service import (
    DEFAULT_RETRIEVAL_DEADLINE_SECONDS,
    MAX_PACKED_CHUNKS,
    MAX_PACKED_CHUNKS_ENUMERATION,
    QueryIntent,
    Reranker,
    RetrievalService,
    RetrievalTrace,
    _classify_intent,
)
from zekam.application.secret_detection import scan_text
from zekam.application.source_architecture_answer import (
    build_architecture_summary,
    is_architecture_query,
    source_module,
)
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.retrieval import (
    AnswerState,
    FusedHit,
    RetrievalChannel,
    ScoredHit,
    answer_semantics,
    extract_identifiers,
)

_TOKEN = re.compile(r"\w+", re.UNICODE)
MAX_QUERY_BYTES = 16 * 1024
DEFAULT_DENSE_EVIDENCE_THRESHOLD = 0.49
DEFAULT_DENSE_MARGIN_THRESHOLD = 0.04
DEFAULT_LEXICAL_COVERAGE_THRESHOLD = 0.50

#: Kaynak-mimari sorularda sinirli kod envanteri ornekleme limitleri
#: (``KnowledgeIndexPort.architecture_candidates`` varsayilanlariyla ayni).
ARCHITECTURE_CANDIDATE_LIMIT = 96
ARCHITECTURE_MAX_MODULES = 12
ARCHITECTURE_PER_MODULE = 8

#: RAG26: kaynak-mimari niyet acik etiketi; deterministik kod turetimi LLM
#: uretimi olarak etiketlenmez.
ARCHITECTURE_INTENT = "source-architecture"

#: WP6 (B07): an evidence set is only "relationship-satisfying" when at least one
#: of the gathered chunks carries genuine call-site / data-flow / dependency edge
#: language.  Merely finding A and B separately is NOT an edge (no fabrication).
_RELATIONSHIP_EDGE_SIGNALS = (
    "calls",
    "çağırır",
    "cagirir",
    "çağrı",
    "cagri",
    "call",
    "bağımlı",
    "bagimli",
    "bağımlılık",
    "bagimlilik",
    "dependency",
    "depends on",
    "uses",
    "import ",
    "from ",
    "->",
    "-->",
    "::",
    "calls into",
    "invokes",
)

#: WP6 (B08): maximum length (characters) of the answer excerpt that is taken
#: from a single used chunk.  Kept bounded so the output contract stays small
#: and model context/output reserve is respected.  The excerpt is chosen from
#: the *used* chunks (evidence-selected), not a blind first-500 of the very
#: first chunk.
MAX_ANSWER_EXCERPT_CHARS = 1200


_EXCERPT_NOISE_PREFIXES = ("import ", "package ", "#include", "using ", "from ")
_QUESTION_FILLER = frozenset(
    {"ve", "gerekir", "mi", "mu", "mı", "mü", "bir", "bu", "de", "da", "ne", "en"}
)
_EXCERPT_STOPWORDS = frozenset(
    {
        *("hangi", "nedir", "nasil", "neler", "nerede", "ile", "icin", "veya", "ama"),
        *("gibi", "olan", "olarak", "sunar", "calistirir", "tanimli", "var", "the"),
        *("and", "for", "what", "which", "how", "does"),
    }
)


def _excerpt_terms(query: str) -> frozenset[str]:
    return frozenset(
        item.casefold()
        for item in _TOKEN.findall(query)
        if len(item) > 2 and item.casefold() not in _EXCERPT_STOPWORDS
    )


def _excerpt_line_score(line: str, terms: frozenset[str]) -> int:
    """Query relevance of one source line; import/package preamble scores zero."""
    stripped = line.strip()
    if not stripped or stripped.startswith(_EXCERPT_NOISE_PREFIXES):
        return 0
    lowered = stripped.casefold()
    return sum(1 for term in terms if term in lowered)


#: Turkce atama kaliplari (`anahtar = "..."`, `sifre: '...'`) genel tarayicida yok; yalniz
#: CIKTI tarafinda, savunma katmani olarak maskelenir (indeks kurallari degismez).
_TURKISH_CREDENTIAL_LINE = re.compile(
    r"(?:anahtar|sifre|şifre|parola)\w*\s*[:=]\s*[\"'][^\"'\s]{6,}[\"']", re.IGNORECASE
)
REDACTED_LINE = "[REDACTED: secret-pattern]"


def _redact_secret_lines(text: str) -> tuple[str, bool]:
    """Gizli deger kalibi tasiyan satirlari maskeler; deger asla ciktiya gecmez."""

    flagged = {item.line_number for item in scan_text(text, relative_path="answer-excerpt.txt")}
    lines = text.splitlines(keepends=True)
    for number, line in enumerate(lines, start=1):
        if _TURKISH_CREDENTIAL_LINE.search(line):
            flagged.add(number)
    if not flagged:
        return text, False
    masked = [
        REDACTED_LINE + ("\n" if line.endswith("\n") else "") if number in flagged else line
        for number, line in enumerate(lines, start=1)
    ]
    return "".join(masked), True


#: Pencere, sinif/metod tanimindan hemen once gelen anotasyon satirlarini (`@RequestMapping`)
#: en fazla bu kadar geriye dogru dahil eder; taban yol/route bilgisi kaybolmaz.
MAX_LEADING_ANNOTATION_LINES = 6


def _excerpt_window(
    text: str, max_chars: int, query: str = ""
) -> tuple[str, bool, tuple[int, int]]:
    """Return a bounded, line-aligned, query-aware window of ``text``.

    RAG26-R03: the window is the contiguous run of *exact source lines* (never
    rewritten) that maximises query-term relevance within ``max_chars``.
    Import/package preambles carry no score, so an endpoint or flow body that
    starts after a long import block is not cut off.  Without any query match the
    deterministic leading window is used.  ``(window, truncated, (first, last))``
    where the 1-based line range lets the consumer bind the excerpt to the same
    bytes the chunk locator refers to.
    """
    if max_chars <= 0:
        return "", text != "", (1, 1)
    lines = text.splitlines(keepends=True)
    if len(text) <= max_chars:
        return text, False, (1, max(len(lines), 1))
    terms = _excerpt_terms(query)
    scores = [_excerpt_line_score(line, terms) for line in lines]
    best = (0, 0, 0)  # (score, start, end_exclusive)
    if any(scores):
        for start in range(len(lines)):
            if not scores[start]:
                continue
            total = 0
            score = 0
            end = start
            while end < len(lines) and total + len(lines[end]) <= max_chars:
                total += len(lines[end])
                score += scores[end]
                end += 1
            if end > start and score > best[0]:
                best = (score, start, end)
    if best[0] == 0:
        start, end, total = 0, 0, 0
        while end < len(lines) and total + len(lines[end]) <= max_chars:
            total += len(lines[end])
            end += 1
        if end == 0:
            return text[:max_chars], True, (1, 1)
        return "".join(lines[:end]), True, (1, end)
    _, start, end = best
    back = 0
    while (
        start > 0
        and back < MAX_LEADING_ANNOTATION_LINES
        and lines[start - 1].lstrip().startswith("@")
    ):
        start -= 1
        back += 1
    if back:
        total = 0
        end = start
        while end < len(lines) and total + len(lines[end]) <= max_chars:
            total += len(lines[end])
            end += 1
    return "".join(lines[start:end]), True, (start + 1, end)


def _excerpt_relevance(text: str, query: str) -> int:
    terms = _excerpt_terms(query)
    return sum(_excerpt_line_score(line, terms) for line in text.splitlines())


def _build_excerpt(
    answer: Any, views: dict[str, Any], query: str = ""
) -> tuple[str | None, dict[str, Any] | None]:
    """Build the answer excerpt window and its additive provenance metadata.

    ``answer_excerpt`` stays a **string** (backward-compatible), taken from the
    used chunk whose query-aware window is most relevant (first used chunk on
    ties).  The digest/locator relationship is carried in the additive
    ``answer_excerpt_meta`` field, now including ``excerpt_line_range`` so the
    excerpt digest is never mixed with the full chunk digest.  The range is
    relative to the chunk text; the file line is ``locator.line_start + a - 1``.

    Returns ``(text_or_None, meta_or_None)``.
    """
    if not answer.used_chunk_ids or not answer.citations:
        return None, None
    candidates = [
        (chunk_id, views[chunk_id]) for chunk_id in answer.used_chunk_ids if chunk_id in views
    ]
    if not candidates:
        return None, None
    chunk_id, view = max(
        enumerate(candidates),
        key=lambda item: (_excerpt_relevance(item[1][1].text, query), -item[0]),
    )[1]
    text, truncated, line_range = _excerpt_window(view.text, MAX_ANSWER_EXCERPT_CHARS, query)
    text, redacted = _redact_secret_lines(text)
    meta: dict[str, Any] = {
        "chunk_id": chunk_id,
        "truncated": truncated,
        "redacted": redacted,
        # Derived digest of JUST the excerpt window — never confused with the
        # full chunk content_digest (kept distinct and clearly labelled).
        "excerpt_digest": digest_of_bytes(text.encode("utf-8")),
        "excerpt_line_range": list(line_range),
        "content_digest": view.content_digest,
        "locator": view.locator.as_dict() if view.locator else None,
        "source_ref": view.locator.relative_path if view.locator else None,
    }
    return text, meta


#: RAG26-R04: coklu-chunk kanit icin en fazla bu kadar pencere ve toplam karakter.
MAX_ANSWER_EXCERPTS = 3
MAX_ANSWER_EXCERPTS_TOTAL_CHARS = 2400
#: Listeleme sorulari daha cok dosyaya yayilir; pencere ve toplam karakter siniri genisler.
MAX_ANSWER_EXCERPTS_ENUMERATION = 6
MAX_ANSWER_EXCERPTS_TOTAL_CHARS_ENUMERATION = 3600


_CONFIG_QUESTION_PATTERN = re.compile(
    r"(?<!\w)(?:port|portu|portta|profil|profile|profilleri|yaml|yml|properties|"
    r"config|konfig\w*|ayar\w*|environment)(?!\w)",
    re.IGNORECASE,
)
_CONFIG_SUFFIXES = (".yaml", ".yml", ".properties", ".toml", ".ini", ".env", ".conf")


def _coverage_notes(query: str, citations: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Kanitin kapsam bosluklarini dogrudan sorgu ve citation'lardan turetir (indeks okumaz).

    Config sorusunda hicbir citation config dosyasi degilse bunu acikca yazar: gizli deger
    kalibi tasiyan config dosyalari indekslenmez; cevap config degeri uydurmaz.
    """

    notes: list[dict[str, str]] = []
    if _CONFIG_QUESTION_PATTERN.search(query) and not any(
        str(item.get("source_ref") or "").lower().endswith(_CONFIG_SUFFIXES) for item in citations
    ):
        notes.append(
            {
                "code": "config-not-in-evidence",
                "detail": (
                    "Sorgu config (port/profil/ayar) ile ilgili ama kanitta config dosyasi yok; "
                    "gizli deger kalibi tasiyan config dosyalari indekslenmez. Config degerini "
                    "kaynak kodundan veya tesadufi eslesmeden cikarma."
                ),
            }
        )
    return notes


#: Kimliksiz ve en fazla bu kadar anlamli terimli sorular belirsiz sayilir.
MAX_AMBIGUOUS_QUERY_TERMS = 2


def _clarification(
    query: str, identifiers: tuple[str, ...], citations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Kisa, kimliksiz soruda tek netlestirme sorusu onerir; cevap uydurmaz.

    Onerilen alanlar (`candidate_areas`) yalniz bulunan citation yollarindan gelir.
    """

    terms = _excerpt_terms(query)
    suggested = not identifiers and 0 < len(terms) <= MAX_AMBIGUOUS_QUERY_TERMS
    areas: list[str] = []
    if suggested:
        for item in citations:
            path = str(item.get("source_ref") or "")
            parts = path.split("/")
            area = "/".join(parts[:-1][-2:]) or path
            if area and area not in areas:
                areas.append(area)
    return {
        "suggested": suggested,
        "reason": "short-ambiguous-query" if suggested else None,
        "candidate_areas": areas[:4],
    }


def _build_excerpts(
    answer: Any,
    views: dict[str, Any],
    query: str = "",
    related_ids: tuple[str, ...] = (),
    enumeration: bool = False,
) -> list[dict[str, Any]]:
    """Kullanilan chunk'larin en alakali pencerelerini, toplami sinirli listeler.

    Cevap birden cok chunk'a yayildiginda (`X hangi endpoint'leri sunar?` sinif govdesi iki
    parcadir) tek ``answer_excerpt`` yetmez. Her oge kaynak satirlarini aynen tasir, kendi
    digest/satir araligina sahiptir ve sorguyla hic eslesmeyen chunk'lar eklenmez; kaynak
    referansi veya sinif iliskisiyle genisletilen (``related_ids``) chunk'lar tabana dahildir.
    """

    if not answer.used_chunk_ids or not answer.citations:
        return []
    ranked = sorted(
        (
            (
                max(
                    _excerpt_relevance(views[chunk_id].text, query),
                    1 if chunk_id in related_ids else 0,
                ),
                -order,
                chunk_id,
            )
            for order, chunk_id in enumerate(answer.used_chunk_ids)
            if chunk_id in views
        ),
        reverse=True,
    )
    max_items = MAX_ANSWER_EXCERPTS_ENUMERATION if enumeration else MAX_ANSWER_EXCERPTS
    items: list[dict[str, Any]] = []
    remaining = (
        MAX_ANSWER_EXCERPTS_TOTAL_CHARS_ENUMERATION
        if enumeration
        else MAX_ANSWER_EXCERPTS_TOTAL_CHARS
    )
    for relevance, _, chunk_id in ranked:
        if len(items) >= max_items or remaining < 200:
            break
        if relevance <= 0 and items:
            break
        view = views[chunk_id]
        text, truncated, line_range = _excerpt_window(
            view.text, min(MAX_ANSWER_EXCERPT_CHARS, remaining), query
        )
        text, redacted = _redact_secret_lines(text)
        remaining -= len(text)
        items.append(
            {
                "chunk_id": chunk_id,
                "text": text,
                "truncated": truncated,
                "redacted": redacted,
                "excerpt_digest": digest_of_bytes(text.encode("utf-8")),
                "excerpt_line_range": list(line_range),
                "content_digest": view.content_digest,
                "source_ref": view.locator.relative_path if view.locator else None,
                "line_start": view.locator.line_start if view.locator else None,
            }
        )
    return items


#: RAG26-R04: "hangi step'leri hangi sirayla" gibi akis sorularinda, bulunan job
#: yapilandirmasi akisi baska bir dosyada tanimli bean/metod adiyla referanslar.
_FLOW_QUESTION_TERMS = (
    "step",
    "adim",
    "adım",
    "sirayla",
    "sırayla",
    "akis",
    "akış",
    "flow",
    "zincir",
    "asama",
    "aşama",
)
_REFERENCE_IDENTIFIER = re.compile(
    r"(?<![\w.])([a-z][A-Za-z0-9]*(?:Flow|Step|Job|Tasklet|Reader|Writer|Processor))\b"
)
#: Bir referansi cozmek icin en fazla bu kadar ek chunk eklenir (bounded, tek hop).
MAX_REFERENCE_EXPANSION = 3
#: Sorgu maliyeti sabit: seed basina en fazla bu kadar referans, toplamda en fazla bu kadar arama.
MAX_REFERENCE_IDENTIFIERS_PER_SEED = 6
MAX_REFERENCE_LOOKUPS = 12
#: Uye listeleme sorularinda bulunan sinifin ayni dosyadaki sonraki chunk sayisi.
MAX_NEIGHBOR_EXPANSION = 2
_REFERENCE_SEED_CHUNKS = 3


def _is_flow_question(query: str) -> bool:
    lowered = query.casefold()
    return any(term in lowered for term in _FLOW_QUESTION_TERMS)


def _reference_identifiers(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_REFERENCE_IDENTIFIER.findall(text)))


_JAVA_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)


def _defines_identifier(text: str, identifier: str) -> bool:
    """Chunk, `identifier`i yalniz cagirmakla kalmayip metod/bean olarak tanimlar.

    Yorum satirlari ve blok yorumlari tanim sayilmaz.
    """

    code = _JAVA_COMMENTS.sub(" ", text)
    definition = (
        rf"(?:public|protected|private|static)\s+[\w<>\[\],.? ]+\s+{re.escape(identifier)}\s*\("
    )
    return re.search(definition, code) is not None


_ENUMERATION_PATTERN = re.compile(
    r"(?:hangi\b(?:\s+\S+){0,4}?\s+\S*(?:lar|ler)i?\b|listele|\btum\b|\btüm\b|\bhepsi\b|\bkac\s+tane\b)",
    re.IGNORECASE,
)


def _is_enumeration_question(query: str) -> bool:
    """`hangi job'lar`, `listele`, `tum ...` gibi cok nesneli listeleme sorulari."""

    return _ENUMERATION_PATTERN.search(query) is not None


_MEMBER_QUESTION_TERMS = (
    "endpoint",
    "metod",
    "method",
    "fonksiyon",
    "function",
    "alan",
    "field",
    "sunar",
    "icerir",
    "içerir",
)
_TYPE_DECLARATION = re.compile(r"(?:class|interface|record|enum)\s+([A-Z][A-Za-z0-9_]*)")


def _is_member_question(query: str) -> bool:
    lowered = query.casefold()
    return any(term in lowered for term in _MEMBER_QUESTION_TERMS)


def _tokens(value: str) -> frozenset[str]:
    return frozenset(item.casefold() for item in _TOKEN.findall(value) if len(item) > 1)


_ASCII_FOLD = str.maketrans("çğıöşüı", "cgiosui")
_STEM_CHARS = 5


def _stems(terms: frozenset[str]) -> frozenset[str]:
    """Turkce ek/aksan farklarina dayanikli kok (ASCII-fold + ilk 5 karakter)."""
    return frozenset(term.translate(_ASCII_FOLD)[:_STEM_CHARS] for term in terms)


def _content_coverage(content_terms: frozenset[str], text: str) -> float:
    if not content_terms:
        return 0.0
    wanted = _stems(content_terms)
    return len(wanted & _stems(_tokens(text))) / len(wanted)


def _supports_identifier(text: str, identifier: str) -> bool:
    """Require one answerable chunk to contain a single technical identity."""
    text_tokens = _tokens(text)
    return all(
        part.casefold() in text_tokens
        for part in re.findall(r"[A-Za-z0-9_$#]+", identifier)
        if len(part) > 1
    )


def _supports_all_identifiers(text: str, identifiers: tuple[str, ...]) -> bool:
    """Require one answerable chunk to contain every technical query identity.

    This is the *single-object* contract: a single chunk must support the object
    (kept strict for single-object questions).  For multi-object/comparison/
    relationship questions this per-chunk requirement is intentionally NOT used —
    coverage is collective across the evidence set (WP6 / B07).
    """

    if not identifiers:
        return True
    text_tokens = _tokens(text)
    return all(
        all(
            part.casefold() in text_tokens
            for part in re.findall(r"[A-Za-z0-9_$#]+", identifier)
            if len(part) > 1
        )
        for identifier in identifiers
    )


def _collective_identifier_coverage(
    views: dict[str, Any],
    candidate_ids: tuple[str, ...],
    identifiers: tuple[str, ...],
) -> tuple[bool, frozenset[str]]:
    """Return (covered, missing) — whether the evidence SET covers every identifier.

    WP6 (B07): a verified evidence set collectively satisfies a multi-object or
    comparison question when, across all candidate chunks, every required
    identifier appears at least once.  Unlike ``_supports_all_identifiers`` this
    does NOT demand a single chunk to hold every identifier; the objects may live
    in different files/chunks.  Each identifier must be covered at least once so
    none is starved (aligns with B04 fair exact).

    Note this deliberately computes coverage from *candidate* texts regardless of
    whether they were later dropped for the context budget.  The caller applies
    budget filtering separately; evidence-gating (is there enough evidence at all)
    and context-packing (which verified evidence fits the budget) stay distinct.
    """
    if not identifiers:
        return True, frozenset()
    text_tokens: dict[str, frozenset[str]] = {}
    for chunk_id in candidate_ids:
        view = views.get(chunk_id)
        if view is not None:
            text_tokens[chunk_id] = _tokens(view.text)
    covered: set[str] = set()
    for identifier in identifiers:
        parts = [
            part.casefold() for part in re.findall(r"[A-Za-z0-9_$#]+", identifier) if len(part) > 1
        ]
        if any(all(part in tokens for part in parts) for tokens in text_tokens.values()):
            covered.add(identifier)
    missing = frozenset(identifier for identifier in identifiers if identifier not in covered)
    return not missing, missing


def _relationship_edge_evidence(views: dict[str, Any], candidate_ids: tuple[str, ...]) -> bool:
    """Return True only when at least one candidate chunk carries edge language.

    WP6 (B07) relationship rule: finding A and B separately is NOT an edge.  A
    relationship / call-chain / dependency assertion is only consider satisfied
    when an actual call-site, data-flow or dependency reference exists in the
    gathered source.  This keeps the system from fabricating an edge from mere
    co-presence of names.
    """
    lowered = " ".join(
        views[chunk_id].text.casefold() for chunk_id in candidate_ids if chunk_id in views
    )
    return any(signal in lowered for signal in _RELATIONSHIP_EDGE_SIGNALS)


def compose_graph_reranker(
    graph: GraphReadPort,
    *,
    project_id: str,
    source_revision: str,
    tree_digest: str,
    chunk_file: Callable[[str], str | None],
    enabled: bool = False,
    config: GraphRankingConfig | None = None,
) -> Reranker | None:
    """Compose an optional graph reranker.

    Default-off (task #17): unless ``enabled`` is true, ``None`` is returned so
    ``EmbeddedProjectRAG.reranker`` stays unchanged and the baseline RAG path is
    preserved byte-for-byte.  All binding identity checks run inside the
    reranker per query; a binding mismatch simply bypasses it at runtime.
    """
    if not enabled:
        return None
    try:
        return ProjectGraphReranker(
            graph,
            project_id=project_id,
            source_revision=source_revision,
            tree_digest=tree_digest,
            chunk_file=chunk_file,
            config=config,
        )
    except Exception:
        return None


def build_embedded_project_generation(
    index: KnowledgeIndexPort,
    plan: ProjectIndexPlan,
    *,
    embedding_provider: EmbeddingProvider,
    embedding_policy: EmbeddingPolicy,
    created_at: dt.datetime | None = None,
    batch_size: int = MAX_PROJECT_EMBED_BATCH_SIZE,
    cancel: threading.Event | None = None,
    deadline_monotonic: float | None = None,
    max_provider_calls: int | None = None,
) -> tuple[ProjectIndexPlan, KnowledgeGeneration]:
    """Embed bounded source chunks before atomically activating the generation.

    The older embedded path used a fixed slice size and called the provider
    directly.  That made token/payload limits, cancellation and total deadlines
    depend on the caller rather than the indexing boundary.  Keep all provider
    work before generation publication, while reusing the canonical bounded
    batching and retry policy used by the SQLite project index path.
    """

    profile = embedding_provider.describe()
    profile.assert_policy(embedding_policy)
    if (
        plan.embedding_profile.dimension != profile.dimension
        or plan.embedding_profile.model_ref
        not in {profile.exact_model_id, f"openai/{profile.exact_model_id}"}
    ):
        raise PolicyViolation("Embedded project plan/provider drift")
    provider_batch_limit = getattr(embedding_provider, "max_batch_size", 8)
    if type(provider_batch_limit) is not int or provider_batch_limit < 1:
        raise ValidationFailed("Embedded provider batch limiti gecersiz")
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_PROJECT_EMBED_BATCH_SIZE:
        raise ValidationFailed("Embedded project batch sayi siniri gecersiz")
    if cancel is not None and not isinstance(cancel, threading.Event):
        raise ValidationFailed("Embedded project cancel typed olmali")
    if deadline_monotonic is not None and (
        type(deadline_monotonic) not in (int, float)
        or not math.isfinite(float(deadline_monotonic))
    ):
        raise ValidationFailed("Embedded project deadline gecersiz")
    if max_provider_calls is not None and (
        type(max_provider_calls) is not int or max_provider_calls < 1
    ):
        raise ValidationFailed("Embedded project provider cagrisi siniri gecersiz")
    effective_batch_size = min(batch_size, provider_batch_limit)
    bound_plan = replace(
        plan,
        embedding_profile=replace(
            plan.embedding_profile,
            model_ref=profile.exact_model_id,
            dimension=profile.dimension,
            query_prefix=profile.query_prefix,
            passage_prefix=profile.passage_prefix,
            provider_profile_digest=profile.profile_digest,
        ),
    )
    source_digests = {
        item.relative_path: item.content_digest for item in bound_plan.discovery.files
    }
    vectors: dict[str, tuple[float, ...]] = {}
    provider_calls = 0
    for chunks in _project_embedding_batches(
        bound_plan.chunks,
        batch_size=effective_batch_size,
        model_id=profile.exact_model_id,
    ):
        _check_embedded_generation_budget(cancel, deadline_monotonic)
        batch = _embed_project_batch_with_retry(
            embedding_provider,
            tuple(chunk.text for chunk in chunks),
            embedding_policy,
            cancel=cancel,
            deadline_monotonic=deadline_monotonic,
        )
        _check_embedded_generation_budget(cancel, deadline_monotonic)
        if (
            len(batch.vectors) != len(chunks)
            or batch.receipt.vector_count != len(chunks)
            or batch.receipt.profile_digest != profile.profile_digest
            or batch.receipt.dimension != profile.dimension
        ):
            raise PolicyViolation("Embedded project provider partial/receipt drift")
        provider_calls += batch.receipt.provider_call_count
        if max_provider_calls is not None and provider_calls > max_provider_calls:
            raise ProviderCallQuotaExceeded("Embedded project provider cagrisi kotasi asildi")
        for chunk, vector in zip(chunks, batch.vectors, strict=True):
            profile.validate_vector(vector)
            vectors[chunk.chunk_id] = vector
    if len(vectors) != len(bound_plan.chunks):
        raise PolicyViolation("Embedded project vector set incomplete")
    _check_embedded_generation_budget(cancel, deadline_monotonic)
    records = tuple(
        KnowledgeIndexRecord(
            chunk_id=chunk.chunk_id,
            project_id=str(bound_plan.project_id),
            source_revision=bound_plan.source_revision,
            source_path=str(chunk.locator.relative_path),
            source_digest=source_digests[str(chunk.locator.relative_path)],
            locator=chunk.locator,
            text=chunk.text,
            content_digest=digest_of_bytes(chunk.text.encode("utf-8")),
            chunk_order=chunk.order,
            vector=vectors[chunk.chunk_id],
            vector_dimension=profile.dimension,
        )
        for chunk in bound_plan.chunks
    )
    moment = created_at or dt.datetime.now(dt.UTC)
    generation = index.build_generation(
        records,
        project_id=str(bound_plan.project_id),
        source_revision=bound_plan.source_revision,
        tree_digest=bound_plan.tree_digest,
        source_manifest_digest=digest_of_bytes(bound_plan.manifest),
        embedding_profile_digest=bound_plan.embedding_profile.profile_digest,
        provider_profile_digest=profile.profile_digest,
        created_at=moment.isoformat().replace("+00:00", "Z"),
    )
    return bound_plan, generation


def _check_embedded_generation_budget(
    cancel: threading.Event | None, deadline_monotonic: float | None
) -> None:
    """Fail before publication when the caller cancelled or the budget expired."""

    if cancel is not None and cancel.is_set():
        raise ProviderCallCancelled("Embedded project indexing cancelled")
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        raise ProviderDeadlineExceeded("Embedded project indexing deadline expired")


_DOC_SUFFIXES = (".md", ".mdx", ".rst", ".adoc", ".txt")
_DOC_EXCLUDED_DIRS = frozenset(
    {"graft", "node_modules", "vendor", "dist", "build", "out", "target", "archive"}
)
DOC_PRIOR_LIMIT = 3
DOC_PRIOR_DEPTH = 100


def _view_relative_path(view: Any) -> str:
    locator = getattr(view, "locator", None)
    if isinstance(locator, dict):
        value = locator.get("relative_path")
    else:
        value = getattr(locator, "relative_path", None)
    return str(value or "").replace("\\", "/")


def _is_documentation_path(path: str) -> bool:
    """README ve dokuman dosyalari; uretilmis/vendor agaclari haric."""
    parts = [part for part in path.casefold().split("/") if part]
    if not parts or any(part in _DOC_EXCLUDED_DIRS for part in parts[:-1]):
        return False
    name = parts[-1]
    return name.startswith("readme") or name.endswith(_DOC_SUFFIXES)


def _promote_documentation(
    index: KnowledgeIndexPort,
    project_id: str,
    generation_digest: str,
    hits: tuple[ScoredHit, ...],
    deep: tuple[ScoredHit, ...],
) -> tuple[ScoredHit, ...]:
    """Kimliksiz genel sorularda derin sonuc listesindeki ilk dokuman chunk'larini one al.

    Ilk sonuc korunur (kanit esikleri onun skoruna bakar); dokuman chunk'lari ondan
    hemen sonra girer, kalan siralama degismez. Yalniz zaten bulunmus adaylar yeniden
    siralanir; yeni kanit uretilmez.
    """
    if not deep:
        return hits
    views = index.views(
        project_id, tuple(hit.chunk_id for hit in deep), generation_digest=generation_digest
    )
    docs = [
        hit
        for hit in deep
        if hit.chunk_id in views
        and _is_documentation_path(_view_relative_path(views[hit.chunk_id]))
    ][:DOC_PRIOR_LIMIT]
    if not docs:
        return hits
    seen: set[str] = set()
    ordered: list[ScoredHit] = []
    for hit in (*hits[:1], *docs, *hits[1:]):
        if hit.chunk_id not in seen:
            seen.add(hit.chunk_id)
            ordered.append(hit)
    return tuple(
        replace(hit, rank=rank) if hit.rank != rank else hit
        for rank, hit in enumerate(ordered[: max(len(hits), 1)], start=1)
    )


@dataclass(slots=True)
class EmbeddedProjectSearchBackend:
    index: KnowledgeIndexPort
    project_id: str
    generation_digest: str
    embedding_provider: EmbeddingProvider
    embedding_policy: EmbeddingPolicy
    source_type: str = "embedded-project-knowledge"
    dense_enabled: bool = True
    dense_failure_reason: str | None = None
    doc_prior: bool = False
    last_exact: tuple[ScoredHit, ...] = ()
    last_lexical: tuple[ScoredHit, ...] = ()
    last_dense: tuple[ScoredHit, ...] = ()

    def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
        self.last_exact = self.index.exact(
            self.project_id,
            identifiers,
            limit=limit,
            generation_digest=self.generation_digest,
        )
        return self.last_exact

    def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        depth = max(limit, DOC_PRIOR_DEPTH) if self.doc_prior else limit
        found = self.index.lexical(
            self.project_id, query, limit=depth, generation_digest=self.generation_digest
        )
        self.last_lexical = self._with_documentation(found[:limit], found)
        return self.last_lexical

    def _with_documentation(
        self, hits: tuple[ScoredHit, ...], deep: tuple[ScoredHit, ...]
    ) -> tuple[ScoredHit, ...]:
        if not self.doc_prior:
            return hits
        return _promote_documentation(
            self.index, self.project_id, self.generation_digest, hits, deep
        )

    def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        if not self.dense_enabled:
            self.last_dense = ()
            return ()
        try:
            profile = self.embedding_provider.describe()
            profile.assert_policy(self.embedding_policy)
            batch = self.embedding_provider.embed_query(query, self.embedding_policy)
            if (
                len(batch.vectors) != 1
                or batch.receipt.vector_count != 1
                or batch.receipt.profile_digest != profile.profile_digest
                or batch.receipt.dimension != profile.dimension
            ):
                raise PolicyViolation("Embedded query provider exact tek vector ister")
            profile.validate_vector(batch.vectors[0])
        except Exception as exc:
            self.dense_failure_reason = f"query-embedding-failed:{type(exc).__name__}"
            self.last_dense = ()
            return ()
        depth = max(limit, DOC_PRIOR_DEPTH) if self.doc_prior else limit
        found = self.index.dense(
            self.project_id, batch.vectors[0], limit=depth, generation_digest=self.generation_digest
        )
        self.last_dense = self._with_documentation(found[:limit], found)
        return self.last_dense


@dataclass(frozen=True, slots=True)
class EmbeddedProjectRAG:
    index: KnowledgeIndexPort
    embedding_provider: EmbeddingProvider
    embedding_policy: EmbeddingPolicy
    reranker: Reranker | None = None
    dense_evidence_threshold: float = DEFAULT_DENSE_EVIDENCE_THRESHOLD
    dense_margin_threshold: float = DEFAULT_DENSE_MARGIN_THRESHOLD
    lexical_coverage_threshold: float = DEFAULT_LEXICAL_COVERAGE_THRESHOLD
    # WP5 (P0): one monotonic end-to-end retrieval deadline shared across the
    # discovery/qualification, channels, reranker and fallback; ``None`` disables.
    retrieval_deadline_seconds: float | None = DEFAULT_RETRIEVAL_DEADLINE_SECONDS

    def _stale_result(self, query: str, *, project_id: str, reason: str) -> dict[str, Any]:
        state_value = "abstained-index-unavailable"
        value = {
            "schema": "zekam-embedded-rag-result/v1",
            "state": state_value,
            "project_id": project_id,
            "query_digest": digest({"query": query}),
            "reason": reason,
            "citations": [],
            "searched_channels": [],
            "fallback_allowed": False,
        }
        # WP7 / B08: the embedded contract carries the explicit retrieval-only
        # semantics on every result, including fail-closed stale/index results.
        value["evidence_found"] = False
        value.update(answer_semantics(state_value, evidence_found=False))
        value["retrieval_digest"] = digest(value)
        return value

    def _expand_references(
        self,
        query: str,
        hits: tuple[FusedHit, ...],
        *,
        project_id: str,
        generation_digest: str,
    ) -> tuple[tuple[FusedHit, ...], tuple[str, ...]]:
        """Akis sorusunda, en iyi chunk'larin referans verdigi tanimlari ekler.

        Yalniz gercek kaynak referansi kullanilir: tek hop, en fazla
        ``MAX_REFERENCE_EXPANSION`` ek chunk. Isim birlikteligi yeterli degildir; ek chunk
        referansi metod/bean olarak TANIMLAMALIDIR. Kaynakta tanim yoksa hicbir sey eklenmez.
        """

        if not hits or not _is_flow_question(query):
            return hits, ()
        seed_ids = tuple(hit.chunk_id for hit in hits[:_REFERENCE_SEED_CHUNKS])
        seed_views = self.index.views(project_id, seed_ids, generation_digest=generation_digest)
        known = {hit.chunk_id for hit in hits}
        floor = min(hit.score for hit in hits)
        added: list[FusedHit] = []
        referenced: list[str] = []
        lookups = 0
        for chunk_id in seed_ids:
            view = seed_views.get(chunk_id)
            if view is None:
                continue
            identifiers_in_seed = _reference_identifiers(view.text)[
                :MAX_REFERENCE_IDENTIFIERS_PER_SEED
            ]
            for identifier in identifiers_in_seed:
                if len(referenced) >= MAX_REFERENCE_EXPANSION or lookups >= MAX_REFERENCE_LOOKUPS:
                    break
                lookups += 1
                found = self.index.exact(
                    project_id, (identifier,), limit=4, generation_digest=generation_digest
                )
                candidates = tuple(item.chunk_id for item in found if item.chunk_id != chunk_id)
                if not candidates:
                    continue
                views = self.index.views(
                    project_id, candidates, generation_digest=generation_digest
                )
                for candidate in candidates:
                    candidate_view = views.get(candidate)
                    if candidate_view is None or not _defines_identifier(
                        candidate_view.text, identifier
                    ):
                        continue
                    if candidate in referenced or len(referenced) >= MAX_REFERENCE_EXPANSION:
                        continue
                    referenced.append(candidate)
                    if candidate not in known:
                        known.add(candidate)
                        added.append(
                            FusedHit(
                                candidate,
                                floor * 0.999,
                                (RetrievalChannel.EXACT,),
                                exact_match=False,
                            )
                        )
        return (*hits, *added), tuple(referenced)

    def _expand_neighbors(
        self,
        query: str,
        hits: tuple[FusedHit, ...],
        *,
        project_id: str,
        generation_digest: str,
    ) -> tuple[tuple[FusedHit, ...], tuple[str, ...]]:
        """Uye sorusunda, sorguda adi gecen sinifi tanimlayan chunk'in sonraki chunk'larini ekler.

        Sinif govdesi birden cok chunk'a bolunmus olabilir (`X hangi endpoint'leri sunar?`).
        Yalniz sorguda adi gecen bir tipin tanim chunk'i icin, ayni dosyadaki en fazla
        ``MAX_NEIGHBOR_EXPANSION`` sonraki chunk eklenir. Baska hicbir yere komsu eklenmez.
        """

        if not hits or not _is_member_question(query):
            return hits, ()
        lowered_query = query.casefold()
        seed_ids = tuple(hit.chunk_id for hit in hits[:_REFERENCE_SEED_CHUNKS])
        seed_views = self.index.views(project_id, seed_ids, generation_digest=generation_digest)
        known = {hit.chunk_id for hit in hits}
        floor = min(hit.score for hit in hits)
        added: list[FusedHit] = []
        related: list[str] = []
        for chunk_id in seed_ids:
            view = seed_views.get(chunk_id)
            if view is None:
                continue
            declared = {name.casefold() for name in _TYPE_DECLARATION.findall(view.text)}
            if not any(name in lowered_query for name in declared):
                continue
            followers = self.index.following_chunk_ids(
                project_id,
                chunk_id,
                count=MAX_NEIGHBOR_EXPANSION,
                generation_digest=generation_digest,
            )
            for follower in followers:
                if follower in related or len(related) >= MAX_NEIGHBOR_EXPANSION:
                    continue
                related.append(follower)
                if follower not in known:
                    known.add(follower)
                    added.append(
                        FusedHit(
                            follower, floor * 0.998, (RetrievalChannel.EXACT,), exact_match=False
                        )
                    )
        return (*hits, *added), tuple(related)

    def _architecture_summary(
        self,
        query: str,
        project_id: str,
        generation: KnowledgeGeneration,
    ) -> dict[str, Any] | None:
        """Kaynak-mimari sorusu icin sinirli, kod-turetimli ozet; uydurma yok.

        Proje+generation'a pinli ``architecture_candidates`` orneklemeden
        chunk view'leri ve source identity'leri tek toplu bounded okumayla
        alinir; on ozet saf ``build_architecture_summary`` yardimcisiyla
        kurulur. Aday secimi dosya basina yalniz ilk index chunk'ini
        aldigindan, on ozette henuz rol kaniti olmayan modullerin
        temsilcileri icin ayni kaynagin izleyen index chunk'lari
        ``following_chunk_ids`` ile bounded probe edilir (oncelik: frontend
        kod uzantilari, grup icinde aday sirasi korunur). Boylece
        import/baslik chunk'inden sonra gelen gercek UI kodu kanita girer;
        rol cikarimi import/dosya adindan yapilmaz, yalniz chunk icerigi
        konusur. Toplam chunk sayisi ``ARCHITECTURE_CANDIDATE_LIMIT``'i
        asamaz; yeni id'ler tek toplu views/source_identities okumasiyla
        birlestirilir ve ozet genisletilmis chunk kumesiyle bir kez yeniden
        kurulur. Aday kesintisi, index'in kanit kesmesi/birakmasi veya yeni
        butcenin dolmasi truncated taniyi dogrular. ``source_inventory`
        sayaclari ekleyici taniidir, authority degildir. Kod kaniti yoksa
        ``insufficient`` ozeti donebilir; hicbir durumda plan/dokuman
        fallback'u veya ham dosya okuma fallback'u yapilmaz. Grenc/uydurma
        chunk adaptasyonu yapilmaz: view/identity kapilari index'in
        kendisidir.
        """
        candidates = self.index.architecture_candidates(
            project_id,
            generation_digest=generation.generation_digest,
            limit=ARCHITECTURE_CANDIDATE_LIMIT,
            max_modules=ARCHITECTURE_MAX_MODULES,
            per_module=ARCHITECTURE_PER_MODULE,
        )
        raw_candidate_ids = tuple(candidates.get("chunk_ids") or ())
        candidate_ids = raw_candidate_ids[:ARCHITECTURE_CANDIDATE_LIMIT]
        candidate_ids_clamped = len(raw_candidate_ids) > len(candidate_ids)
        # Aday listesi bos olsa bile ozet kurucu CALISTIRILIR: yapisal
        # 'insufficient' ozeti uretilir; normal-dokuman/dosya fallback'u
        # hicbir durumda yapilmaz.
        views = (
            self.index.views(
                project_id,
                candidate_ids,
                generation_digest=generation.generation_digest,
            )
            if candidate_ids
            else {}
        )
        identities = (
            self.index.source_identities(
                project_id,
                candidate_ids,
                generation_digest=generation.generation_digest,
            )
            if candidate_ids
            else {}
        )
        identity_map = {
            chunk_id: dict(identity) for chunk_id, identity in identities.items()
        }
        candidate_metadata = {
            "chunk_ids": candidate_ids,
            "truncated": (
                bool(candidates.get("truncated", False))
                or candidate_ids_clamped
            ),
            "observed_module_count": candidates.get("observed_module_count"),
            "sampled_module_count": candidates.get("sampled_module_count"),
        }
        preliminary = build_architecture_summary(
            query=query,
            project_id=project_id,
            generation_digest=generation.generation_digest,
            source_revision=generation.source_revision,
            views=views,
            identities=identity_map,
            candidate_metadata=candidate_metadata,
        )
        if not isinstance(preliminary, dict):
            return None
        representative_count = len(views)
        neighbor_ids: list[str] = []
        truncated = bool(candidates.get("truncated", False)) or candidate_ids_clamped
        if candidate_ids:

            def _locator_path(view: Any) -> str | None:
                locator = getattr(view, "locator", None)
                if locator is None and isinstance(view, dict):
                    locator = view.get("locator")
                path = getattr(locator, "relative_path", None)
                if path is None and isinstance(locator, dict):
                    path = locator.get("relative_path")
                return path if isinstance(path, str) else None

            # Rol kaniti olan moduller tekrar probe edilmez; "kanitli"
            # bilgisi yalniz on ozet iceriginden gelir (import/dosya
            # adindan degil).
            proven: set[str] = set()
            module_entries = preliminary.get("modules")
            if isinstance(module_entries, list):
                for entry in module_entries:
                    if not isinstance(entry, dict):
                        continue
                    module_name = entry.get("module")
                    if isinstance(module_name, str) and any(
                        role in (entry.get("roles") or ())
                        for role in (
                            "http_api",
                            "service_logic",
                            "data_access",
                            "frontend_rendering",
                            "frontend_api_requests",
                            "application_entrypoint",
                        )
                    ):
                        proven.add(module_name)
            frontend_extensions = (".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte")
            frontend_representatives: list[str] = []
            other_representatives: list[str] = []
            seen_ids: set[str] = set()
            for chunk_id in candidate_ids:
                view = views.get(chunk_id)
                if view is None:
                    continue
                path = _locator_path(view)
                if path is None:
                    continue
                module_name = source_module(path)
                if not isinstance(module_name, str) or module_name in proven:
                    continue
                if chunk_id in seen_ids:
                    continue
                seen_ids.add(chunk_id)
                if path.lower().endswith(frontend_extensions):
                    frontend_representatives.append(chunk_id)
                else:
                    other_representatives.append(chunk_id)
            remaining = ARCHITECTURE_CANDIDATE_LIMIT - len(candidate_ids)
            # Oncelik sirasi: frontend uzantilari once; her grup icinde
            # aday sirasi korunur, modul round-robin sirasi bozulmaz.
            for representative_id in frontend_representatives + other_representatives:
                if remaining <= 0:
                    # Yeni komsu butcesi doldu: parcali taniyi koru.
                    truncated = True
                    break
                count = min(2, remaining)
                following = self.index.following_chunk_ids(
                    project_id,
                    representative_id,
                    count=count,
                    generation_digest=generation.generation_digest,
                )
                if len(following) < count:
                    # Index kaniti kesti/birakti: truncated tani dusurulmez.
                    truncated = True
                for following_id in following:
                    if following_id in seen_ids or following_id in candidate_ids:
                        continue
                    seen_ids.add(following_id)
                    neighbor_ids.append(following_id)
                    remaining -= 1
                    if remaining <= 0:
                        truncated = True
                        break
        if neighbor_ids:
            # Yeni id'ler icin tek toplu views + source_identities okumasi;
            # ham dosya fallback'u yok, kapilar index'in kendisidir.
            neighbor_views = self.index.views(
                project_id,
                tuple(neighbor_ids),
                generation_digest=generation.generation_digest,
            )
            views = {**views, **neighbor_views}
            neighbor_identities = self.index.source_identities(
                project_id,
                tuple(neighbor_ids),
                generation_digest=generation.generation_digest,
            )
            for chunk_id, identity in neighbor_identities.items():
                identity_map.setdefault(chunk_id, dict(identity))
            candidate_metadata = {
                **candidate_metadata,
                "chunk_ids": candidate_ids + tuple(neighbor_ids),
                "truncated": truncated,
            }
            summary = build_architecture_summary(
                query=query,
                project_id=project_id,
                generation_digest=generation.generation_digest,
                source_revision=generation.source_revision,
                views=views,
                identities=identity_map,
                candidate_metadata=candidate_metadata,
            )
        else:
            summary = preliminary
        if not isinstance(summary, dict):
            return None
        # Tanisal sayclar ekleyicidir; ozet digesti claims/coverage/bindings
        # uzerinden kurulur, bu sayclar authority tasimaz.
        summary["source_inventory"] = {
            "representative_count": representative_count,
            "expanded_chunk_count": len(neighbor_ids),
            "total_chunk_count": len(candidate_ids) + len(neighbor_ids),
            "candidate_limit": ARCHITECTURE_CANDIDATE_LIMIT,
        }
        return summary

    def query(
        self,
        query: str,
        *,
        project_id: str,
        expected_source_revision: str,
        expected_tree_digest: str,
        token_budget: int = 1200,
        dense_disabled_reason: str | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query.encode("utf-8")) > MAX_QUERY_BYTES
        ):
            raise ValidationFailed("RAG query bounded non-empty text olmali")
        try:
            generation = self.index.generation(project_id)
        except ValidationFailed:
            return self._stale_result(query, project_id=project_id, reason="generation-missing")
        if generation.state != "ready":
            return self._stale_result(query, project_id=project_id, reason="generation-not-ready")
        if generation.source_revision != expected_source_revision:
            raise PolicyViolation("RAG generation source revision binding drift")
        if generation.tree_digest != expected_tree_digest:
            raise PolicyViolation("RAG generation source tree binding drift")
        from zekam.application.metadata_schema_answer import (
            build_metadata_schema_result,
            is_database_schema_query,
        )

        if is_database_schema_query(query):
            return build_metadata_schema_result(
                self.index, query, project_id=project_id, generation=generation
            )
        stale_reasons: list[str] = []
        provider_available = False
        provider_failure_reason = dense_disabled_reason or "provider-unavailable"
        if generation.provider_profile_digest != self.embedding_policy.expected_profile_digest:
            provider_failure_reason = "provider-profile-stale"
            stale_reasons.append("embedding-profile-stale")
        elif dense_disabled_reason is None:
            try:
                profile = self.embedding_provider.describe()
                if profile.profile_digest != generation.provider_profile_digest:
                    provider_failure_reason = "provider-profile-stale"
                    stale_reasons.append("embedding-profile-stale")
                else:
                    profile.assert_policy(self.embedding_policy)
                    health = self.embedding_provider.health()
                    provider_available = (
                        health.healthy and health.profile_digest == profile.profile_digest
                    )
                    if not provider_available:
                        provider_failure_reason = "provider-health-unavailable"
            except Exception as exc:
                provider_failure_reason = f"provider-probe-failed:{type(exc).__name__}"

        backend = EmbeddedProjectSearchBackend(
            self.index,
            project_id,
            generation.generation_digest,
            self.embedding_provider,
            self.embedding_policy,
            dense_enabled=provider_available,
            dense_failure_reason=None if provider_available else provider_failure_reason,
            doc_prior=not extract_identifiers(query)
            and not is_architecture_query(query),
        )
        service = RetrievalService(
            backend,
            reranker=self.reranker,
            retrieval_deadline_seconds=self.retrieval_deadline_seconds,
        )
        hits, trace = service.search(query)
        searched_ids = frozenset(hit.chunk_id for hit in hits)
        hits, expanded_ids = self._expand_references(
            query, hits, project_id=project_id, generation_digest=generation.generation_digest
        )
        hits, neighbor_ids = self._expand_neighbors(
            query, hits, project_id=project_id, generation_digest=generation.generation_digest
        )
        expanded_ids = (*expanded_ids, *neighbor_ids)
        candidate_ids = tuple(hit.chunk_id for hit in hits)
        # Genisletilen chunk'lar cevap govdesidir; kimlik kapsami ve iliski kaniti yalniz
        # arama kanallarinin buldugu chunk'lardan hesaplanir (yapay kanit uretilemez).
        evidence_ids = tuple(chunk_id for chunk_id in candidate_ids if chunk_id in searched_ids)
        views = self.index.views(
            project_id,
            candidate_ids,
            generation_digest=generation.generation_digest,
        )
        query_terms = _tokens(query)
        identifiers = extract_identifiers(query)
        lexical_coverage = max(
            (
                len(query_terms & _tokens(views[hit.chunk_id].text)) / len(query_terms)
                for hit in backend.last_lexical
                if query_terms and hit.chunk_id in views
            ),
            default=0.0,
        )
        # Dokuman onceligi sirayi degistirir; esik kararlari ham skor siralamasindan gelir.
        dense_scores = sorted((hit.raw_score for hit in backend.last_dense), reverse=True)
        top_dense = dense_scores[0] if dense_scores else -1.0
        dense_margin = top_dense - dense_scores[1] if len(dense_scores) > 1 else 0.0
        # WP6 (B07): split the evidence contract by query intent instead of a
        # blanket "one chunk must hold every identifier" requirement.
        #   * single-object (EXACT_LOOKUP or one identifier): a single chunk must
        #     genuinely support the object (strict verification preserved).
        #   * multi-object / comparison (MULTI_OBJECT_COMPARISON or >=2 identifiers):
        #     the verified evidence SET collectively covers all required objects
        #     across chunks; no single-chunk-everything demand.
        #   * relationship (RELATIONSHIP): the set must cover the objects AND an
        #     actual call-site / dependency / data-flow edge must exist — mere
        #     co-presence of A and B is NOT evidence of a relationship (no edge
        #     fabrication).
        intent = _classify_intent(query, identifiers)
        single_object = intent.value == QueryIntent.EXACT_LOOKUP.value or (
            # A single-identifier relationship/comparison still targets one
            # object (e.g. "X hangi fonksiyonu cagirir") — strict object
            # support applies, the edge rule is separate.
            len(identifiers) <= 1
        )
        # ``single_chunk_identifier_support`` stays a strict, honest report of
        # whether ANY single chunk holds EVERY identifier (the pre-WP6 contract).
        # It is intentionally NOT relaxed for multi-object questions: it remains
        # a diagnostic even though the *gate* uses the collective contract below.
        single_chunk_identifier_support = any(
            hit.chunk_id in views
            and _supports_all_identifiers(views[hit.chunk_id].text, identifiers)
            for hit in (*backend.last_exact, *backend.last_lexical, *backend.last_dense[:2])
        )
        if single_object:
            coverage_missing: frozenset[str] = frozenset()

            def identity_support_for(text: str) -> bool:
                return _supports_all_identifiers(text, identifiers)
        else:
            # Multi-object: the hits already represent verified candidates.  We
            # require that the evidence SET collectively covers every identifier.
            _, coverage_missing = _collective_identifier_coverage(views, evidence_ids, identifiers)

            def identity_support_for(text: str) -> bool:
                return True

        exact_identity_support = any(
            hit.chunk_id in views and identity_support_for(views[hit.chunk_id].text)
            for hit in backend.last_exact
        )
        lexical_identity_support = any(
            hit.chunk_id in views and identity_support_for(views[hit.chunk_id].text)
            for hit in backend.last_lexical
        )
        content_terms = query_terms - _EXCERPT_STOPWORDS - _QUESTION_FILLER
        dense_identity_support = any(
            hit.chunk_id in views
            and identity_support_for(views[hit.chunk_id].text)
            and (
                # Kimliksiz (anlamsal) sorularda kimlik kaniti yoktur; yoğun kanalin ilk
                # sonucu da sorunun kelimelerinden en az bir kismini kendi govdesinde
                # tasimali. Aksi halde alakasiz ama vektor olarak yakin bir chunk
                # (ornegin buyuk bir katalog dosyasi) "answered" uretir.
                identifiers
                or not content_terms
                or _content_coverage(content_terms, views[hit.chunk_id].text)
                >= self.lexical_coverage_threshold / 2
            )
            for hit in backend.last_dense[:2]
        )
        enough_evidence = (
            exact_identity_support
            or (lexical_coverage >= self.lexical_coverage_threshold and lexical_identity_support)
            or (
                top_dense >= self.dense_evidence_threshold
                and dense_identity_support
                and (
                    dense_margin >= self.dense_margin_threshold
                    or (
                        lexical_coverage >= self.lexical_coverage_threshold / 2
                        and lexical_identity_support
                    )
                )
            )
        )
        # Multi-object coverage must not starve any required identifier (B04 fair
        # exact).  When an identifier is completely absent from the evidence set,
        # there is not enough evidence to answer the multi-object question.
        if not single_object and coverage_missing:
            enough_evidence = False
        # Kaynak-mimari sorusu: sinirli kod-turetimli ozet HER ZAMAN turetilir
        # (bos aday listesi dahil; yapisal 'insufficient' ozeti normal-dokuman
        # fallback'unu engeller). Early enough_evidence mutasyonu KALDIRILDI:
        # normal kanit kapilari burada degistirilmez; FINAL mimari
        # projeksiyonu asagida kanit/used_ids/excerpt'i ozet kanitindan
        # degistirir. Normal sorgulara dokunulmaz.
        architecture_summary: dict[str, Any] | None = None
        architecture_only = is_architecture_query(query)
        if architecture_only:
            architecture_summary = self._architecture_summary(
                query, project_id, generation
            )
        # WP6 (B07) relationship rule: finding names A and B separately is not an
        # edge.  For relationship/call-chain/dependency questions, enough_evidence
        # additionally requires genuine edge language in the gathered source;
        # otherwise the system abstains rather than fabricating a relationship.
        if (
            intent.value == QueryIntent.RELATIONSHIP.value
            and identifiers
            and not _relationship_edge_evidence(views, evidence_ids)
        ):
            enough_evidence = False
            if identifiers and not coverage_missing:
                # Names exist but no edge: explicit no-edge abstain state (never a
                # confidently fabricated relationship answer).
                relationship_state = "abstained-no-edge"
            else:
                relationship_state = None
        else:
            relationship_state = None

        if identifiers and single_object:
            # Single-object strictness: only hits whose single chunk supports the
            # object are retained (preserved, not loosened).
            hits = tuple(
                hit
                for hit in hits
                if hit.chunk_id in views
                and (
                    hit.chunk_id in expanded_ids
                    or _supports_all_identifiers(views[hit.chunk_id].text, identifiers)
                )
            )
        elif identifiers:
            # Multi/relationship: no per-chunk-everything filter.  Keep hits that
            # exist in the verified views; the collective set already proved
            # coverage (deduped/counted in ``enough_evidence``/coverage_missing).
            hits = tuple(hit for hit in hits if hit.chunk_id in views)
        if not enough_evidence:
            hits = ()
        # Kaynak-mimari sorusu: ozet destekliyse CODE citation'lari ozetin
        # kanit chunk'larindan kurulur (hits'ten BAGIMSIZ; normal sorgunun
        # task-plan dokumanlari asla citation kaynagi olmaz). Bunlar orijinal
        # sorgunun rank/kanal kaniti DEGIL; provenance acikca etiketlenir
        # (structural-expansion, uydurma rank/skor yok).
        architecture_citations: list[dict[str, Any]] = []
        if (
            architecture_only
            and architecture_summary is not None
            and architecture_summary.get("status") == "supported"
        ):
            arch_view_ids = tuple(architecture_summary.get("used_chunk_ids") or ())
            arch_views = self.index.views(
                project_id, arch_view_ids, generation_digest=generation.generation_digest
            )
            arch_identities = self.index.source_identities(
                project_id, arch_view_ids, generation_digest=generation.generation_digest
            )
            for order, chunk_id in enumerate(arch_view_ids, start=1):
                view = arch_views.get(chunk_id)
                identity = arch_identities.get(chunk_id)
                if view is None or identity is None:
                    continue
                architecture_citations.append(
                    {
                        "source_id": identity["source_id"],
                        "project_scope": project_id,
                        "source_ref": identity["source_ref"],
                        "source_revision": identity["source_revision"],
                        "source_digest": identity["source_digest"],
                        "content_digest": identity["content_digest"],
                        "chunk_id": chunk_id,
                        "locator_type": (
                            "database-object"
                            if view.locator.object_name
                            else "project-file"
                        ),
                        "locator": view.locator.as_dict(),
                        # Structural expansion provenance: kod envanteri kaniti,
                        # orijinal sorgu rank'i veya dense skoru DEGIL.
                        "retrieval_channels": [],
                        "rank_trace": {
                            "fused_rank": None,
                            "rrf_score": None,
                            "exact_match": False,
                            "channel_ranks": {},
                            "provenance": "architecture-structural-expansion",
                        },
                        "source_kind": "code",
                        "rank": order,
                        "architecture_order": order,
                    }
                )
        answer = service.build_answer(
            query,
            hits,
            RetrievalTrace(
                identifiers=extract_identifiers(query),
                per_channel=trace.per_channel,
                fused_count=trace.fused_count,
                after_dedupe=trace.after_dedupe,
                reranker_used=trace.reranker_used,
                reranker_failed=trace.reranker_failed,
                source_type=trace.source_type,
                graph_used=trace.graph_used,
                graph_state=trace.graph_state,
                graph_bypass=trace.graph_bypass,
                intent=trace.intent,
                deadline_expired=trace.deadline_expired,
                degraded_reason=trace.degraded_reason,
            ),
            views=views,
            token_budget=token_budget,
            max_packed_chunks=(
                MAX_PACKED_CHUNKS_ENUMERATION
                if _is_enumeration_question(query)
                else MAX_PACKED_CHUNKS
            ),
        )
        citations: list[dict[str, Any]] = []
        if answer.citations:
            # WP4 (B04): bulk-hydrate the identity fields for every cited chunk
            # in ONE bounded SQL read instead of one source_identity round-trip
            # per citation (the N+1 pattern).  Same validation and output shape
            # as the previous per-citation path; only the final citation chunk
            # set is read.  source_identity is kept for any other callers.
            citation_ids = tuple(citation.chunk_id for citation in answer.citations)
            identities = self.index.source_identities(
                project_id,
                citation_ids,
                generation_digest=generation.generation_digest,
            )
        else:
            identities = {}
        for citation in answer.citations:
            identity = identities[citation.chunk_id]
            view = views[citation.chunk_id]
            fused_hit = next(hit for hit in hits if hit.chunk_id == citation.chunk_id)
            fused_rank = next(
                rank for rank, hit in enumerate(hits, start=1) if hit.chunk_id == citation.chunk_id
            )
            # Reference/neighbor expansion can add a citation absent from the
            # original query's channel results. Keep its source evidence without
            # inventing an original-query rank for that contextual lookup.
            channel_ranks = {
                item.channel.value: item.rank
                for item in (*backend.last_exact, *backend.last_lexical, *backend.last_dense)
                if item.chunk_id == citation.chunk_id and item.channel in fused_hit.channels
            }
            citations.append(
                {
                    "source_id": identity["source_id"],
                    "project_scope": project_id,
                    "source_ref": identity["source_ref"],
                    "source_revision": identity["source_revision"],
                    "source_digest": identity["source_digest"],
                    "content_digest": identity["content_digest"],
                    "chunk_id": citation.chunk_id,
                    "locator_type": (
                        "database-object" if view.locator.object_name else "project-file"
                    ),
                    "locator": view.locator.as_dict(),
                    "retrieval_channels": [item.value for item in fused_hit.channels],
                    "rank_trace": {
                        "fused_rank": fused_rank,
                        "rrf_score": fused_hit.score,
                        "exact_match": fused_hit.exact_match,
                        "channel_ranks": channel_ranks,
                    },
                }
            )
        state = answer.state.value
        # WP6 (B07): explicit no-edge abstain for relationship questions where
        # names exist but no call-site/dependency evidence was found.  This
        # overrides even a generic answered state so the system never fabricates
        # a relationship assertion from mere co-presence of A and B.
        if relationship_state:
            state = relationship_state
            citations = []
        if not enough_evidence and state == AnswerState.ABSTAINED_NO_HIT.value:
            state = AnswerState.ABSTAINED_LOW_EVIDENCE.value
        if backend.dense_failure_reason:
            # WP5 (P0): provider/dependency unavailable.  Strong exact/lexical
            # evidence gathered before the failure may still be returned, but it
            # is marked explicitly degraded ("lexical-only-degraded") rather than
            # a silent full success.  Without strong evidence the result
            # explicitly abstains (never a fabricated "answered").
            if state in (
                AnswerState.ANSWERED.value,
                AnswerState.DEGRADED_PROVIDER_UNAVAILABLE.value,
            ) and (
                exact_identity_support
                or (
                    lexical_coverage >= self.lexical_coverage_threshold and lexical_identity_support
                )
            ):
                state = "lexical-only-degraded"
            else:
                state = AnswerState.ABSTAINED_LOW_EVIDENCE.value
                citations = []
        # Kaynak-mimari sorusu: FINAL mimari projeksiyonu kaniti tamamen
        # degistirir. Ozet destekliyse citation'lar YALNIZ pinli view/identity
        # kanitindan gelen CODE citation'laridir (eski hits/task-plan dokumanlari
        # asla kanit kaynagi olmaz); used_chunk_ids yalniz ozet kanit
        # id'leridir. Provider saglikliysa ve denseFailure yoksa "answered";
        # dense kapaliysa acik "lexical-only-degraded" (tam saglik iddia
        # EDILMEZ). Ozet 'insufficient' veya destekli ozette gecerli citation
        # kurulamadiysa: dokuman/plan fallback YOK, acik abstain
        # (citation/used_ids bos). Normal sorgulara dokunulmaz.
        architecture_final = architecture_only and architecture_summary is not None
        architecture_supported = architecture_final and bool(
            architecture_summary.get("status") == "supported" and architecture_citations
        )
        if architecture_final:
            if architecture_supported:
                state = (
                    "lexical-only-degraded"
                    if backend.dense_failure_reason
                    else AnswerState.ANSWERED.value
                )
                citations = architecture_citations
                relationship_state = None
            else:
                state = AnswerState.ABSTAINED_LOW_EVIDENCE.value
                citations = []
            enough_evidence = architecture_supported
        excerpt, excerpt_meta = _build_excerpt(answer, views, query)
        answer_excerpts = _build_excerpts(
            answer, views, query, expanded_ids, enumeration=_is_enumeration_question(query)
        )
        used_chunk_ids = list(answer.used_chunk_ids)
        if architecture_final:
            # Mimari soruda excerpt alanlari task-doc answer'indan asla
            # tasinmaz; kullaniciya giden yazi architecture_summary.text'tir
            # ve CLI onu secer. Kanit id'leri yalniz ozet kanitindan gelir.
            excerpt = None
            excerpt_meta = None
            answer_excerpts = []
            used_chunk_ids = (
                list(architecture_summary.get("used_chunk_ids") or ())
                if architecture_supported
                else []
            )
        coverage_notes_out = _coverage_notes(query, citations)
        if architecture_final:
            coverage_notes_out = coverage_notes_out + [
                {"code": "architecture-summary-coverage", "detail": str(note)}
                for note in (architecture_summary.get("coverage") or {}).get("notes") or ()
                if isinstance(note, str)
            ]
        # WP7 / B08: `evidence_found` is derived from actual citations carried by
        # the result (never from the `state` string alone) so the retrieval
        # outcome and the payload reality stay consistent.
        evidence_found = bool(citations)
        state_value = state
        result: dict[str, Any] = {
            "schema": "zekam-embedded-rag-result/v1",
            "state": state_value,
            "project_id": project_id,
            "query_digest": answer.query_digest,
            "generation_digest": generation.generation_digest,
            "source_revision": generation.source_revision,
            "tree_digest": generation.tree_digest,
            "embedding_profile_digest": generation.embedding_profile_digest,
            "provider_profile_digest": generation.provider_profile_digest,
            "searched_channels": (
                ["exact", "lexical", "dense"] if provider_available else ["exact", "lexical"]
            ),
            "channel_counts": trace.per_channel,
            "graph_reranker": (
                {
                    "used": trace.graph_used,
                    "state": trace.graph_state,
                    "bypass": trace.graph_bypass,
                }
                if trace.graph_used
                else None
            ),
            "candidate_count": len(candidate_ids),
            "lexical_coverage": lexical_coverage,
            "identifier_count": len(identifiers),
            "query_intent": (
                ARCHITECTURE_INTENT if architecture_final else intent.value
            ),
            "uncovered_identifiers": list(coverage_missing),
            "relationship_state": relationship_state,
            "single_chunk_identifier_support": single_chunk_identifier_support,
            "top_dense_similarity": top_dense,
            "dense_top_2_margin": dense_margin,
            "evidence_sufficient": enough_evidence,
            "degraded_reason": backend.dense_failure_reason,
            "index_freshness": "stale" if stale_reasons else "current",
            "stale_reasons": stale_reasons,
            "snapshot_only": bool(stale_reasons),
            "reindex_recommended": bool(stale_reasons),
            "citations": citations,
            "used_chunk_ids": used_chunk_ids,
            "tokens_used": answer.tokens_used,
            "fallback_allowed": False,
            "answer_excerpt": excerpt,
            "answer_excerpts": answer_excerpts,
            "clarification": _clarification(query, identifiers, citations),
            "coverage_notes": coverage_notes_out,
            "answer_excerpt_meta": excerpt_meta,
            # Kaynak-mimari ek kanit alanlari; normal sorgularda None kalir ve
            # diger alanlar bayt-bayt'a aynen korunur.
            "architecture_summary": architecture_summary,
            "architecture_code_inventory": (
                {
                    "requested_modules": ARCHITECTURE_MAX_MODULES,
                    "per_module": ARCHITECTURE_PER_MODULE,
                    "candidate_limit": ARCHITECTURE_CANDIDATE_LIMIT,
                }
                if architecture_only
                else None
            ),
            "reference_expansion": list(expanded_ids),
            "explanation": list(answer.explanation),
        }
        # WP7 / B08: additive answer-semantics fields.  The legacy ``state`` and
        # ``answer_excerpt`` keys above are preserved byte-for-byte for v1
        # consumers; these NEW fields make explicit that this is a RETRIEVAL-ONLY
        # evidence packet and that NO generated natural-language answer exists in
        # this core route.  ``generation_state`` is always ``not_generated`` and
        # ``answer_kind`` is never ``generated_answer`` here (provider-less and
        # unauthorized states included) — no fabricated synthesis is ever claimed.
        result["evidence_found"] = evidence_found
        result.update(answer_semantics(state_value, evidence_found=evidence_found))
        result["retrieval_digest"] = digest(result)
        return result
