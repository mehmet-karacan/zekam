"""Bounded exact/lexical candidates, never semantic search or source verification."""

from __future__ import annotations

import math
import re
from dataclasses import asdict
from typing import Any

from zekam.application.context_ranking import count_context_tokens
from zekam.application.knowledge_file_plane import validate_portable_relative
from zekam.application.knowledge_index import KnowledgeGeneration, KnowledgeIndexPort
from zekam.application.local_continuity import bounded_int, logical
from zekam.application.retrieval_service import ChunkView
from zekam.domain.canonical import digest, digest_of_bytes, parse_digest
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed
from zekam.domain.knowledge import Locator
from zekam.domain.retrieval import (
    RetrievalChannel,
    ScoredHit,
    dedupe,
    extract_identifiers,
    reciprocal_rank_fusion,
)

MAX_QUERY_BYTES = 16384
CHANNEL_LIMIT = 16
LEXICAL_COVERAGE_THRESHOLD = 0.5
_TOKEN = re.compile(r"\w+", re.UNICODE)
_IDENTITY_KEYS = {"source_id", "source_revision", "source_ref", "source_digest", "content_digest"}


def _tokens(text: str) -> frozenset[str]:
    return frozenset(item.casefold() for item in _TOKEN.findall(text) if len(item) > 1)


def _generation(value: object, project_id: str) -> KnowledgeGeneration:
    if not isinstance(value, KnowledgeGeneration):
        raise PolicyViolation("Startup retrieval typed generation required")
    value.__post_init__()
    if value.project_id != project_id:
        raise PolicyViolation("Startup retrieval generation crossed project scope")
    logical(value.source_revision, "Index source revision")
    if type(value.chunk_count) is not int or value.chunk_count < 0:
        raise PolicyViolation("Startup retrieval generation count malformed")
    return value


def _hits(value: object, channel: RetrievalChannel) -> tuple[ScoredHit, ...]:
    if not isinstance(value, tuple) or len(value) > CHANNEL_LIMIT:
        raise PolicyViolation("Startup retrieval channel must be a bounded tuple")
    seen = set()
    for rank, item in enumerate(value, 1):
        if (
            not isinstance(item, ScoredHit)
            or item.channel is not channel
            or type(item.rank) is not int
            or item.rank != rank
            or type(item.raw_score) is not float
            or not math.isfinite(item.raw_score)
        ):
            raise PolicyViolation("Startup retrieval malformed channel/rank/score")
        logical(item.chunk_id, "Index chunk")
        if item.chunk_id in seen:
            raise PolicyViolation("Startup retrieval duplicate channel chunk")
        seen.add(item.chunk_id)
    return value


def _view(value: object, chunk_id: str, generation: KnowledgeGeneration) -> ChunkView:
    if not isinstance(value, ChunkView) or value.chunk_id != chunk_id:
        raise PolicyViolation("Startup retrieval missing or misbound chunk view")
    if value.document_id != f"{generation.generation_digest}:{generation.source_revision}":
        raise PolicyViolation("Startup retrieval view generation identity drift")
    if (
        not isinstance(value.text, str)
        or not value.text.strip()
        or len(value.text.encode("utf-8")) > 512 * 1024
        or value.content_digest != digest_of_bytes(value.text.encode("utf-8"))
    ):
        raise PolicyViolation("Startup retrieval view content integrity drift")
    locator = value.locator
    if not isinstance(locator, Locator):
        raise PolicyViolation("Startup retrieval exact typed locator required")
    locator.__post_init__()
    if (
        not isinstance(locator.relative_path, str)
        or type(locator.line_start) is not int
        or type(locator.line_end) is not int
        or locator.line_start < 1
        or locator.line_end < locator.line_start
        or locator.entry_path is not None
    ):
        raise PolicyViolation("Startup retrieval requires an exact portable line locator")
    validate_portable_relative(locator.relative_path)
    return value


def _identity(value: object, view: ChunkView, generation: KnowledgeGeneration) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != _IDENTITY_KEYS
        or any(not isinstance(item, str) for item in value.values())
    ):
        raise PolicyViolation("Startup retrieval source identity malformed")
    for field in ("source_id", "source_digest", "content_digest"):
        parse_digest(value[field])
    if (
        value["source_ref"] != view.locator.relative_path
        or value["source_revision"] != generation.source_revision
        or value["content_digest"] != view.content_digest
        or value["source_id"]
        != digest(
            {
                "schema": "zekam-project-source-identity/v1",
                "project_id": generation.project_id,
                "source_ref": value["source_ref"],
            }
        )
    ):
        raise PolicyViolation("Startup retrieval source/locator/scope identity drift")
    return value


