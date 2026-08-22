"""Dogal dil intake orchestration.

Kanonik project registry'den aday kurar, exact identifier onceligini korur ve
belirsizlik varsa mutation baslatmadan netlestirme talebi uretir.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from zekam.domain.intake import (
    AmbiguityKind,
    ConversationSubject,
    IntakeRequest,
    IntakeResolution,
    MatchKind,
    ProjectCandidate,
    RequestClass,
    assert_identifier_preserved,
    normalize_text,
    resolve_intake,
)
from zekam.domain.project import Project, ProjectAlias

MIN_TRIGRAM_TOKEN_CHARS = 3

#: Kucuk sayi = guclu eslesme.
_PRIORITY: dict[MatchKind, int] = {
    MatchKind.EXACT_ID: 0,
    MatchKind.EXACT_ALIAS: 1,
    MatchKind.NORMALIZED_ALIAS: 2,
    MatchKind.TRIGRAM: 3,
}


@dataclass(frozen=True, slots=True)
class ClarificationRequest:
    """Kullaniciya sorulacak tek netlestirme."""

    kind: AmbiguityKind
    question: str
    options: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "question": self.question,
            "options": list(self.options),
        }


_QUESTIONS: dict[AmbiguityKind, str] = {
    AmbiguityKind.MULTIPLE_INTENTS: "Istegi hangi sinifta ele alalim?",
    AmbiguityKind.NO_INTENT_CUE: "Ne yapmami istiyorsunuz?",
    AmbiguityKind.ANAPHORA_UNRESOLVED: "'Bunu' ile hangi konuyu kastediyorsunuz?",
    AmbiguityKind.PROJECT_AMBIGUOUS: "Hangi projeyi kastediyorsunuz?",
    AmbiguityKind.PROJECT_UNRESOLVED: "Hangi proje uzerinde calisalim?",
    AmbiguityKind.IDENTIFIER_UNKNOWN: "Bu kimlik kayitli degil; dogru numara nedir?",
}


@dataclass(frozen=True, slots=True)
class IntakeOutcome:
    resolution: IntakeResolution
    clarifications: tuple[ClarificationRequest, ...]

    @property
    def may_start_work(self) -> bool:
        return self.resolution.may_start_work

    def as_dict(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution.as_dict(),
            "clarifications": [item.as_dict() for item in self.clarifications],
            "may_start_work": self.may_start_work,
        }


def build_candidates(
    text: str,
    *,
    projects: tuple[Project, ...],
    aliases: dict[str, tuple[ProjectAlias, ...]],
) -> tuple[ProjectCandidate, ...]:
    """Kanonik registry'den aday listesi kurar; exact eslesme once gelir."""

    normalized = normalize_text(text)
    tokens = {token for token in normalized.split(" ") if len(token) >= MIN_TRIGRAM_TOKEN_CHARS}
    found: dict[str, ProjectCandidate] = {}

    def _offer(project: Project, kind: MatchKind, matched_on: str) -> None:
        current = found.get(project.slug)
        if current is not None and _PRIORITY[current.match_kind] <= _PRIORITY[kind]:
            return
        found[project.slug] = ProjectCandidate(
            project_ref=project.slug,
            display_name=project.display_name,
            match_kind=kind,
            matched_on=matched_on,
        )

    for project in projects:
        if normalize_text(project.slug) in tokens:
            _offer(project, MatchKind.EXACT_ID, project.slug)
            continue
        for alias in aliases.get(project.slug, ()):  # alias tablosu kanoniktir
            if normalize_text(alias.alias) in tokens:
                _offer(project, MatchKind.EXACT_ALIAS, alias.alias)
                continue
            phrase = normalize_text(alias.normalized.replace("-", " "))
            if phrase and re.search(rf"(?:^|\s){re.escape(phrase)}(?:\s|$)", normalized):
                _offer(project, MatchKind.NORMALIZED_ALIAS, alias.alias)
    return tuple(found[key] for key in sorted(found))


@dataclass(frozen=True, slots=True)
class IntakeService:
    """Salt okunur intake; hicbir kaydi degistirmez."""

    def resolve(
        self,
        text: str,
        *,
        now: dt.datetime,
        projects: tuple[Project, ...] = (),
        aliases: dict[str, tuple[ProjectAlias, ...]] | None = None,
        known_identifiers: frozenset[str] = frozenset(),
        subject: ConversationSubject | None = None,
        current_project_ref: str | None = None,
        current_work_ref: str | None = None,
        project_required: bool = True,
    ) -> IntakeOutcome:
        request = IntakeRequest(
            text=text,
            received_at=now,
            subject=subject,
            current_project_ref=current_project_ref,
            current_work_ref=current_work_ref,
        )
        candidates = build_candidates(text, projects=projects, aliases=aliases or {})
        resolution = resolve_intake(
            request,
            candidates=candidates,
            known_identifiers=known_identifiers,
            project_required=project_required,
        )
        assert_identifier_preserved(resolution, original_text=text)
        clarifications = tuple(
            ClarificationRequest(
                kind=item.kind,
                question=_QUESTIONS[item.kind],
                options=item.options,
            )
            for item in resolution.ambiguities
        )
        return IntakeOutcome(resolution=resolution, clarifications=clarifications)

    def is_research(self, outcome: IntakeOutcome) -> bool:
        return outcome.resolution.request_class is RequestClass.RESEARCH

    def is_benchmark(self, outcome: IntakeOutcome) -> bool:
        """Benchmark niyetini tanir; tek basina authority veya provider effect vermez."""

        return outcome.resolution.request_class is RequestClass.BENCHMARK
