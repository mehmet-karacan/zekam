"""Deterministic user-intent and multi-project family routing.

The reviewed family catalogue is portable configuration.  Runtime project identity
still comes from the operational store; catalogue members that are not registered are
reported as unavailable instead of being silently invented.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from zekam.application.config import core_root, package_root
from zekam.application.jira_issue_routing import resolve_jira_issue
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.identifiers import validate_slug

SCHEMA = "zekam-project-families/v1"
_ROLES = frozenset({"ui", "backend", "fullstack"})
_ISSUE_KEY = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]+)-(\d+)(?![A-Za-z0-9])", re.I)
_LONG_NUMBER = re.compile(r"(?<![A-Za-z0-9-])(\d{3,})(?![A-Za-z0-9-])")

_UI_TERMS = (
    "ui",
    "frontend",
    "front end",
    "ekran",
    "sayfa",
    "buton",
    "css",
    "react",
    "nextjs",
    "component",
    "komponent",
    "tasarım",
    "tasarim",
)
_BACKEND_TERMS = (
    "backend",
    "back end",
    "mikroservis",
    "microservice",
    "api",
    "endpoint",
    "controller",
    "controllerlar",
    "controllerları",
    "controllerlari",
    "service",
    "repository",
    "jwt",
    "spring",
    "kafka",
    "veritaban",
    "database",
)
_CHANGE_TERMS = (
    "değiştir",
    "degistir",
    "düzelt",
    "duzelt",
    "ekle",
    "implement",
    "refactor",
    "oluştur",
    "olustur",
)
_REVIEW_TERMS = ("review", "incele", "denetle", "kod inceleme")
_TEST_TERMS = ("test et", "testleri", "doğrula", "dogrula", "acceptance")
_PROJECT_CONTEXT_TERMS = (
    "proje",
    "kod",
    "class",
    "sınıf",
    "sinif",
    "dosya",
    "modül",
    "modul",
    "müşteri",
    "musteri",
)


@dataclass(frozen=True, slots=True)
class ProjectFamilyMember:
    project_ref: str
    role: str


@dataclass(frozen=True, slots=True)
class ProjectFamily:
    family_ref: str
    display_name: str
    aliases: tuple[str, ...]
    jira_prefix: str
    members: tuple[ProjectFamilyMember, ...]


@dataclass(frozen=True, slots=True)
class ProjectFamilyCatalog:
    families: tuple[ProjectFamily, ...]
    catalog_digest: str


@dataclass(frozen=True, slots=True)
class RegisteredProject:
    project_ref: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RequestRoute:
    status: str
    intent: str
    strategy: str
    family_ref: str | None
    project_refs: tuple[str, ...]
    project_roles: tuple[str, ...]
    agent_roles: tuple[str, ...]
    reason_codes: tuple[str, ...]
    matched_terms: tuple[str, ...]
    unavailable_project_refs: tuple[str, ...]
    jira_issue_key: str | None
    query_digest: str
    catalog_digest: str

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": "zekam-request-route/v1",
            "status": self.status,
            "intent": self.intent,
            "strategy": self.strategy,
            "family_ref": self.family_ref,
            "project_refs": list(self.project_refs),
            "project_roles": list(self.project_roles),
            "agent_roles": list(self.agent_roles),
            "reason_codes": list(self.reason_codes),
            "matched_terms": list(self.matched_terms),
            "unavailable_project_refs": list(self.unavailable_project_refs),
            "jira_issue_key": self.jira_issue_key,
            "query_digest": self.query_digest,
            "catalog_digest": self.catalog_digest,
            "provider_calls": 0,
            "grants_authority": False,
        }
        body["route_digest"] = digest(body)
        return body


def default_family_file() -> Path:
    repository_copy = core_root() / "config" / "project_families.yaml"
    if repository_copy.is_file():
        return repository_copy
    return package_root() / "_config" / "project_families.yaml"


def _normalized_phrase(value: object, label: str) -> str:
    phrase = " ".join(str(value).casefold().split())
    if not phrase or len(phrase.encode("utf-8")) > 128:
        raise ValidationFailed(f"{label} bounded non-empty olmali")
    return phrase


def load_project_families(path: Path | None = None) -> ProjectFamilyCatalog:
    candidate = path or default_family_file()
    if candidate.is_symlink():
        raise PolicyViolation("Project family dosyasi symlink olamaz")
    target = candidate.resolve(strict=True)
    if not target.is_file() or target.stat().st_size > 64 * 1024:
        raise PolicyViolation("Project family dosyasi guvenli regular file olmali")
    document: Any = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"schema", "families"}:
        raise ValidationFailed("Project family semasi exact olmali")
    raw_families = document["families"]
    if document["schema"] != SCHEMA or not isinstance(raw_families, list):
        raise ValidationFailed("Project family schema/version gecersiz")
    families: list[ProjectFamily] = []
    seen_families: set[str] = set()
    seen_aliases: set[str] = set()
    seen_prefixes: set[str] = set()
    seen_members: set[str] = set()
    canonical: list[dict[str, Any]] = []
    for item in raw_families:
        if not isinstance(item, dict) or set(item) != {
            "family_ref",
            "display_name",
            "aliases",
            "jira_prefix",
            "members",
        }:
            raise ValidationFailed("Project family girdisi exact olmali")
        family_ref = validate_slug(str(item["family_ref"]))
        display_name = str(item["display_name"]).strip()
        aliases_value = item["aliases"]
        members_value = item["members"]
        prefix = str(item["jira_prefix"]).strip().upper()
        if (
            family_ref in seen_families
            or not display_name
            or not isinstance(aliases_value, list)
            or not aliases_value
            or not isinstance(members_value, list)
            or not members_value
            or not prefix.isalnum()
            or prefix in seen_prefixes
        ):
            raise ValidationFailed("Project family kimligi tekrarli veya gecersiz")
        aliases = tuple(
            _normalized_phrase(value, "Project family alias") for value in aliases_value
        )
        if len(aliases) != len(set(aliases)) or set(aliases) & seen_aliases:
            raise ValidationFailed("Project family aliaslari global unique olmali")
        members: list[ProjectFamilyMember] = []
        for raw_member in members_value:
            if not isinstance(raw_member, dict) or set(raw_member) != {"project_ref", "role"}:
                raise ValidationFailed("Project family member girdisi exact olmali")
            project_ref = validate_slug(str(raw_member["project_ref"]))
            role = str(raw_member["role"]).strip().casefold()
            if project_ref in seen_members or role not in _ROLES:
                raise ValidationFailed("Project family member tekrarli veya rolu gecersiz")
            seen_members.add(project_ref)
            members.append(ProjectFamilyMember(project_ref, role))
        family = ProjectFamily(family_ref, display_name, aliases, prefix, tuple(members))
        families.append(family)
        seen_families.add(family_ref)
        seen_aliases.update(aliases)
        seen_prefixes.add(prefix)
        canonical.append(
            {
                "family_ref": family_ref,
                "display_name": display_name,
                "aliases": list(aliases),
                "jira_prefix": prefix,
                "members": [
                    {"project_ref": member.project_ref, "role": member.role} for member in members
                ],
            }
        )
    if not families:
        raise ValidationFailed("Project family listesi bos olamaz")
    return ProjectFamilyCatalog(tuple(families), digest({"schema": SCHEMA, "families": canonical}))


def _contains(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.UNICODE) is not None


def _matched(text: str, phrases: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {phrase for phrase in phrases if _contains(text, phrase)},
            key=lambda x: (-len(x), x),
        )
    )


def _family_for_project(catalog: ProjectFamilyCatalog, project_ref: str) -> ProjectFamily | None:
    return next(
        (
            family
            for family in catalog.families
            if any(member.project_ref == project_ref for member in family.members)
        ),
        None,
    )


def _family_for_reference(catalog: ProjectFamilyCatalog, reference: str) -> ProjectFamily | None:
    return next(
        (
            family
            for family in catalog.families
            if family.family_ref == reference
            or family.jira_prefix.casefold() == reference.casefold()
            or any(member.project_ref == reference for member in family.members)
        ),
        None,
    )


def route_request(
    question: str,
    *,
    catalog: ProjectFamilyCatalog,
    registered_projects: tuple[RegisteredProject, ...],
) -> RequestRoute:
    if (
        not isinstance(question, str)
        or not question.strip()
        or len(question.encode("utf-8")) > 16_384
    ):
        raise ValidationFailed("Route sorusu bounded non-empty text olmali")
    normalized = " ".join(question.casefold().split())
    query_digest = digest({"query": question})
    available = {project.project_ref for project in registered_projects}

    direct_matches: list[tuple[int, RegisteredProject, str]] = []
    for project in registered_projects:
        for phrase in (project.project_ref, *project.aliases):
            normalized_phrase = _normalized_phrase(phrase, "Project alias")
            if _contains(normalized, normalized_phrase):
                direct_matches.append((len(normalized_phrase), project, normalized_phrase))
    if direct_matches:
        direct_refs = {item[1].project_ref for item in direct_matches}
        if len(direct_refs) > 1:
            return RequestRoute(
                "clarification-required",
                "project-question",
                "clarify-project",
                None,
                tuple(sorted(direct_refs)),
                (),
                (),
                ("multiple-direct-projects",),
                tuple(sorted({item[2] for item in direct_matches})),
                (),
                None,
                query_digest,
                catalog.catalog_digest,
            )
        direct_project = next(iter(direct_refs))
        direct_family = _family_for_project(catalog, direct_project)
    else:
        direct_project = None
        direct_family = None

    family_matches = [
        (len(alias), family, alias)
        for family in catalog.families
        for alias in (family.family_ref, *family.aliases)
        if _contains(normalized, alias)
    ]
    exact_issue = _ISSUE_KEY.findall(question)
    long_numbers = _LONG_NUMBER.findall(normalized)
    jira_candidate = bool(exact_issue) or (
        bool(family_matches or direct_matches)
        and len(long_numbers) == 1
        and (
            len(long_numbers[0]) >= 4
            or _contains(normalized, "jira")
            or _contains(normalized, "task")
        )
    )
    if jira_candidate:
        try:
            resolution = resolve_jira_issue(question)
            family = _family_for_reference(catalog, resolution.project_ref)
        except ValidationFailed:
            family = None
            resolution = None
        if resolution is None or family is None:
            return RequestRoute(
                "clarification-required",
                "jira-detail",
                "clarify-jira-project",
                None,
                (),
                (),
                (),
                ("jira-project-unresolved",),
                (),
                (),
                None,
                query_digest,
                catalog.catalog_digest,
            )
        members = tuple(member for member in family.members if member.project_ref in available)
        unavailable = tuple(
            member.project_ref for member in family.members if member.project_ref not in available
        )
        return RequestRoute(
            "selected",
            "jira-detail",
            "jira-mcp",
            family.family_ref,
            tuple(member.project_ref for member in members),
            tuple(member.role for member in members),
            ("researcher",),
            ("jira-key-resolved",),
            (resolution.jira_prefix,),
            unavailable,
            resolution.issue_key,
            query_digest,
            catalog.catalog_digest,
        )

    chosen: ProjectFamilyMember | tuple[ProjectFamilyMember, ...]
    if direct_project is not None:
        matched_families = {item[1].family_ref: item[1] for item in family_matches}
        direct_family_ref = direct_family.family_ref if direct_family is not None else None
        conflicting_families = {
            family_ref: family
            for family_ref, family in matched_families.items()
            if family_ref != direct_family_ref
        }
        if conflicting_families:
            signaled_refs = {direct_project}
            unavailable_refs: set[str] = set()
            for family in conflicting_families.values():
                for member in family.members:
                    if member.project_ref in available:
                        signaled_refs.add(member.project_ref)
                    else:
                        unavailable_refs.add(member.project_ref)
            return RequestRoute(
                "clarification-required",
                "project-question",
                "clarify-project-family",
                None,
                tuple(sorted(signaled_refs)),
                (),
                (),
                ("direct-project-family-conflict", "multiple-project-families"),
                tuple(
                    sorted(
                        {item[2] for item in direct_matches} | {item[2] for item in family_matches}
                    )
                ),
                tuple(sorted(unavailable_refs)),
                None,
                query_digest,
                catalog.catalog_digest,
            )

    if direct_project is not None:
        family = direct_family
        chosen = (
            ProjectFamilyMember(direct_project, "fullstack")
            if family is None
            else next(member for member in family.members if member.project_ref == direct_project)
        )
        matched_terms = tuple(sorted({item[2] for item in direct_matches}))
        family_reason = "direct-project-match"
    else:
        matched_families = {item[1].family_ref: item[1] for item in family_matches}
        if len(matched_families) > 1:
            return RequestRoute(
                "clarification-required",
                "project-question",
                "clarify-project-family",
                None,
                (),
                (),
                (),
                ("multiple-project-families",),
                tuple(sorted({item[2] for item in family_matches})),
                (),
                None,
                query_digest,
                catalog.catalog_digest,
            )
        if not matched_families:
            project_context = _matched(normalized, _PROJECT_CONTEXT_TERMS)
            role_context = _matched(normalized, _UI_TERMS + _BACKEND_TERMS)
            if role_context and _matched(normalized, _CHANGE_TERMS):
                project_context = tuple(dict.fromkeys((*project_context, *role_context)))
            status = "clarification-required" if project_context else "general"
            return RequestRoute(
                status,
                "project-question" if project_context else "general-knowledge",
                "clarify-project" if project_context else "general-research",
                None,
                (),
                (),
                ("researcher",) if status == "general" else (),
                ("project-context-without-project",) if project_context else ("no-project-signal",),
                project_context,
                (),
                None,
                query_digest,
                catalog.catalog_digest,
            )
        family = next(iter(matched_families.values()))
        ui_terms = _matched(normalized, _UI_TERMS)
        backend_terms = _matched(normalized, _BACKEND_TERMS)
        requested_roles: frozenset[str]
        if ui_terms and not backend_terms:
            requested_roles = frozenset({"ui", "fullstack"})
        elif backend_terms and not ui_terms:
            requested_roles = frozenset({"backend", "fullstack"})
        else:
            requested_roles = frozenset(_ROLES)
        selected = tuple(member for member in family.members if member.role in requested_roles)
        if not selected:
            selected = family.members
        chosen = selected
        matched_terms = tuple(
            sorted({item[2] for item in family_matches} | set(ui_terms + backend_terms))
        )
        family_reason = "family-role-match" if ui_terms or backend_terms else "family-all-members"

    selected_members = (chosen,) if isinstance(chosen, ProjectFamilyMember) else chosen
    selected_available = tuple(
        member for member in selected_members if member.project_ref in available
    )
    unavailable = tuple(
        member.project_ref for member in selected_members if member.project_ref not in available
    )
    if unavailable and len(selected_members) > 1:
        return RequestRoute(
            "clarification-required",
            "project-question",
            "project-family-incomplete",
            family.family_ref if family else None,
            tuple(member.project_ref for member in selected_available),
            tuple(member.role for member in selected_available),
            (),
            (family_reason, "selected-projects-unavailable"),
            matched_terms,
            unavailable,
            None,
            query_digest,
            catalog.catalog_digest,
        )
    if not selected_available:
        return RequestRoute(
            "clarification-required",
            "project-question",
            "project-unavailable",
            family.family_ref if family else None,
            (),
            (),
            (),
            (family_reason, "selected-projects-unavailable"),
            matched_terms,
            unavailable,
            None,
            query_digest,
            catalog.catalog_digest,
        )

    if _matched(normalized, _CHANGE_TERMS) and len(selected_available) > 1:
        return RequestRoute(
            "clarification-required",
            "code-change",
            "clarify-project-role",
            family.family_ref if family else None,
            tuple(member.project_ref for member in selected_available),
            tuple(member.role for member in selected_available),
            (),
            (family_reason, "multi-project-mutation-ambiguous"),
            matched_terms,
            unavailable,
            None,
            query_digest,
            catalog.catalog_digest,
        )
    agents: tuple[str, ...]
    if _matched(normalized, _CHANGE_TERMS):
        intent = "code-change"
        strategy = "project-agentic"
        agents = ("builder", "reviewer", "verifier")
    elif _matched(normalized, _REVIEW_TERMS):
        intent = "code-review"
        strategy = "project-review"
        agents = ("reviewer", "verifier")
    elif _matched(normalized, _TEST_TERMS):
        intent = "test"
        strategy = "project-test"
        agents = ("verifier",)
    else:
        intent = "project-question"
        strategy = "single-project-rag" if len(selected_available) == 1 else "parallel-project-rag"
        agents = ("researcher",)
    return RequestRoute(
        "selected",
        intent,
        strategy,
        family.family_ref if family else None,
        tuple(member.project_ref for member in selected_available),
        tuple(member.role for member in selected_available),
        agents,
        (family_reason,),
        matched_terms,
        unavailable,
        None,
        query_digest,
        catalog.catalog_digest,
    )