class LocalStartupRetrieval:
    """Index-only candidates: a trusted resolver must verify original bytes later.

    There is intentionally no provider parameter, health probe, dense search,
    generated answer, persistence operation or implicit index rebuild.
    """

    def __init__(self, index: KnowledgeIndexPort) -> None:
        self.index = index

    def verify_fragment(
        self,
        *,
        project_id: str,
        expected_source_revision: str,
        expected_tree_digest: str,
        generation_digest: str,
        chunk_id: str,
    ) -> dict[str, Any]:
        """Recheck a persisted index pin; original source bytes still need a resolver.

        Unlike a new query, a persisted pin cannot silently degrade or select a
        replacement generation. Unavailable or changed evidence is an error.
        """
        logical(project_id, "Retrieval project")
        logical(expected_source_revision, "Expected source revision")
        logical(chunk_id, "Index chunk")
        for value in (expected_tree_digest, generation_digest):
            if not isinstance(value, str):
                raise ValidationFailed("Startup retrieval exact digest required")
            parse_digest(value)
        generation = _generation(self.index.generation(project_id), project_id)
        if (
            generation.state != "ready"
            or generation.chunk_count == 0
            or generation.source_revision != expected_source_revision
            or generation.tree_digest != expected_tree_digest
            or generation.generation_digest != generation_digest
        ):
            raise PolicyViolation("Startup retrieval persisted generation pin drift")
        pinned = {"generation_digest": generation_digest}
        raw_views = self.index.views(project_id, (chunk_id,), **pinned)
        if not isinstance(raw_views, dict) or set(raw_views) != {chunk_id}:
            raise PolicyViolation("Startup retrieval exact view partition missing or foreign")
        view = _view(raw_views[chunk_id], chunk_id, generation)
        identity = _identity(
            self.index.source_identity(project_id, chunk_id, **pinned), view, generation
        )
        if _generation(self.index.generation(project_id), project_id) != generation:
            raise PolicyViolation("Startup retrieval persisted generation changed during read")
        return {
            "chunk_id": chunk_id,
            "document_id": view.document_id,
            "text": view.text,
            "locator": view.locator.as_dict(),
            **identity,
            "generation_digest": generation_digest,
        }

    def query(
        self,
        query: str,
        *,
        project_id: str,
        expected_source_revision: str,
        expected_tree_digest: str,
        limit: int = 8,
        token_budget: int = 4096,
    ) -> dict[str, Any]:
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query.encode("utf-8")) > MAX_QUERY_BYTES
            or "\x00" in query
        ):
            raise ValidationFailed("Startup retrieval query must be bounded non-empty text")
        logical(project_id, "Retrieval project")
        logical(expected_source_revision, "Expected source revision")
        if not isinstance(expected_tree_digest, str):
            raise ValidationFailed("Startup retrieval tree digest required")
        parse_digest(expected_tree_digest)
        bounded_int(limit, maximum=8)
        bounded_int(token_budget, maximum=16384)
        result: dict[str, Any] = {
            "schema": "zekam-local-startup-retrieval/v1",
            "project_id": project_id,
            "query_digest": digest(query),
            "expected_source_revision": expected_source_revision,
            "expected_tree_digest": expected_tree_digest,
            "state": "abstained-insufficient-evidence",
            "reason": "insufficient-evidence",
            "generation": None,
            "searched_channels": [],
            "dense": "not-invoked",
            "provider_called": False,
            "resolver_required": True,
            "source_bytes_verified": False,
            "grants_authority": False,
            "answer_generated": False,
            "fragments": [],
            "token_count": 0,
            "token_budget": token_budget,
            "budget_scope": "fragment-text-utf8-bytes",
            "dropped_for_budget": [],
        }

        def finish(state: str, reason: str) -> dict[str, Any]:
            result.update(state=state, reason=reason)
            if state != "candidates-require-source-verification":
                result.update(fragments=[], token_count=0)
            return result | {"retrieval_digest": digest(result)}

        try:
            raw = self.index.generation(project_id)
        except NotFound:
            return finish("abstained-index-unavailable", "generation-unavailable")
        except ValidationFailed as exc:
            # The existing index port predates NotFound for this single condition.
            # Do not misreport corrupt metadata or any other validation as absence.
            if str(exc) != "Project current knowledge generation bulunamadi":
                raise
            return finish("abstained-index-unavailable", "generation-unavailable")
        except (TimeoutError, OSError):
            return finish("abstained-index-unavailable", "index-unavailable")
        generation = _generation(raw, project_id)
        result["generation"] = asdict(generation)
        if (
            generation.state != "ready"
            or generation.source_revision != expected_source_revision
            or generation.tree_digest != expected_tree_digest
            or generation.chunk_count == 0
        ):
            return finish("abstained-index-unavailable", "generation-stale-or-unready")
        pinned = {"generation_digest": generation.generation_digest}
        identifiers = extract_identifiers(query)
        try:
            exact = _hits(
                self.index.exact(project_id, identifiers, limit=CHANNEL_LIMIT, **pinned),
                RetrievalChannel.EXACT,
            )
            lexical = _hits(
                self.index.lexical(project_id, query, limit=CHANNEL_LIMIT, **pinned),
                RetrievalChannel.LEXICAL,
            )
            result["searched_channels"] = ["exact", "lexical"]
            fused = reciprocal_rank_fusion(
                {RetrievalChannel.EXACT: exact, RetrievalChannel.LEXICAL: lexical},
                exact_ids=frozenset(hit.chunk_id for hit in exact),
            )
            refs = tuple(hit.chunk_id for hit in fused)
            raw_views = self.index.views(project_id, refs, **pinned)
            if not isinstance(raw_views, dict) or set(raw_views) != set(refs):
                raise PolicyViolation("Startup retrieval exact view partition missing or foreign")
            views = {ref: _view(raw_views[ref], ref, generation) for ref in refs}
            identities = {
                ref: _identity(
                    self.index.source_identity(project_id, ref, **pinned), views[ref], generation
                )
                for ref in refs
            }
            for exact_hit in exact:
                view = views[exact_hit.chunk_id]
                if not any(
                    part in view.text or part in view.locator.relative_path for part in identifiers
                ):
                    raise PolicyViolation("Startup retrieval forged exact evidence")
            terms = _tokens(query)
            coverage = {
                ref: len(terms & _tokens(view.text)) / len(terms) if terms else 0.0
                for ref, view in views.items()
            }
            ordered = dedupe(
                fused, content_digests={ref: view.content_digest for ref, view in views.items()}
            )
            fragments: list[dict[str, Any]] = []
            spent = 0
            for hit in ordered:
                if not hit.exact_match and coverage[hit.chunk_id] < LEXICAL_COVERAGE_THRESHOLD:
                    continue
                view = views[hit.chunk_id]
                tokens = count_context_tokens(view.text)
                if spent + tokens > token_budget:
                    result["dropped_for_budget"].append(hit.chunk_id)
                    continue
                if len(fragments) == limit:
                    break
                fragments.append(
                    {
                        "chunk_id": hit.chunk_id,
                        "document_id": view.document_id,
                        "text": view.text,
                        "locator": view.locator.as_dict(),
                        **identities[hit.chunk_id],
                        "generation_digest": generation.generation_digest,
                        "channels": [str(channel) for channel in hit.channels],
                        "ranks": {
                            str(channel): next(
                                item.rank for item in items if item.chunk_id == hit.chunk_id
                            )
                            for channel, items in (
                                (RetrievalChannel.EXACT, exact),
                                (RetrievalChannel.LEXICAL, lexical),
                            )
                            if any(item.chunk_id == hit.chunk_id for item in items)
                        },
                        "rrf_score": hit.score,
                        "exact_match": hit.exact_match,
                        "lexical_coverage": coverage[hit.chunk_id],
                        "token_count": tokens,
                    }
                )
                spent += tokens
            current = _generation(self.index.generation(project_id), project_id)
            if current != generation:
                return finish("abstained-index-unavailable", "generation-changed")
        except (TimeoutError, OSError):
            return finish("abstained-index-unavailable", "index-unavailable")
        result.update(fragments=fragments, token_count=spent)
        if not fragments:
            return finish(
                "abstained-insufficient-evidence",
                "budget-exhausted" if result["dropped_for_budget"] else "insufficient-evidence",
            )
        return finish("candidates-require-source-verification", "resolver-required")
