"""Deterministic, authority-free root projection for the canonical active work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import yaml

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

PROJECTION_SCHEMA = "zekam-active-work-projection/v1"
GENERATOR_VERSION = "active-work-root/v1"


@dataclass(frozen=True, slots=True)
class ActiveWorkProjection:
    project_id: UUID
    project_slug: str
    work_id: UUID
    title: str
    summary: str
    state: str
    work_revision: int
    work_record_digest: str
    acceptance_criteria: tuple[dict[str, Any], ...]
    plan_id: UUID
    plan_revision: int
    plan_digest: str
    plan_effect_digest: str
    source_revision: str
    plan_steps: tuple[dict[str, Any], ...]
    run_id: UUID
    run_state: str
    run_digest: str
    source_observation_id: UUID
    source_head: str
    source_tree_digest: str
    source_branch: str | None
    source_dirty: bool
    source_file_count: int
    migration_head: int
    memory_mode: str
    hook_set_digest: str
    projection_receipt_digest: str
    projection_source_digest: str
    queue_blocked: int
    queue_pending: int
    queue_recovery: int
    claim_without_receipt: int
    global_dod_digest: str
    release_report_digest: str

    def __post_init__(self) -> None:
        if not self.project_slug.strip() or not self.title.strip() or not self.state.strip():
            raise ValidationFailed("Active work projection identity fields cannot be empty")
        if min(self.work_revision, self.plan_revision, self.source_file_count) < 0:
            raise ValidationFailed("Active work projection revisions/counts cannot be negative")
        if self.migration_head < 1:
            raise ValidationFailed("Active work projection requires a positive migration head")
        for value in (
            self.work_record_digest,
            self.plan_digest,
            self.plan_effect_digest,
            self.run_digest,
            self.source_tree_digest,
            self.hook_set_digest,
            self.projection_receipt_digest,
            self.projection_source_digest,
            self.global_dod_digest,
            self.release_report_digest,
        ):
            parse_digest(value)
        if any(bool(item.get("grants_authority")) for item in self.plan_steps):
            raise PolicyViolation("Projection plan steps cannot grant authority")

    def body(self) -> dict[str, Any]:
        return {
            "schema": PROJECTION_SCHEMA,
            "generator_version": GENERATOR_VERSION,
            "source": "canonical-postgresql-work-graph",
            "project": {"id": str(self.project_id), "slug": self.project_slug},
            "work": {
                "id": str(self.work_id),
                "title": self.title,
                "summary": self.summary,
                "state": self.state,
                "revision": self.work_revision,
                "record_digest": self.work_record_digest,
                "acceptance_criteria": list(self.acceptance_criteria),
            },
            "plan": {
                "id": str(self.plan_id),
                "revision": self.plan_revision,
                "plan_digest": self.plan_digest,
                "effect_digest": self.plan_effect_digest,
                "source_revision": self.source_revision,
                "steps": list(self.plan_steps),
            },
            "run": {
                "id": str(self.run_id),
                "state": self.run_state,
                "run_digest": self.run_digest,
            },
            "source_observation": {
                "id": str(self.source_observation_id),
                "head": self.source_head,
                "tree_digest": self.source_tree_digest,
                "branch": self.source_branch,
                "dirty": self.source_dirty,
                "file_count": self.source_file_count,
            },
            "memory_continuity": {
                "migration_head": self.migration_head,
                "mode": self.memory_mode,
                "hook_set_digest": self.hook_set_digest,
                "projection_receipt_digest": self.projection_receipt_digest,
                "projection_source_digest": self.projection_source_digest,
            },
            "runtime": {
                "blocked_jobs": self.queue_blocked,
                "pending_jobs": self.queue_pending,
                "recovery_required_jobs": self.queue_recovery,
                "claims_without_receipt": self.claim_without_receipt,
            },
            "legacy_global_dod": {
                "status": "preserved-not-reapplied",
                "report": "GLOBAL_DOD_DURUM.md",
                "report_digest": self.global_dod_digest,
                "release_report": "SURUM_RAPORU.md",
                "release_report_digest": self.release_report_digest,
            },
            "next_safe_action": "root projection parity, then full acceptance verification",
            "read_only": True,
            "grants_authority": False,
            "approval_inherited": False,
        }

    @property
    def projection_digest(self) -> str:
        return digest(self.body())

    def document(self) -> dict[str, Any]:
        return self.body() | {"projection_digest": self.projection_digest}

    def render_yaml(self) -> str:
        return yaml.safe_dump(
            self.document(),
            allow_unicode=True,
            sort_keys=False,
            width=4096,
        )

    def render_markdown(self) -> str:
        verified = sum(bool(item.get("verified")) for item in self.acceptance_criteria)
        criterion_lines = "\n".join(
            f"- [{'x' if bool(item.get('verified')) else ' '}] {item['text']}"
            for item in self.acceptance_criteria
        )
        step_lines = "\n".join(
            f"| `{item['step_id']}` | {item['title']} | `{item['effect']}` |"
            for item in self.plan_steps
        )
        return (
            "# Zekam Aktif Görev Projeksiyonu\n\n"
            "> Bu dosya kanonik PostgreSQL Work Graph'tan deterministik olarak üretilen, "
            "salt okunur bir projeksiyondur. Yetki vermez.\n\n"
            "## Aktif iş\n\n"
            "| Alan | Değer |\n|---|---|\n"
            f"| Proje | `{self.project_slug}` (`{self.project_id}`) |\n"
            f"| Work | `{self.work_id}` — {self.title} |\n"
            f"| Durum | `{self.state}`; revision `{self.work_revision}` |\n"
            f"| Work digest | `{self.work_record_digest}` |\n"
            f"| Plan | rev `{self.plan_revision}` / `{self.plan_id}` |\n"
            f"| Run | `{self.run_state}` / `{self.run_id}` |\n"
            f"| Source HEAD | `{self.source_head}` |\n"
            f"| Source tree | `{self.source_tree_digest}` |\n"
            f"| Memory | migration `{self.migration_head}`, mode `{self.memory_mode}`, "
            f"hooks current |\n"
            f"| Kabul | `{verified}/{len(self.acceptance_criteria)}` doğrulandı |\n"
            "| Yetki | `false`; approval devralınmadı |\n\n"
            "## Plan adımları\n\n"
            "| Adım | Açıklama | Etki |\n|---|---|---|\n"
            f"{step_lines}\n\n"
            "## Kabul kriterleri\n\n"
            f"{criterion_lines}\n\n"
            "## Süreklilik ve güvenlik\n\n"
            f"- Projection receipt: `{self.projection_receipt_digest}`\n"
            f"- Hook set: `{self.hook_set_digest}`\n"
            f"- Açık receipt'siz claim: `{self.claim_without_receipt}`\n"
            f"- Pending/recovery job: `{self.queue_pending}/{self.queue_recovery}`\n"
            f"- Bloklu runtime kaydı: `{self.queue_blocked}`\n"
            "- Eski Global DoD çalışması korunmuştur; yeniden uygulanmamıştır.\n"
            f"- `GLOBAL_DOD_DURUM.md`: `{self.global_dod_digest}`\n"
            f"- `SURUM_RAPORU.md`: `{self.release_report_digest}`\n\n"
            "## Sonraki güvenli adım\n\n"
            "Root projection parity doğrulamasını tamamla; ardından tam kabul testlerine geç.\n\n"
            f"Projection digest: `{self.projection_digest}`\n"
        )
