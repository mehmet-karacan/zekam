"""Bounded existing-note evidence, never learned-state admission or vault preload."""

from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import closing
from typing import Any

from zekam.application.context_ranking import count_context_tokens
from zekam.application.knowledge_file_plane import (
    KnowledgeClassification,
    KnowledgeNoteManifest,
    manifest_digest,
    note_content_digest,
    validate_generated_note,
)
from zekam.application.local_continuity import ContinuityBinding, bounded_int, uuid_text
from zekam.application.secret_detection import SECRET_RULES, scan_text
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.context_continuity import AuthorityLevel, ContextCandidate, ContextCandidateKind
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore


class SQLiteStartupNoteSource:
    def __init__(self, continuity: SQLiteContinuityStore, files: KnowledgeFileStore) -> None:
        if not isinstance(continuity, SQLiteContinuityStore) or not isinstance(
            files, KnowledgeFileStore
        ):
            raise ValidationFailed("Startup notes require trusted operational/file adapters")
        self.continuity, self.files = continuity, files

    def _rows(
        self, binding: ContinuityBinding, *, limit: int, note_id: str | None = None
    ) -> list[sqlite3.Row]:
        bounded_int(limit, maximum=8)
        if not isinstance(binding, ContinuityBinding):
            raise ValidationFailed("Startup notes typed binding required")
        if note_id is not None:
            uuid_text(note_id, "Startup note")
        with closing(
            sqlite3.connect(f"{self.continuity.path.resolve().as_uri()}?mode=ro", uri=True)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("begin")
            current = self.continuity._assert_binding(db, binding)
            if current["status"] != "open":
                raise PolicyViolation("Startup note evidence requires open session")
            return db.execute(
                "select * from knowledge_note where realm_id=? and state='active'"
                " and materialized=1"
                " and note_kind in ('decision','failure','lesson','skill','note','reference')"
                " and ((owner_scope='global-user' and project_id is null and project_slug is null)"
                " or (project_id=? and owner_scope in (?,?)))"
                " and (? is null or id=?) order by updated_at desc,id limit ?",
                (
                    binding.realm_id,
                    binding.project_id,
                    f"project:{binding.project_id}",
                    f"work:{binding.work_item_id}",
                    note_id,
                    note_id,
                    limit,
                ),
            ).fetchall()

    def _fragment(self, binding: ContinuityBinding, row: sqlite3.Row) -> tuple[str, str, str]:
        try:
            classification = KnowledgeClassification(row["classification"])
        except (ValueError, TypeError) as exc:
            raise PolicyViolation("Startup note classification corrupt") from exc
        manifest = KnowledgeNoteManifest(
            row["owner_scope"],
            row["note_kind"],
            row["authorship"],
            classification,
            row["portable_ref"],
            row["content_digest"],
            row["project_slug"],
            row["state"],
        )
        if (
            manifest.classification is KnowledgeClassification.SECRET
            or row["archived_ref"] is not None
        ):
            raise PolicyViolation("Startup note secret/archive boundary rejected")
        if manifest.owner_scope != "global-user":
            with closing(
                sqlite3.connect(f"{self.continuity.path.resolve().as_uri()}?mode=ro", uri=True)
            ) as db:
                project = db.execute(
                    "select slug from project where id=?", (binding.project_id,)
                ).fetchone()
            if project is None or project[0] != manifest.project_slug:
                raise PolicyViolation("Startup note exact project slug drift")
        evidence = digest(
            {
                "operation": "knowledge-note-materialized",
                "note_id": row["id"],
                "portable_ref": manifest.portable_ref,
                "content_digest": manifest.content_digest,
            }
        )
        if row["materialization_evidence_digest"] != evidence:
            raise PolicyViolation("Startup note materialization receipt drift")
        payload = self.files._read_optional(manifest.portable_ref, max_bytes=65536)
        if payload is None or note_content_digest(payload) != manifest.content_digest:
            raise PolicyViolation("Startup selected note missing or content changed")
        if manifest.authorship == "generated":
            metadata = validate_generated_note(payload)
            if any(
                metadata[key] != expected
                for key, expected in {
                    "owner_scope": manifest.owner_scope,
                    "project_slug": manifest.project_slug,
                    "classification": manifest.classification.value,
                    "note_kind": manifest.note_kind,
                    "freshness": "current",
                }.items()
            ):
                raise PolicyViolation("Startup generated note provenance drift")
        text = canonical_json(
            {
                "note_id": row["id"],
                "owner_scope": manifest.owner_scope,
                "realm_id": row["realm_id"],
                "authorship": manifest.authorship,
                "note_kind": manifest.note_kind,
                "content": payload.decode("utf-8"),
                "knowledge_state": manifest.state,
                "learned_activation": "not-established",
                "evidence_only": True,
                "grants_authority": False,
            }
        )
        if scan_text(text, relative_path="startup/knowledge-note", rules=SECRET_RULES):
            raise PolicyViolation("Startup note content secret rejected")
        scope = manifest.owner_scope.replace(":", "/", 1)
        return text, manifest_digest(manifest), scope

    def candidates(
        self, binding: ContinuityBinding, *, observed_at: dt.datetime, limit: int = 8
    ) -> tuple[tuple[ContextCandidate, str], ...]:
        if not isinstance(observed_at, dt.datetime) or observed_at.tzinfo is None:
            raise ValidationFailed("Startup notes timezone-aware time required")
        result = []
        for row in self._rows(binding, limit=limit):
            text, revision, scope = self._fragment(binding, row)
            result.append(
                (
                    ContextCandidate(
                        candidate_id=f"startup-note-{row['id']}",
                        authority=AuthorityLevel.OBSERVED,
                        observed_at=observed_at,
                        source_revision=revision,
                        content_digest=digest(text),
                        token_count=count_context_tokens(text),
                        kind=ContextCandidateKind.KNOWLEDGE,
                        source_ref=row["portable_ref"],
                        scope_ref=scope,
                        canonical_revision_id=row["id"],
                        identity_refs=(f"work/{binding.work_item_id}",),
                        applicable_roles=("builder",),
                    ),
                    text,
                )
            )
        return tuple(result)

    def __call__(self, binding: ContinuityBinding, provenance: dict[str, Any]) -> str:
        if not isinstance(provenance, dict) or provenance.get("kind") != "knowledge":
            raise ValidationFailed("Startup typed knowledge provenance required")
        if (
            type(provenance.get("authority")) is not int
            or type(provenance.get("tokens")) is not int
        ):
            raise ValidationFailed("Startup note authority/tokens require exact integers")
        note_id = uuid_text(provenance.get("canonical_revision_id"), "Startup note")
        rows = self._rows(binding, limit=1, note_id=note_id)
        if not rows:
            raise PolicyViolation("Startup note current owner/realm/state mismatch")
        row = rows[0]
        text, revision, scope = self._fragment(binding, row)
        expected = {
            "id": f"startup-note-{note_id}",
            "authority": int(AuthorityLevel.OBSERVED),
            "revision": revision,
            "digest": digest(text),
            "tokens": count_context_tokens(text),
            "source_ref": row["portable_ref"],
            "scope_ref": scope,
            "identity_refs": [f"work/{binding.work_item_id}"],
        }
        if any(provenance.get(key) != value for key, value in expected.items()):
            raise PolicyViolation("Startup knowledge canonical provenance drift")
        return text
