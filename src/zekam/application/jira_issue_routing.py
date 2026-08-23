"""Dogal proje + sayi ifadesini exact Jira issue key'e cozer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from zekam.application.config import core_root, package_root
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

SCHEMA = "zekam-jira-project-mappings/v1"
_ISSUE_KEY = re.compile(r"\b([A-Z][A-Z0-9]+)-(\d+)\b", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![A-Za-z0-9-])(\d+)(?![A-Za-z0-9-])")


@dataclass(frozen=True, slots=True)
class JiraProjectMapping:
    project_ref: str
    project_aliases: tuple[str, ...]
    jira_prefix: str


@dataclass(frozen=True, slots=True)
class JiraIssueResolution:
    project_ref: str
    jira_prefix: str
    issue_key: str
    mapping_digest: str

    def as_dict(self) -> dict[str, str]:
        return {
            "schema": "zekam-jira-issue-resolution/v1",
            "status": "resolved",
            "project_ref": self.project_ref,
            "jira_prefix": self.jira_prefix,
            "issue_key": self.issue_key,
            "mcp_server": "jira",
            "mapping_digest": self.mapping_digest,
        }


def default_mapping_file() -> Path:
    repository_copy = core_root() / "config" / "jira_project_mappings.yaml"
    if repository_copy.is_file():
        return repository_copy
    return package_root() / "_config" / "jira_project_mappings.yaml"


def load_mappings(path: Path | None = None) -> tuple[JiraProjectMapping, ...]:
    candidate = path or default_mapping_file()
    if candidate.is_symlink():
        raise PolicyViolation("Jira mapping dosyasi symlink olamaz")
    target = candidate.resolve(strict=True)
    if not target.is_file() or target.stat().st_size > 64 * 1024:
        raise PolicyViolation("Jira mapping dosyasi guvenli regular file olmali")
    document: Any = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"schema", "mappings"}:
        raise ValidationFailed("Jira mapping semasi exact olmali")
    if document["schema"] != SCHEMA or not isinstance(document["mappings"], list):
        raise ValidationFailed("Jira mapping schema/version gecersiz")
    mappings: list[JiraProjectMapping] = []
    for item in document["mappings"]:
        if not isinstance(item, dict) or set(item) != {
            "project_ref",
            "project_aliases",
            "jira_prefix",
        }:
            raise ValidationFailed("Jira project mapping girdisi gecersiz")
        aliases = tuple(str(alias).strip().casefold() for alias in item["project_aliases"])
        prefix = str(item["jira_prefix"]).strip().upper()
        if not aliases or len(aliases) != len(set(aliases)) or not prefix.isalnum():
            raise ValidationFailed("Jira project mapping normalize degil")
        mappings.append(
            JiraProjectMapping(
                project_ref=str(item["project_ref"]).strip().casefold(),
                project_aliases=aliases,
                jira_prefix=prefix,
            )
        )
    if not mappings:
        raise ValidationFailed("Jira mapping listesi bos olamaz")
    return tuple(mappings)


def resolve_jira_issue(query: str, path: Path | None = None) -> JiraIssueResolution:
    mappings = load_mappings(path)
    normalized = " ".join(query.casefold().split())
    exact_matches = _ISSUE_KEY.findall(query)
    if len(exact_matches) > 1:
        raise ValidationFailed("Tek sorguda birden fazla Jira issue key olamaz")
    alias_candidates = [
        mapping
        for mapping in mappings
        if any(
            re.search(rf"\b{re.escape(alias)}\b", normalized)
            for alias in mapping.project_aliases
        )
    ]
    if exact_matches:
        prefix, number = exact_matches[0]
        key = f"{prefix.upper()}-{number}"
        prefix_candidates = [
            mapping for mapping in mappings if mapping.jira_prefix == key.split("-")[0]
        ]
        if alias_candidates and (
            len(alias_candidates) != 1 or alias_candidates != prefix_candidates
        ):
            raise ValidationFailed("Jira proje aliasi exact issue key ile celisiyor")
        if prefix_candidates:
            mapping = prefix_candidates[0]
        else:
            mapping = JiraProjectMapping(
                project_ref="unmapped-exact-key",
                project_aliases=(),
                jira_prefix=key.split("-")[0],
            )
    else:
        numbers = _NUMBER.findall(normalized)
        if len(alias_candidates) != 1 or len(numbers) != 1:
            raise ValidationFailed("Jira proje aliasi ve tek task sayisi gerekli")
        mapping = alias_candidates[0]
        key = f"{mapping.jira_prefix}-{numbers[0]}"
    mapping_digest = digest(
        {"schema": SCHEMA, "project_ref": mapping.project_ref, "jira_prefix": mapping.jira_prefix}
    )
    return JiraIssueResolution(mapping.project_ref, mapping.jira_prefix, key, mapping_digest)
