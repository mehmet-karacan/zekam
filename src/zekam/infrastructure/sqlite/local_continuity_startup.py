"""Trusted heterogeneous startup provenance from existing operational authority.

No database admission, provider call, directory traversal or learned-state activation.
The selected rows are re-read during hydration inside the writer's transaction window.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any

from zekam.application.context_ranking import count_context_tokens
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_startup import StartupRequest, StartupSnapshot
from zekam.application.local_startup_retrieval import LocalStartupRetrieval
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.context_continuity import (
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
    EvidenceReference,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.local_continuity_environment import LocalContinuityEnvironment
from zekam.infrastructure.local_continuity_source import ProjectContinuitySourceResolver
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_startup_checkpoint import SQLiteStartupCheckpointSource
from zekam.infrastructure.sqlite.local_startup_notes import SQLiteStartupNoteSource
from zekam.infrastructure.sqlite.operational_schema import SCHEMA_VERSION, status


def _document(raw: object, label: str) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode()) > 262144:
        raise PolicyViolation(f"Startup {label} bounded object required")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or canonical_json(value) != raw:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise PolicyViolation(f"Startup {label} canonical object required") from exc
    return value


class SQLiteStartupSourceResolver:
    def __init__(
        self,
        continuity: SQLiteContinuityStore,
        project_sources: ProjectContinuitySourceResolver,
        *,
        note_sources: SQLiteStartupNoteSource | None = None,
        retrieval: LocalStartupRetrieval | None = None,
        environment: LocalContinuityEnvironment | None = None,
        checkpoints: SQLiteStartupCheckpointSource | None = None,
    ) -> None:
        if not isinstance(continuity, SQLiteContinuityStore) or not isinstance(
            project_sources, ProjectContinuitySourceResolver
        ):
            raise ValidationFailed("Startup requires exact operational/project source adapters")
        self.continuity, self.project_sources = continuity, project_sources
        if note_sources is not None and (
            not isinstance(note_sources, SQLiteStartupNoteSource)
            or note_sources.continuity.path != continuity.path
        ):
            raise ValidationFailed("Startup notes exact operational store required")
        self.note_sources = note_sources
        if retrieval is not None and not isinstance(retrieval, LocalStartupRetrieval):
            raise ValidationFailed("Startup retrieval typed source required")
        self.retrieval = retrieval
        if environment is not None and (
            not isinstance(environment, LocalContinuityEnvironment)
            or environment.operational_path != continuity.path
        ):
            raise ValidationFailed("Startup environment exact operational path required")
        self.environment = environment
        if checkpoints is not None and (
            not isinstance(checkpoints, SQLiteStartupCheckpointSource)
            or checkpoints.continuity.path != continuity.path
        ):
            raise ValidationFailed("Startup checkpoint exact operational store required")
        self.checkpoints = checkpoints

    def preflight(self, binding: ContinuityBinding) -> dict[str, Any] | None:
        return None if self.environment is None else self.environment.validate(binding)

    @staticmethod
    def _no_predecessor_pending(db: sqlite3.Connection, binding: ContinuityBinding) -> None:
        # Session ownership alone misses unknown effects left by a predecessor on this work.
        scope = (
            "(json_extract(j.payload_json,'$.work_item_id')=?"
            " or json_extract(j.payload_json,'$.run_id')=?"
            " or json_extract(j.payload_json,'$.session_id') in"
            " (select id from session where work_item_id=?))"
        )
        args = (binding.work_item_id, binding.run_id, binding.work_item_id)
        pending = db.execute(
            "select 1 from local_job j where " + scope + " and ("
            "j.state in ('ready','running','recovery-required')"
            " or exists(select 1 from local_lease l where l.job_id=j.id)"
            " or exists(select 1 from local_recovery_case rc"
            " where rc.job_id=j.id and rc.state='open')"
            " or exists(select 1 from local_outbox o"
            " left join local_outbox_delivery d on d.outbox_id=o.id"
            " left join local_outbox_receipt r on r.outbox_id=o.id"
            " where o.job_id=j.id and (d.outbox_id is null or d.state<>'delivered' or r.id is null"
            " or r.claim_id<>d.claim_id or r.fencing_token<>d.fencing_counter"
            " or not(r.status='delivered' or (r.status='unknown' and exists("
            " select 1 from local_recovery_case rc join local_recovery_resolution rr"
            " on rr.recovery_case_id=rc.id where rc.outbox_id=o.id and rc.state='resolved'"
            " and rr.outcome='delivered')))))"
            " or exists(select 1 from local_effect_claim c left join local_effect_receipt r"
            " on r.claim_id=c.id where c.job_id=j.id and (r.id is null or r.status='unknown')"
            " and not exists(select 1 from local_recovery_case rc join local_recovery_resolution rr"
            " on rr.recovery_case_id=rc.id where rc.effect_claim_id=c.id and rc.state='resolved'"
            " and rr.outcome in ('completed','failed')))) limit 1",
            args,
        ).fetchone()
        if pending:
            raise PolicyViolation("Startup predecessor work/run requires reconciliation")

    def _rows(self, binding: ContinuityBinding) -> dict[str, Any]:
        if not isinstance(binding, ContinuityBinding) or binding.run_id is None:
            raise ValidationFailed("Startup requires typed exact work/run binding")
        binding.__post_init__()
        self.preflight(binding)
        with closing(
            sqlite3.connect(f"{self.continuity.path.resolve().as_uri()}?mode=ro", uri=True)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("begin")
            session = self.continuity._assert_binding(db, binding)
            if session["status"] != "open":
                raise PolicyViolation("Startup requires open session")
            self.continuity._no_pending(db, binding)
            self._no_predecessor_pending(db, binding)
            config = dict(
                db.execute(
                    "select * from config_revision"
                    " where active=1 and config_digest=? and task_digest=?",
                    (binding.policy_digest, binding.task_digest),
                ).fetchone()
            )
            work_row = db.execute(
                "select w.id as work_id,w.project_id,w.kind,w.title,w.state as current_state,"
                "w.revision as current_revision,w.evidence_digest as current_evidence,r.*"
                " from work_item w join work_revision r on r.work_item_id=w.id"
                " and r.revision=w.revision where w.id=? and w.project_id=?",
                (binding.work_item_id, binding.project_id),
            ).fetchone()
            if work_row is None:
                raise PolicyViolation("Startup current work revision missing")
            work = dict(work_row)
            run = dict(db.execute("select * from run where id=?", (binding.run_id,)).fetchone())
            source = dict(
                db.execute(
                    "select * from source_snapshot where id=?", (binding.source_snapshot_id,)
                ).fetchone()
            )
        config_document = _document(config["sanitized_json"], "config")
        if digest(config_document) != binding.policy_digest:
            raise PolicyViolation("Startup admitted config digest drift")
        payload = _document(work["payload_json"], "work")
        if (
            digest(payload) != work["payload_digest"]
            or work["state"] != work["current_state"]
            or work["evidence_digest"] != work["current_evidence"]
        ):
            raise PolicyViolation("Startup current work revision/payload drift")
        if run["status"] == "unknown":
            raise PolicyViolation("Startup unknown run requires reconciliation")
        runtime = config_document.get("runtime")
        if (
            not isinstance(runtime, dict)
            or not isinstance(runtime.get("network_default"), str)
            or runtime["network_default"] not in {"deny", "allow"}
        ):
            raise PolicyViolation("Startup admitted runtime policy missing or malformed")
        profile = runtime.get("permission_profile")
        if not isinstance(profile, str) or profile not in {
            "workspace-write-no-network",
            "read-only",
            "workspace-write",
            "danger-full-access",
        }:
            raise PolicyViolation("Startup admitted permission profile unrecognized")
        # No paths, credentials, process identities or unreviewed arbitrary fields are rendered.
        policy = {
            "config_revision_id": config["id"],
            "task_digest": binding.task_digest,
            "config_digest": config["config_digest"],
            "realm_id": binding.realm_id,
            "runtime": {
                "network_default": runtime["network_default"],
                "permission_profile": profile,
            },
            "continuity_grants_authority": False,
        }
        work_body = {
            "work_item_id": binding.work_item_id,
            "project_id": binding.project_id,
            "revision_id": work["id"],
            "revision": work["revision"],
            "state": work["state"],
            "title": work["title"],
            "kind": work["kind"],
            "payload": payload,
            "payload_digest": work["payload_digest"],
            "evidence_digest": work["evidence_digest"],
            "continuity_grants_authority": False,
        }
        run_body = {
            key: run[key]
            for key in (
                "id",
                "work_item_id",
                "source_snapshot_id",
                "config_revision_id",
                "status",
                "plan_digest",
                "terminal_receipt_digest",
                "updated_at",
            )
        }
        run_body["continuity_grants_authority"] = False
        return {
            "source": source,
            "system-policy": (
                canonical_json(policy),
                config["config_digest"],
                f"config/{config['id']}",
                f"realm/{binding.realm_id}",
                config["id"],
            ),
            "work-contract": (
                canonical_json(work_body),
                f"work-revision/{work['id']}",
                f"work/{binding.work_item_id}",
                f"work/{binding.work_item_id}",
                work["id"],
            ),
            "run-status": (
                canonical_json(run_body),
                digest(run_body),
                f"run/{binding.run_id}",
                f"work/{binding.work_item_id}",
                binding.run_id,
            ),
        }

    def snapshot(self, binding: ContinuityBinding, request: StartupRequest) -> StartupSnapshot:
        if not isinstance(request, StartupRequest):
            raise ValidationFailed("Typed startup request required")
        request.__post_init__()
        current = status(self.continuity.path)
        if (
            not current.schema_ok
            or not current.integrity_ok
            or current.schema_version != SCHEMA_VERSION
        ):
            raise PolicyViolation("Startup operational schema/integrity drift")
        rows = self._rows(binding)
        candidates, fragments = [], []
        for kind in ("system-policy", "work-contract", "run-status"):
            text, revision, ref, scope, canonical_id = rows[kind]
            candidate = ContextCandidate(
                candidate_id=f"startup-{kind}",
                authority=AuthorityLevel.CANONICAL,
                observed_at=request.observed_at,
                source_revision=revision,
                content_digest=digest(text),
                token_count=count_context_tokens(text),
                required=True,
                kind=ContextCandidateKind(kind),
                source_ref=ref,
                scope_ref=scope,
                identity_refs=(f"work/{binding.work_item_id}",),
                applicable_roles=("builder",),
                canonical_revision_id=canonical_id,
            )
            candidates.append(candidate)
            fragments.append((candidate.candidate_id, text))
        for ref in request.source_refs:
            text = self.project_sources.read_fragment(binding, ref)
            candidate = ContextCandidate(
                candidate_id=f"startup-source-{digest(ref)[7:23]}",
                authority=AuthorityLevel.VERIFIED,
                observed_at=request.observed_at,
                source_revision=rows["source"]["revision_ref"],
                content_digest=digest(text),
                token_count=count_context_tokens(text),
                required=True,
                kind=ContextCandidateKind.SOURCE_SLICE,
                source_ref=ref,
                scope_ref=f"project/{binding.project_id}",
                identity_refs=(f"work/{binding.work_item_id}",),
                applicable_roles=("builder",),
                canonical_revision_id=binding.source_snapshot_id,
            )
            candidates.append(candidate)
            fragments.append((candidate.candidate_id, text))
        if request.note_limit:
            if self.note_sources is None:
                raise PolicyViolation("Startup knowledge note source unavailable")
            for candidate, text in self.note_sources.candidates(
                binding, observed_at=request.observed_at, limit=request.note_limit
            ):
                candidates.append(candidate)
                fragments.append((candidate.candidate_id, text))
        retrieval_report = None
        if request.retrieval_query is not None:
            if self.retrieval is None:
                retrieval_report = {
                    "state": "abstained-index-unavailable",
                    "reason": "index-not-configured",
                    "searched_channels": [],
                    "dense": "not-invoked",
                    "source_bytes_verified": False,
                    "fragment_count": 0,
                }
            else:
                report = self.retrieval.query(
                    request.retrieval_query,
                    project_id=binding.project_id,
                    expected_source_revision=rows["source"]["revision_ref"],
                    expected_tree_digest=rows["source"]["tree_digest"],
                    token_budget=min(request.token_budget, 16384),
                )
                for fragment in report["fragments"]:
                    checked = self.retrieval.verify_fragment(
                        project_id=binding.project_id,
                        expected_source_revision=rows["source"]["revision_ref"],
                        expected_tree_digest=rows["source"]["tree_digest"],
                        generation_digest=fragment["generation_digest"],
                        chunk_id=fragment["chunk_id"],
                    )
                    ref, text = self._retrieved_bytes(binding, checked)
                    candidate = ContextCandidate(
                        candidate_id=f"startup-citation-{digest(fragment['chunk_id'])[7:39]}",
                        authority=AuthorityLevel.VERIFIED,
                        observed_at=request.observed_at,
                        source_revision=rows["source"]["revision_ref"],
                        content_digest=digest(text),
                        token_count=count_context_tokens(text),
                        kind=ContextCandidateKind.CITATION,
                        source_ref=ref,
                        scope_ref=f"project/{binding.project_id}",
                        canonical_revision_id=binding.source_snapshot_id,
                        identity_refs=(f"work/{binding.work_item_id}",),
                        applicable_roles=("builder",),
                        evidence_refs=(
                            EvidenceReference(
                                "citation",
                                f"index-generation/{fragment['generation_digest'][7:]}",
                                fragment["generation_digest"],
                            ),
                            EvidenceReference(
                                "citation",
                                f"index-chunk/{fragment['chunk_id']}",
                                checked["content_digest"],
                            ),
                        ),
                    )
                    candidates.append(candidate)
                    fragments.append((candidate.candidate_id, text))
                retrieval_report = {
                    "state": "source-verified-candidates"
                    if report["fragments"]
                    else report["state"],
                    "index_retrieval_digest": report["retrieval_digest"],
                    "reason": report["reason"],
                    "searched_channels": report["searched_channels"],
                    "generation": report["generation"],
                    "dense": "not-invoked",
                    "source_bytes_verified": bool(report["fragments"]),
                    "fragment_count": len(report["fragments"]),
                    "citations": [
                        {
                            key: fragment[key]
                            for key in (
                                "chunk_id",
                                "source_ref",
                                "locator",
                                "generation_digest",
                                "channels",
                                "ranks",
                                "exact_match",
                                "rrf_score",
                            )
                        }
                        for fragment in report["fragments"]
                    ],
                }
        checkpoint_report = None
        if self.checkpoints is not None:
            checkpoint_fragment, checkpoint_report = self.checkpoints.snapshot(
                binding, observed_at=request.observed_at
            )
            if checkpoint_fragment is not None:
                candidate, text = checkpoint_fragment
                candidates.append(candidate)
                fragments.append((candidate.candidate_id, text))
        return StartupSnapshot(
            tuple(candidates),
            tuple(fragments),
            rows["source"]["revision_ref"],
            retrieval_report,
            checkpoint_report,
        )

    def _retrieved_bytes(
        self, binding: ContinuityBinding, fragment: dict[str, Any]
    ) -> tuple[str, str]:
        locator = fragment["locator"]
        path = fragment["source_ref"]
        whole = self.project_sources.read_fragment(binding, path)
        first, last = locator["line_start"], locator["line_end"]
        lines = whole.splitlines(keepends=True)
        if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(lines):
            raise ValidationFailed("Startup citation line locator outside captured source")
        ref = f"{path}#L{first}-L{last}"
        # Whole-file identity and citation bytes must come from ONE captured buffer.
        # Reopening the path for the slice could combine two different file revisions.
        text = "".join(lines[first - 1 : last])
        # Index normalization may trim surrounding whitespace, never rewrite source content.
        if (
            digest_of_bytes(whole.encode()) != fragment["source_digest"]
            or text.strip() != fragment["text"].strip()
        ):
            raise PolicyViolation("Startup retrieval original source bytes/locator drift")
        return ref, text

    def __call__(self, binding: ContinuityBinding, provenance: dict[str, Any]) -> str:
        if not isinstance(provenance, dict):
            raise ValidationFailed("Startup provenance object required")
        if (
            type(provenance.get("authority")) is not int
            or type(provenance.get("tokens")) is not int
        ):
            raise ValidationFailed("Startup authority/tokens require exact integers")
        rows = self._rows(binding)
        kind = provenance.get("kind")
        if kind == "checkpoint" and self.checkpoints is not None:
            return self.checkpoints(binding, provenance)
        if kind == "knowledge" and self.note_sources is not None:
            return self.note_sources(binding, provenance)
        if kind == "citation":
            evidence = provenance.get("evidence_refs")
            if self.retrieval is None or not isinstance(evidence, list) or len(evidence) != 2:
                raise PolicyViolation("Startup citation exact generation/chunk evidence required")
            generation, chunk = evidence
            if (
                not isinstance(generation, dict)
                or not isinstance(chunk, dict)
                or generation.get("kind") != "citation"
                or chunk.get("kind") != "citation"
                or not isinstance(generation.get("digest"), str)
                or generation.get("ref") != f"index-generation/{generation['digest'][7:]}"
                or not isinstance(chunk.get("ref"), str)
                or not chunk["ref"].startswith("index-chunk/")
                or generation.get("revision") is not None
                or chunk.get("revision") is not None
            ):
                raise PolicyViolation("Startup citation evidence malformed")
            checked = self.retrieval.verify_fragment(
                project_id=binding.project_id,
                expected_source_revision=rows["source"]["revision_ref"],
                expected_tree_digest=rows["source"]["tree_digest"],
                generation_digest=generation["digest"],
                chunk_id=chunk["ref"][12:],
            )
            if chunk.get("digest") != checked["content_digest"]:
                raise PolicyViolation("Startup citation pinned content digest drift")
            ref, text = self._retrieved_bytes(binding, checked)
            revision, scope, canonical_id = (
                rows["source"]["revision_ref"],
                f"project/{binding.project_id}",
                binding.source_snapshot_id,
            )
            authority = AuthorityLevel.VERIFIED
        elif kind == "source-slice":
            text = self.project_sources(binding, provenance)
            revision, ref, scope, canonical_id = (
                rows["source"]["revision_ref"],
                provenance["source_ref"],
                f"project/{binding.project_id}",
                binding.source_snapshot_id,
            )
            authority = AuthorityLevel.VERIFIED
        elif isinstance(kind, str) and kind in {"system-policy", "work-contract", "run-status"}:
            text, revision, ref, scope, canonical_id = rows[kind]
            authority = AuthorityLevel.CANONICAL
        else:
            raise PolicyViolation("Startup source kind has no registered resolver")
        expected = {
            "revision": revision,
            "source_ref": ref,
            "scope_ref": scope,
            "canonical_revision_id": canonical_id,
            "digest": digest(text),
            "tokens": count_context_tokens(text),
            "authority": int(authority),
            "identity_refs": [f"work/{binding.work_item_id}"],
        }
        if any(provenance.get(key) != value for key, value in expected.items()):
            raise PolicyViolation("Startup source canonical revision/scope/provenance drift")
        return text

    def assert_current(self, binding: ContinuityBinding, snapshot: StartupSnapshot) -> None:
        if not isinstance(snapshot, StartupSnapshot):
            raise ValidationFailed("Startup typed snapshot required")
        for candidate in snapshot.candidates:
            if (
                self(binding, candidate.provenance_body)
                != dict(snapshot.fragments)[candidate.candidate_id]
            ):
                raise PolicyViolation("Startup snapshot changed before hydration")
