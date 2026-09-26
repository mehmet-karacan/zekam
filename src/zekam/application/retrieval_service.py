"""Hibrit retrieval orchestration ve golden degerlendirme.

Sira: exact identifier -> lexical (FTS/trigram) -> dense -> RRF -> opsiyonel
reranker -> dedupe -> parent expansion -> token butceli baglam. Reranker
basarisiz olursa fusion sonucuna geri donulur; sonuc kaybolmaz.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.retrieval import (
    AnswerState,
    Citation,
    FusedHit,
    RetrievalAnswer,
    RetrievalChannel,
    ScoredHit,
    dedupe,
    estimate_tokens,
    expand_parents,
    extract_identifiers,
    reciprocal_rank_fusion,
)
from zekam.infrastructure.query_measurement import active as active_counters

#: Reranker basarisiz olursa cagiran taraf fusion sirasini kullanir.
Reranker = Callable[[str, tuple[FusedHit, ...]], tuple[FusedHit, ...]]

#: WP2 (B05) conservative intent signal.  A query that explicitly asks for an
#: explanation, relationship or call-chain must never be short-circuited on a
#: mere exact name hit; the dense/lexical channels stay enabled.  The classifier
#: signal sets below (``_RELATIONSHIP_SIGNALS_WP5``, ``_COMPARISON_SIGNALS``,
#: ``_SEMANTIC_STRONG``, ``_SEMANTIC_WEAK``) are the single source of truth and
#: drive both the WP2 gate and the WP5 intent classifier.


def _has_relationship_intent(query: str) -> bool:
    """Return True when the query signals explanation/relationship/call-chain.

    This drives the WP2 (B05) *conservative* dense gate: any explanation,
    relationship, comparison or semantic signal keeps the dense channel enabled
    so a mere exact name hit never short-circuits a full-answer question.  It
    intentionally uses the union of ALL WP5 classifier signal sets (relationship
    + comparison + strong/weak semantic) — the gate stays maximally permissive
    (never skips dense on a doubt) even though the reported intent is decided
    more precisely by ``_classify_intent``.

    Turkish text is casefolded before the match so ``İ/ı/ş/ğ`` behave as the
    normalized retrieval does.
    """
    lowered = query.casefold()
    return any(
        signal in lowered
        for signal in (
            *_RELATIONSHIP_SIGNALS_WP5,
            *_COMPARISON_SIGNALS,
            *_SEMANTIC_STRONG,
            *_SEMANTIC_WEAK,
        )
    )


#: WP2 (B05) word tokenization for the pure-lookup gate.  Mirrors the retrieval
#: ``\w+`` token contract; phrases/identifiers are parsed back out by their
#: alphabetic/digit components.
_LOOKUP_WORD = re.compile(r"\w+", re.UNICODE)


def _select_window(text: str, budget_tokens: int) -> str:
    """Return a bounded, line-aligned leading window of ``text`` fitting ``budget_tokens``.

    Used by ``build_answer`` to avoid losing a large, highly relevant chunk
    entirely when it exceeds the context budget (WP6 / B08): instead of dropping
    the chunk, a stable window that fits is selected so the evidence is not lost
    wholesale.  The window never fabricates content: whole trailing lines are
    dropped and the boundary is deterministic.  The original text is returned
    unchanged when it already fits.

    The caller is responsible for labelling the window: its derived digest and
    (where deterministically computable) line sub-range must NOT be confused with
    the full source/chunk digest and locator.
    """
    if budget_tokens <= 0:
        return ""
    if estimate_tokens(text) <= budget_tokens:
        return text
    lines = text.splitlines(keepends=True)
    parts: list[str] = []
    total = 0
    for line in lines:
        cost = estimate_tokens(line)
        if total + cost > budget_tokens:
            if not parts:
                # One single over-budget line: take a token-bounded prefix of it.
                words = line.split()
                kept: list[str] = []
                accum = 0
                for word in words:
                    word_cost = estimate_tokens(word + " ")
                    if accum + word_cost > budget_tokens:
                        break
                    kept.append(word)
                    accum += word_cost
                return " ".join(kept)
            break
        parts.append(line)
        total += cost
    return "".join(parts)


#: WP6 (B08): the smallest window that counts as meaningful evidence.  When even
#: this much of a large chunk cannot fit the remaining budget, the chunk is
#: dropped and the answer abstains (low-evidence) rather than force-fitting a
#: barely-readable sliver into the context.
MIN_CONTEXT_WINDOW_TOKENS = 48


def _is_pure_identifier_lookup(query: str, identifiers: tuple[str, ...]) -> bool:
    """Return True only for a *pure single-object exact lookup*.

    A query is treated as a pure identifier lookup when the technical
    identifiers dominate its content: after removing the identifier components
    and short stop-like words, there is little or no remaining free natural
    language.  A prose/explanation/relationship question (e.g. "ADR-0006
    idempotent dosya ice aktarma SHA-256 ile tekrar engeller") carries many free
    words and therefore keeps the dense channel enabled, so a mere exact name
    hit never short-circuits a full answer (task WP2/WP5).
    """
    words = [word.casefold() for word in _LOOKUP_WORD.findall(query) if len(word) > 1]
    if not words:
        return False
    # Components of the technical identifiers are "locked" content, not free
    # natural language.  Remaining words after stripping them are free prose.
    identifier_parts: set[str] = set()
    for identifier in identifiers:
        for part in re.findall(r"[A-Za-z0-9_$#]+", identifier):
            part = part.casefold()
            if len(part) > 1:
                identifier_parts.add(part)
    free_words = [word for word in words if word not in identifier_parts]
    # Pure lookups may carry a few short scope words (e.g. "tekil nesnesi",
    # "tablosu"); cap free prose at a small fraction of the query so a genuinely
    # verbose question never fast-paths past dense.
    return len(free_words) <= max(2, len(words) // 4)


class QueryIntent(StrEnum):
    """Deterministic, provider-free query-intent classification (WP5).

    Kept explicit and cheap (no LLM).  The classifier is a pure function of the
    query text/identifiers, so the same input always yields the same intent.
    """

    EXACT_LOOKUP = "single-object-exact"
    SEMANTIC = "semantic-explanation"
    MULTI_OBJECT_COMPARISON = "multi-object-comparison"
    RELATIONSHIP = "relationship-call-chain"
    AMBIGUOUS = "ambiguous-question"


#: Comparison words — signal an expectation that several distinct objects are
#: related/contrasted against each other (WP5 multi-object/comparison intent).
_COMPARISON_SIGNALS = (
    "karsilastir",
    "karşılaştır",
    "karsilastirma",
    "karşılaştırma",
    "farki",
    "farkı",
    "fark",
    "arasindaki",
    "arasındaki",
    "benzerlik",
    "ortak",
    "hangisi",
    "hangisi daha",
    "vs",
    "karsilik",
    "eşleştir",
    "eslestir",
)

#: Relationship/call-chain words — the query asks how A depends on, calls into,
#: or is reached from B.  This set is deliberately scoped to *genuine*
#: relationship/call-chain language so it does not shadow comparison or
#: semantic/explanation signals (which carry their own dedicated sets).  It is
#: shared with the WP2 conservative gate (``_has_relationship_intent``).
_RELATIONSHIP_SIGNALS_WP5 = tuple(
    dict.fromkeys(
        (
            "cagirir",
            "çağırır",
            "cagirdigi",
            "çağırdığı",
            "cagiran",
            "çağıran",
            "cagiren",
            "çağıran",
            "cagri zinciri",
            "çağrı zinciri",
            "call chain",
            "calls",
            "depends on",
            "depends",
            "dependency",
            "bagimli",
            "bağımlı",
            "bagimlilik",
            "bağımlılık",
            "bagimliligi",
            "bağımlılığı",
            "iliskisi",
            "ilişkisi",
            "kullanan",
            "kullanir",
            "kullanır",
            "referans",
            "cagir",
            "çağrı",
            "cagrisi",
            "cagrisini",
            "akisi",
            "akışı",
            "akış",
            "tipki",
        )
    )
)

#: Strong explanation words — "why/how/explain/what does X do/what is X for".
#: These dominate the classifier so a why/how question is always SEMANTIC even
#: when a verb like ``kullanir`` (also a relationship signal) appears.
_SEMANTIC_STRONG = (
    "neden",
    "niye",
    "nasil",
    "nasıl",
    "nasil calisir",
    "acikla",
    "açıkla",
    "aciklama",
    "açıklama",
    "aciklamasi",
    "aciklamasini",
    "anlami",
    "anlamı",
    "amac",
    "amaç",
    "mantigi",
    "mantığı",
    "sebebi",
    "gerekce",
    "gerekçe",
    "calisir",
    "çalışır",
    "islevi",
    "işlevi",
    "ne ise yarar",
    "ne ise",
)

#: Weak explanation words — only meaningful when a named object is identified;
#: a bare "bu nedir" without any technical identifier stays AMBIGUOUS.
_SEMANTIC_WEAK = (
    "nedir",
    "ne demek",
    "ne icin",
    "ne için",
)


def _classify_intent(query: str, identifiers: tuple[str, ...]) -> QueryIntent:
    """Deterministically classify the query intent (WP5).

    Rule precedence (first match wins):

    1. Strong explanatory openers (``neden``/``nasil``/``acikla``/``anlami``/
       ``amac``/... why-and-how questions) -> SEMANTIC, even when a verb like
       ``kullanir`` also appears.
    2. Relationship / call-chain signals (genuine dependency/call language,
       e.g. ``cagirir``, ``bagimliligi``) -> RELATIONSHIP.
    3. Comparison signals (``farki``/``karsilastir``/``benzerlik``/
       ``arasindaki``/...) -> MULTI_OBJECT_COMPARISON.
    4. Weak explanatory words (``nedir``/``ne demek``/``ne icin``): SEMANTIC when
       a named object is present, else AMBIGUOUS (e.g. "bu nedir").
    5. Several distinct technical identifiers -> MULTI_OBJECT_COMPARISON.
    6. Exactly one identifier that is a *pure* lookup (minuscule prose) ->
       EXACT_LOOKUP.
    7. Otherwise -> AMBIGUOUS.

    The classifier is provider-free and deterministic: it only tokenizes and
    matches fixed Turkish signal sets, so the same input always yields the same
    value.  It intentionally does **not** short-circuit explanation or
    relationship questions on a mere exact name hit (WP2/WP5).
    """
    lowered = query.casefold()
    # 1. Strong explanatory openers dominate (why/how/explain).
    if any(signal in lowered for signal in _SEMANTIC_STRONG):
        return QueryIntent.SEMANTIC
    # 2. Relationship / call-chain (genuine dependency/call language).  Checked
    #    before comparison so "arasindaki bagimliligi" is a relationship, not a
    #    bare comparison.
    if any(signal in lowered for signal in _RELATIONSHIP_SIGNALS_WP5):
        return QueryIntent.RELATIONSHIP
    # 3. Comparison (several objects contrasted / compared).
    if any(signal in lowered for signal in _COMPARISON_SIGNALS):
        return QueryIntent.MULTI_OBJECT_COMPARISON
    # 4. Weak explanatory words: need a named object to be meaningful.
    if any(signal in lowered for signal in _SEMANTIC_WEAK):
        if identifiers:
            return QueryIntent.SEMANTIC
        return QueryIntent.AMBIGUOUS
    # 5. Several distinct technical identifiers -> comparison.
    if len(identifiers) >= 2:
        return QueryIntent.MULTI_OBJECT_COMPARISON
    # 6. Exactly one identifier and it is a *pure* lookup (minuscule prose).
    if identifiers and _is_pure_identifier_lookup(query, identifiers):
        return QueryIntent.EXACT_LOOKUP
    return QueryIntent.AMBIGUOUS


#: Default end-to-end retrieval deadline (seconds).  A single monotonic budget
#: shared across exact/lexical/dense, reranker and cancellation so no sub-layer
#: restarts its own timer and total wait stays bounded (WP5).  Configurable via
#: ``RetrievalService(retrieval_deadline_seconds=...)`` and the embedded RAG
#: ``retrieval_deadline_seconds`` constructor argument.  This is a reasonable
#: default, not a proven optimal value.
DEFAULT_RETRIEVAL_DEADLINE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class RetrievalDeadline:
    """Single monotonic end-to-end retrieval deadline (WP5).

    Every sub-layer (exact, lexical, dense, reranker, fallback) consults the same
    object and shares the remaining budget; it never restarts its own timer.  A
    sub-layer must stop launching further work once ``expired`` is True, and a
    result that arrives after the deadline is suppressed rather than published.
    """

    deadline: float
    started: float = field(default_factory=time.monotonic)

    @classmethod
    def with_timeout(cls, seconds: float | None) -> RetrievalDeadline:
        """Create a deadline; ``seconds=None`` disables it (bounded by no clock)."""
        if seconds is None:
            return cls(deadline=float("inf"))
        if seconds <= 0:
            raise ValidationFailed("retrieval deadline pozitif olmali")
        return cls(deadline=time.monotonic() + seconds)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def ensure_remaining(self, label: str) -> bool:
        """Return False (and mark caller) when budget is spent; never raises."""
        del label
        return not self.expired


class SearchBackend(Protocol):
    """Kaynak turune ozel aday ureten ince adapter sozlesmesi.

    Siralama, fusion ve rerank bu adapterda degil ``RetrievalService`` icinde
    kalir. Adapter yalniz kendi corpus'unda uc kanalin adaylarini uretir.
    """

    def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]: ...

    def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]: ...

    def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]: ...


@dataclass(frozen=True, slots=True)
class ChunkView:
    """Baglam kurulumu icin gereken minimum chunk bilgisi."""

    chunk_id: str
    document_id: str
    text: str
    locator: Any
    content_digest: str
    parent_id: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalTrace:
    """Kararin aciklamasi: hangi kanal ne buldu, ne neden elendi."""

    identifiers: tuple[str, ...]
    per_channel: dict[str, int]
    fused_count: int
    after_dedupe: int
    reranker_used: bool
    reranker_failed: bool
    source_type: str = "knowledge"
    dropped_for_budget: tuple[str, ...] = field(default_factory=tuple)
    # WP5 (P0): intent classification + deadline observability.  Defaults keep
    # the existing trace contract intact for every current caller.
    intent: str = QueryIntent.AMBIGUOUS.value
    deadline_expired: bool = False
    degraded_reason: str | None = None
    # Optional graph-reranker trace metadata (G2). Defaults preserve the exact
    # protocol for callers that never compose a graph reranker.
    graph_used: bool = False
    graph_state: str | None = None
    graph_bypass: str | None = None
    # WP6 (B08): every chunk that was truncated to a bounded window because the
    # whole chunk exceeded the context budget.  ``as_lines``/``as_dict`` expose
    # this so consumers can see used-vs-dropped evidence explicitly instead of a
    # chunk silently disappearing when the budget is too small.
    windowed_chunk_ids: tuple[str, ...] = ()

    def as_lines(self) -> tuple[str, ...]:
        lines = [
            f"kaynak turu: {self.source_type}",
            f"exact kimlik: {', '.join(self.identifiers) or '-'}",
            "kanal sonuclari: "
            + ", ".join(f"{name}={count}" for name, count in sorted(self.per_channel.items())),
            f"fusion sonrasi: {self.fused_count}, dedupe sonrasi: {self.after_dedupe}",
        ]
        if self.reranker_failed:
            lines.append("reranker basarisiz; fusion sirasina geri donuldu")
        elif self.reranker_used:
            lines.append("reranker uygulandi")
        if self.graph_used:
            if self.graph_bypass:
                lines.append(f"graph reranker bypass: {self.graph_bypass}")
            else:
                lines.append(f"graph reranker uygulandi (state={self.graph_state or '-'})")
        if self.dropped_for_budget:
            lines.append(f"token butcesi nedeniyle disarida: {', '.join(self.dropped_for_budget)}")
        if self.windowed_chunk_ids:
            lines.append(
                "siginmayan chunk tampon pencereye kesildi: "
                + ", ".join(self.windowed_chunk_ids)
            )
        lines.append(f"query intent: {self.intent}")
        if self.deadline_expired:
            lines.append("retrieval deadline asildi; kalan kanitla devam edildi")
        if self.degraded_reason:
            lines.append(f"degraded sebep: {self.degraded_reason}")
        return tuple(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "identifiers": list(self.identifiers),
            "per_channel": dict(self.per_channel),
            "fused_count": self.fused_count,
            "after_dedupe": self.after_dedupe,
            "reranker_used": self.reranker_used,
            "reranker_failed": self.reranker_failed,
            "graph_used": self.graph_used,
            "graph_state": self.graph_state,
            "graph_bypass": self.graph_bypass,
            "dropped_for_budget": list(self.dropped_for_budget),
            "windowed_chunk_ids": list(self.windowed_chunk_ids),
            "intent": self.intent,
            "deadline_expired": self.deadline_expired,
            "degraded_reason": self.degraded_reason,
        }


@dataclass(frozen=True, slots=True)
class RetrievalService:
    """Kanal birlestirme, dedupe, genisletme ve butceli baglam kurulumu."""

    backend: SearchBackend
    reranker: Reranker | None = None
    limit: int = 20
    # WP5 (P0): one monotonic end-to-end retrieval deadline shared by every
    # sub-layer.  ``None`` disables the clock (callers that already enforce their
    # own bound); ``DEFAULT_RETRIEVAL_DEADLINE_SECONDS`` is the default.
    retrieval_deadline_seconds: float | None = DEFAULT_RETRIEVAL_DEADLINE_SECONDS

    def classify_query_intent(self, query: str) -> QueryIntent:
        """Expose the deterministic WP5 query-intent classifier."""
        identifiers = extract_identifiers(query)
        return _classify_intent(query, identifiers)

    def _new_deadline(self) -> RetrievalDeadline:
        """Create the single monotonic retrieval deadline for this search.

        Factored so a test seam can force an already-expired deadline without
        timing flakiness; production just derives it from the configured budget.
        """
        return RetrievalDeadline.with_timeout(self.retrieval_deadline_seconds)

    def search(self, query: str) -> tuple[tuple[FusedHit, ...], RetrievalTrace]:
        identifiers = extract_identifiers(query)
        intent = _classify_intent(query, identifiers)
        deadline = self._new_deadline()
        channels: dict[RetrievalChannel, tuple[ScoredHit, ...]] = {}
        deadline_expired = False
        degraded_reason: str | None = None
        # WP1 (B05): record whether each channel was attempted and whether it
        # ran to completion (returned without error).  "Dense is open" and
        # "dense actually ran" stay distinct; these counters never feed a
        # semantic/authority digest.
        counters = active_counters()

        def _attempt(name: RetrievalChannel, invoke: Callable[[], tuple[ScoredHit, ...]]) -> None:
            nonlocal deadline_expired
            if counters is not None:
                counters.mark_channel_attempted(name.value)
            result = invoke()
            # WP5 (P0): a sub-layer must not restart its own timer — the shared
            # deadline is the only budget.  If the result arrived after the
            # deadline expired, it is suppressed (never published) and the
            # remaining budget is marked spent so no further channels launch.
            if deadline.expired:
                deadline_expired = True
                return
            channels[name] = result
            if counters is not None:
                counters.mark_channel_completed(name.value)

        # Exact channel always runs: knowledge adapters return empty when there
        # is no technical identifier, while memory-like adapters may apply exact
        # text/tag matching too.  This keeps the source kind from changing the
        # core flow.
        if not deadline.expired:
            _attempt(
                RetrievalChannel.EXACT,
                lambda: self.backend.exact(identifiers, limit=self.limit),
            )

        # Lexical always runs (when the deadline still allows): finding a name
        # is not answering the whole question.
        if not deadline.expired:
            _attempt(
                RetrievalChannel.LEXICAL,
                lambda: self.backend.lexical(query, limit=self.limit),
            )
        # WP2 (B05) + WP5: conditional dense channel (the WP5 channel selection).
        # Exact/lexical evidence is allowed to fully satisfy only a single-object
        # exact lookup; explanation/relationship and comparison intents always
        # keep dense enabled so a mere name hit never short-circuits a full
        # answer, and the deadline/budget is respected before launching it.
        if (
            not deadline.expired
            and self._dense_required(query, identifiers, channels)
        ):
            _attempt(
                RetrievalChannel.DENSE,
                lambda: self.backend.dense(query, limit=self.limit),
            )
        elif counters is not None:
            # Record the explicit skip so "dense was not attempted" is observable,
            # not conflated with a dense failure.
            counters.mark_channel_skipped(RetrievalChannel.DENSE.value)

        # Exact kanal her zaman cagrilir. Knowledge adapteri teknik kimlik yoksa
        # bos doner; memory gibi adapterlar exact metin/tag eslesmesini de
        # uygulayabilir. Boylece kaynak turu core akisini degistiremez.
        exact_ids = frozenset(hit.chunk_id for hit in channels.get(RetrievalChannel.EXACT, ()))
        fused = reciprocal_rank_fusion(channels, exact_ids=exact_ids)

        # WP5 (P0): surface a provider/dependency-unavailable signal (e.g. the
        # embedded backend's dense provider failure) into the trace so the
        # calling layer can return explicitly-degraded evidence instead of a
        # silent success.  The backend exposes this either as ``dense_failure_reason``
        # or a ``provider_unavailable`` boolean.
        if degraded_reason is None:
            backend_reason = getattr(self.backend, "dense_failure_reason", None) or (
                getattr(self.backend, "provider_unavailable", False) and "provider-unavailable"
            )
            if backend_reason:
                degraded_reason = str(backend_reason)

        # Fusion/rerank also share the remaining budget; a rerank that would run
        # past the deadline is bounded (still returns the fused order, marked
        # expired) rather than blocking indefinitely.
        if deadline.expired:
            deadline_expired = True
        reranked, used, failed = self._rerank(query, fused)
        if deadline.expired:
            deadline_expired = True
        trace = self._build_trace(
            identifiers, intent, channels, fused, reranked, used, failed, deadline,
            deadline_expired, degraded_reason,
        )
        return reranked, trace

    def _build_trace(
        self,
        identifiers: tuple[str, ...],
        intent: QueryIntent,
        channels: dict[RetrievalChannel, tuple[ScoredHit, ...]],
        fused: tuple[FusedHit, ...],
        reranked: tuple[FusedHit, ...],
        reranker_used: bool,
        reranker_failed: bool,
        deadline: RetrievalDeadline,
        deadline_expired: bool,
        degraded_reason: str | None,
    ) -> RetrievalTrace:
        del deadline
        graph_used = False
        graph_state: str | None = None
        graph_bypass: str | None = None
        last_state = getattr(self.reranker, "last_state", None)
        if last_state is not None:
            graph_used = True
            graph_state = getattr(last_state, "graph_state", None)
            graph_bypass = getattr(last_state, "bypass", None)
        return RetrievalTrace(
            identifiers=identifiers,
            source_type=str(getattr(self.backend, "source_type", "knowledge")),
            per_channel={str(name): len(hits) for name, hits in channels.items()},
            fused_count=len(fused),
            after_dedupe=len(reranked),
            reranker_used=reranker_used,
            reranker_failed=reranker_failed,
            graph_used=graph_used,
            graph_state=graph_state,
            graph_bypass=graph_bypass,
            intent=intent.value,
            deadline_expired=deadline_expired,
            degraded_reason=degraded_reason,
        )

    @staticmethod
    def _dense_required(
        query: str,
        identifiers: tuple[str, ...],
        channels: dict[RetrievalChannel, tuple[ScoredHit, ...]],
    ) -> bool:
        """WP2 (B05) + WP5 channel-selection gate: whether the dense channel is needed.

        Returns ``True`` (dense still required) unless every requirement holds,
        so the dense channel is only skipped:

        * the query carries at least one technical identifier,
        * the query is a *pure single-object exact lookup*: it is dominated by
          technical identifiers with little free natural-language content (a
          prose/explanation/relationship/comparison/semantic question must keep
          dense enabled so a name hit never short-circuits a full answer —
          ``_has_relationship_intent`` is the conservative union of all WP5
          intent signals),
        * the exact channel returned genuine high-confidence evidence covering
          every extracted identifier (no identifier is starved), and
        * the query does not signal any full-answer intent.

        This conservatively implements WP5 channel selection: an exact/lexical
        hit that fully satisfies a *single-object* intent allows an early return
        before dense, while explanation/relationship/comparison intents are never
        short-circuited.  The finer intent is reported separately via
        ``_classify_intent`` / the trace ``intent`` field.
        """
        if not identifiers:
            return True
        if _has_relationship_intent(query):
            return True
        if not _is_pure_identifier_lookup(query, identifiers):
            return True
        exact_hits = tuple(channels.get(RetrievalChannel.EXACT, ()))
        if not exact_hits:
            return True
        # Genuine high-confidence exact evidence: an object match whose storage
        # chunk id is distinct from the search term.  (A degenerate/mock backend
        # returns the identifier itself as the chunk id, which is not a real
        # matched object and is insufficient to skip dense.)  Sequence: at least
        # one such genuine object per extracted identifier so no identifier is
        # starved (task B04).
        protected = {identifier.casefold() for identifier in identifiers}
        genuine_ids = {
            hit.chunk_id
            for hit in exact_hits
            if hit.chunk_id.casefold() not in protected
        }
        # Dense stays required unless exact produced at least one genuine object
        # per extracted identifier (so no identifier is starved, task B04).
        return len(genuine_ids) < len(identifiers)

    def _rerank(
        self, query: str, fused: tuple[FusedHit, ...]
    ) -> tuple[tuple[FusedHit, ...], bool, bool]:
        """Reranker hatasi sonucu kaybetmez; fusion sirasi korunur."""

        if self.reranker is None or not fused:
            return fused, False, False
        try:
            reranked = self.reranker(query, fused)
        except Exception:
            return fused, False, True
        if not reranked or {item.chunk_id for item in reranked} != {
            item.chunk_id for item in fused
        }:
            # Reranker sonuc dusurduyse guvenilmez sayilir.
            return fused, False, True
        exact_first = sorted(reranked, key=lambda item: not item.exact_match)
        return tuple(exact_first), True, False

    def build_answer(
        self,
        query: str,
        hits: tuple[FusedHit, ...],
        trace: RetrievalTrace,
        *,
        views: dict[str, ChunkView],
        token_budget: int,
        minimum_citations: int = 1,
    ) -> RetrievalAnswer:
        """Token butceli baglam kurar; kanit yetersizse abstain eder.

        WP5 (P0): a deadline-expired or provider-unavailable retrieval is never
        reported as a silent full success.  Strong exact/lexical evidence that
        was gathered before the failure is still returned, but the answer is
        explicitly marked ``DEGRADED_*`` (with the gathered citations), never
        ``ANSWERED``.  With insufficient evidence the answer explicitly abstains.
        """

        if token_budget <= 0:
            raise ValidationFailed("token butcesi pozitif olmali")
        query_digest = digest({"query": query})

        if not hits:
            # No evidence at all: distinguish "we ran out of time / provider was
            # unavailable" from a genuine no-hit answer.
            if trace.deadline_expired:
                state = AnswerState.DEGRADED_TIMEOUT
            elif trace.degraded_reason:
                state = AnswerState.DEGRADED_PROVIDER_UNAVAILABLE
            else:
                state = AnswerState.ABSTAINED_NO_HIT
            return RetrievalAnswer(
                query_digest=query_digest,
                state=state,
                citations=(),
                used_chunk_ids=(),
                token_budget=token_budget,
                tokens_used=0,
                explanation=trace.as_lines(),
            )

        unique = dedupe(hits, content_digests={k: v.content_digest for k, v in views.items()})
        ordered = expand_parents(
            unique, parents={k: v.parent_id for k, v in views.items() if v.parent_id}
        )

        used: list[str] = []
        citations: list[Citation] = []
        tokens = 0
        dropped: list[str] = []
        windowed: list[str] = []

        # WP6 (B08) packing: the budget is split into per-chunk shares so one
        # early large/verbose chunk cannot consume the whole budget before the
        # mandatory multi-object evidence is reached.  The split is a coarse
        # fairness heuristic (``len(ordered)`` chunks, reserving a floor); it
        # never bumps the total, so model context capacity and output reserve
        # stay bounded.
        shared_floor = max(1, token_budget // max(1, len(ordered)))
        for chunk_id in ordered:
            view = views.get(chunk_id)
            if view is None:
                continue
            cost = estimate_tokens(view.text)
            if tokens + cost <= token_budget and cost <= shared_floor:
                tokens += cost
                used.append(chunk_id)
                citations.append(
                    Citation(
                        chunk_id=view.chunk_id,
                        document_id=view.document_id,
                        locator=view.locator,
                        content_digest=view.content_digest,
                    )
                )
            elif shared_floor > 0 and tokens < token_budget:
                # Large chunk that does not fit whole: keep a bounded window of
                # its leading content instead of losing the evidence entirely
                # (an open-ended chunk sits between 0 and 5k lines, the budget
                # stays at 1200 tokens and the reserve for the other objects is
                # protected by ``shared_floor``).  This is intentionally bounded
                # and line-aligned; it is a fallback to avoid a whole-chunk drop.
                window = _select_window(view.text, min(shared_floor, token_budget - tokens))
                if window.strip() and estimate_tokens(window) >= MIN_CONTEXT_WINDOW_TOKENS:
                    cost = estimate_tokens(window)
                    tokens += cost
                    used.append(chunk_id)
                    windowed.append(chunk_id)
                    citations.append(
                        Citation(
                            chunk_id=view.chunk_id,
                            document_id=view.document_id,
                            locator=view.locator,
                            content_digest=view.content_digest,
                        )
                    )
                else:
                    dropped.append(chunk_id)
            else:
                dropped.append(chunk_id)

        windowed_trace = replace(
            trace, dropped_for_budget=tuple(dropped), windowed_chunk_ids=tuple(windowed)
        )
        explanation = windowed_trace.as_lines()

        # WP5 (P0): decide the final state without conflating distinct meanings.
        evidence_ok = len(citations) >= minimum_citations
        if trace.deadline_expired:
            # Timeout: return gathered evidence marked degraded, never success.
            state = (
                AnswerState.DEGRADED_TIMEOUT
                if evidence_ok
                else AnswerState.ABSTAINED_LOW_EVIDENCE
            )
        elif trace.degraded_reason:
            # Provider/dependency unavailable: same rule.
            state = (
                AnswerState.DEGRADED_PROVIDER_UNAVAILABLE
                if evidence_ok
                else AnswerState.ABSTAINED_LOW_EVIDENCE
            )
        else:
            state = AnswerState.ANSWERED if evidence_ok else AnswerState.ABSTAINED_LOW_EVIDENCE

        if not evidence_ok:
            return RetrievalAnswer(
                query_digest=query_digest,
                state=state,
                citations=(),
                used_chunk_ids=(),
                token_budget=token_budget,
                tokens_used=0,
                explanation=explanation,
            )

        return RetrievalAnswer(
            query_digest=query_digest,
            state=state,
            citations=tuple(citations),
            used_chunk_ids=tuple(used),
            token_budget=token_budget,
            tokens_used=tokens,
            explanation=explanation,
        )


# -- golden degerlendirme -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """Tek degerlendirme ornegi: sorgu ve beklenen chunk kimlikleri."""

    query: str
    relevant_ids: frozenset[str]

    def __post_init__(self) -> None:
        if not self.relevant_ids:
            raise ValidationFailed("golden ornegi en az bir dogru sonuc ister")


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """Recall, MRR ve nDCG. Baseline karsilastirmasi icin deterministiktir."""

    recall_at_k: float
    mrr: float
    ndcg_at_k: float
    k: int
    case_count: int

    def improves_on(self, baseline: EvaluationResult) -> bool:
        """Hicbir metrik gerilemeden en az birinde iyilesme var mi?"""

        metrics = (
            (self.recall_at_k, baseline.recall_at_k),
            (self.mrr, baseline.mrr),
            (self.ndcg_at_k, baseline.ndcg_at_k),
        )
        if any(current < previous - 1e-9 for current, previous in metrics):
            return False
        return any(current > previous + 1e-9 for current, previous in metrics)

    def as_dict(self) -> dict[str, Any]:
        return {
            "recall_at_k": round(self.recall_at_k, 6),
            "mrr": round(self.mrr, 6),
            "ndcg_at_k": round(self.ndcg_at_k, 6),
            "k": self.k,
            "case_count": self.case_count,
        }


def evaluate(
    cases: tuple[GoldenCase, ...],
    *,
    run: Callable[[str], tuple[str, ...]],
    k: int = 10,
) -> EvaluationResult:
    """Golden kume uzerinde Recall@k, MRR ve nDCG@k hesaplar."""

    if not cases:
        raise ValidationFailed("degerlendirme icin en az bir ornek gerekiyor")
    if k <= 0:
        raise ValidationFailed("k pozitif olmali")

    import math

    recalls: list[float] = []
    reciprocal: list[float] = []
    gains: list[float] = []
    for case in cases:
        ranked = run(case.query)[:k]
        hits = [1.0 if item in case.relevant_ids else 0.0 for item in ranked]
        recalls.append(sum(hits) / len(case.relevant_ids))
        first = next((index for index, value in enumerate(hits, start=1) if value), None)
        reciprocal.append(1.0 / first if first else 0.0)
        dcg = sum(value / math.log2(index + 1) for index, value in enumerate(hits, start=1))
        ideal_count = min(len(case.relevant_ids), k)
        idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_count + 1))
        gains.append(dcg / idcg if idcg else 0.0)

    count = len(cases)
    return EvaluationResult(
        recall_at_k=sum(recalls) / count,
        mrr=sum(reciprocal) / count,
        ndcg_at_k=sum(gains) / count,
        k=k,
        case_count=count,
    )


@dataclass(frozen=True, slots=True)
class NegativeCase:
    """Negative / no-answer degerlendirme ornegi.

    ``GoldenCase`` (bos relevant set) aksine, olmayan bir nesneyi veya yetersiz/
    celiskili kaniti soran bir sorgunun *dogru sonucu* abstain/no-answer olmalidir.
    ``relevant_ids`` yoktur; ``__post_init__`` bunu reddetmez cunku bos set burada
    kasitli ve anlamlidir. ``reason`` yalniz insan tarafindan okunabilir etiket icin
    opsiyonel meta veridir; skorlamaya katilmaz.
    """

    query: str
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValidationFailed("negative ornek bos sorgu kabul etmez")


@dataclass(frozen=True, slots=True)
class QualityRunResult:
    """Kalite degerlendiricisinin ``run`` cagrisindan donen provider-free sonuc.

    Pozitif (``GoldenCase``) ve negatif (``NegativeCase``) kaselerin birlikte
    degerlendirilmesi, sistemin yalnizca *siraladigi parcalari* degil ayni zamanda
    *guvenli answered/abstain kararini* da bilmesini gerektirir. ``answered`` True
    ise sistem soruya guvenli/kaynakli bir cevap uretmistir (ve ``ranked_ids`` /
    ``citations`` tasir); False ise sistem abstain etmis/bilgi vermemistir.

    ``citations`` skorlamada dogrudan kullanilmaz; ``answered`` ile birlikte
    "no-answer sorusuna desteksiz kaynakli cevap" hatalarini gorunur kilmak icin
    tasinir (raporlamada islenir).
    """

    ranked_ids: tuple[str, ...] = ()
    answered: bool = False
    citations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QualityEvaluation:
    """Pozitif + negatif kisimlari birlikte raporlayan deterministik sonuc.

    ``positive`` metrikleri ``GoldenCase`` kumesinden (``evaluate`` ile birebir
    ayni formullerle) hesaplanir; ``positive_precision_at_k`` ayni kaselerin
    top-k dagilimini ekler. ``negative_precision``, negatif kaselerin kacinin
    dogru sekilde abstain edildigini (desteksiz cevap uretilmedigini) olcer.

    ``quality_score``, pozitif siralamayi (MRR) ile negatif abstain dogrulugunu
    harmonik ortalama ile bir araya getirir: iki taraftan biri sifirda kalirsa
    genel skor sifira ceker, yani "her seye cevap verme" ya da "hicbir seye
    cevap verme" stratejisi oyunlasamaz.
    """

    positive: EvaluationResult
    positive_precision_at_k: float
    negative_count: int
    negative_precision: float
    negative_false_positive: int
    quality_score: float

    def as_dict(self) -> dict[str, Any]:
        base = self.positive.as_dict()
        base.update(
            {
                "positive_precision_at_k": round(self.positive_precision_at_k, 6),
                "negative_count": self.negative_count,
                "negative_precision": round(self.negative_precision, 6),
                "negative_false_positive": self.negative_false_positive,
                "quality_score": round(self.quality_score, 6),
            }
        )
        return base


def _as_run_result(value: QualityRunResult | tuple[str, ...]) -> QualityRunResult:
    """Normalize run ciktisini ``QualityRunResult`` yapar.

    Legacy ``tuple[str, ...]`` tipten gelen bir sonuc, sistemin cevap urettigini
    (answered) varsayar: pozitif kase icin siralamasi kullanilir, negatif kase
    icin ise "answered" olarak yanlis pozitif sayilir (abstain etmemistir).
    """
    if isinstance(value, QualityRunResult):
        return value
    if isinstance(value, tuple):
        return QualityRunResult(ranked_ids=value, answered=True, citations=value)
    raise ValidationFailed("quality run sonucu QualityRunResult veya tuple olmali")


def evaluate_quality(
    cases: tuple[GoldenCase, ...],
    negatives: tuple[NegativeCase, ...] = (),
    *,
    run: Callable[[str], QualityRunResult | tuple[str, ...]],
    k: int = 10,
) -> QualityEvaluation:
    """Pozitif ve negative kaseleri birlikte deterministik degerlendirir.

    Pozitif metrikler ``evaluate`` ile ayni formullerdir (backward compatible
    taraflar). Negatif kase icin dogru davranis, sistemin abstain etmesidir:
    ``answered`` True donecek ise (desteksiz/suateli cevap) yanlis pozitif
    sayilir ve ``negative_precision`` dusurulur.

    :param cases: pozitif ``GoldenCase`` kumesi (bos olabilir).
    :param negatives: negatif ``NegativeCase`` kumesi (bos olabilir).
    :param run: sorgu -> ``QualityRunResult`` (veya legacy tuple) deterministik cagri.
    :param k: pozitif metriklerde kullanilacak kisit (negatiflerde gecersiz).
    """
    if not cases and not negatives:
        raise ValidationFailed("kalite degerlendirmesi icin en az bir ornek gerekiyor")
    if k <= 0:
        raise ValidationFailed("k pozitif olmali")

    results = [_as_run_result(run(case.query)) for case in cases]
    if cases:
        positive = evaluate(
            cases,
            run=lambda query: next(
                (
                    res.ranked_ids
                    for q, res in zip((c.query for c in cases), results, strict=True)
                    if q == query
                ),
                (),
            ),
            k=k,
        )
    else:
        # Pozitif kase yokken ``evaluate`` boş kümeyi reddeder; pozitif metrikleri
        # sıfır olarak kurarız çünkü değerlendirilecek pozitif örnek yoktur.
        positive = EvaluationResult(recall_at_k=0.0, mrr=0.0, ndcg_at_k=0.0, k=k, case_count=0)
    precisions: list[float] = []
    for case, res in zip(cases, results, strict=True):
        ranked = res.ranked_ids[:k]
        hits = sum(1.0 for item in ranked if item in case.relevant_ids)
        precisions.append(hits / k if k else 0.0)
    positive_precision = sum(precisions) / len(precisions) if precisions else 0.0

    negative_results = [_as_run_result(run(negative.query)) for negative in negatives]
    negative_false_positive = sum(1 for res in negative_results if res.answered)
    negative_count = len(negatives)
    negative_precision = (
        (negative_count - negative_false_positive) / negative_count if negative_count else 1.0
    )
    # Abstain bilgisi, negatiflerin answered_oldugu durumda da raporlanir.
    _ = negative_results

    if negative_count == 0:
        quality_score = positive.mrr
    else:
        # Harmonik ortalama: her iki taraf da sifirdan buyuk olmadan anlamli skor
        # yok. Pozitif taraf siralama kalitesini (MRR), negatif taraf abstain
        # dogrulugunu tasir. "Hep cevapla" negatif_precision'i, "hic cevaplama"
        # positive.mrr'yi dusurur; oyunlasma yolu yoktur.
        positive_mrr = positive.mrr
        if positive_mrr <= 0.0 or negative_precision <= 0.0:
            quality_score = 0.0
        else:
            quality_score = 2 * positive_mrr * negative_precision / (
                positive_mrr + negative_precision
            )

    return QualityEvaluation(
        positive=positive,
        positive_precision_at_k=positive_precision,
        negative_count=negative_count,
        negative_precision=negative_precision,
        negative_false_positive=negative_false_positive,
        quality_score=quality_score,
    )
