"""Fail-closed ODI 11g export preflight and local project binding.

Raw ODI XML is deliberately not converted to retrieval chunks here.  This
boundary proves that a bounded export bundle is stable, parseable and free of
known high-risk repository/topology material before an object-aware sanitizer
is allowed to consume it.
"""

from __future__ import annotations

import os
import re
import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.application.secret_detection import scan_text
from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.identifiers import validate_slug
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

PLAN_SCHEMA = "zekam-odi11g-export-plan/v2"
BINDING_SCHEMA = "zekam-project-local-odi11g-binding/v2"
RESULT_SCHEMA = "zekam-odi11g-export-binding-result/v1"
OPERATION = "project.odi-bind"
_MAX_FILES = 20_000
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_REPARSE_ATTRIBUTE = 0x400
_ALLOWED_SECTIONS = frozenset({"design", "scenarios", "loadplans", "topology", "reports"})
_FORBIDDEN_OBJECTS = (
    "snpconnect",
    "snpphyschema",
    "snppschema",
    "snpcontext",
    "snpagent",
    "snpuser",
    "snpprofile",
    "snpsession",
    "snpsess",
    "snpworkrep",
    "snpmasterrep",
)
_KNOWN_OBJECTS = {
    "project": ("snpproject",),
    "folder": ("snpfolder",),
    "interface": ("snppop", "interface"),
    "package": ("snpsequence", "package"),
    "procedure": ("snptrt", "procedure"),
    "model": ("snpmodel",),
    "datastore": ("snptable", "datastore"),
    "scenario": ("snpscen", "scenario"),
    "load-plan": ("snploadplan", "load_plan", "loadplan"),
    "logical-topology": ("snplschema", "logical_schema"),
}
_XML_DECLARATION_ATTACK = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?is)(?:name|field|property)\s*=\s*['\"](?:password|passwd|credential|jdbc(?:url)?|"
    r"jndi|keystore|wallet|private[_-]?key)[^'\"]*['\"][^>]{0,256}(?:value\s*=\s*"
    r"['\"][^'\"]+['\"]|>\s*(?!</)[^<\s][^<]*<)"
)


@dataclass(frozen=True, slots=True)
class Odi11gExportPlan:
    body: dict[str, Any]
    root: Path

    @property
    def plan_digest(self) -> str:
        return str(self.body["plan_digest"])


def _regular_directory(path: Path) -> Path:
    root = Path(os.path.abspath(os.fspath(path)))
    if not root.exists() and not root.is_symlink():
        raise ValidationFailed("ODI export root bulunamadi")
    chain = (*reversed(root.parents), root)
    for current in chain:
        info = current.lstat()
        attributes = int(getattr(info, "st_file_attributes", 0))
        if stat.S_ISLNK(info.st_mode) or attributes & _REPARSE_ATTRIBUTE:
            raise PolicyViolation("ODI export root/ancestor link veya reparse olamaz")
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise PolicyViolation("ODI export root regular non-link directory olmali")
    if root == Path(root.anchor):
        raise PolicyViolation("ODI export root bounded directory olmali")
    return root


def _bundle_files(root: Path) -> list[Path]:
    pending = [root]
    files: list[Path] = []
    discovered = 0
    while pending:
        parent = pending.pop()
        with os.scandir(parent) as entries:
            for entry in entries:
                discovered += 1
                if discovered > _MAX_FILES:
                    raise ValidationFailed("ODI export dosya/dizin sinirini asiyor")
                info = entry.stat(follow_symlinks=False)
                attributes = int(getattr(info, "st_file_attributes", 0))
                if (
                    entry.is_symlink()
                    or stat.S_ISLNK(info.st_mode)
                    or attributes & _REPARSE_ATTRIBUTE
                ):
                    raise PolicyViolation("ODI export altinda link/reparse olamaz")
                path = Path(entry.path)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    files.append(path)
                else:
                    raise PolicyViolation("ODI export yalniz regular file/directory tasiyabilir")
    return sorted(files, key=lambda item: item.relative_to(root).as_posix().casefold())


def _read_xml(path: Path, *, relative: str) -> tuple[bytes, str, str, list[str], bool]:
    before = path.lstat()
    attributes = int(getattr(before, "st_file_attributes", 0))
    if (
        stat.S_ISLNK(before.st_mode)
        or attributes & _REPARSE_ATTRIBUTE
        or not stat.S_ISREG(before.st_mode)
    ):
        raise PolicyViolation(f"ODI export link/reparse tasiyamaz: {relative}")
    size = before.st_size
    if size > _MAX_FILE_BYTES:
        raise ValidationFailed(f"ODI XML 16 MiB sinirini asiyor: {relative}")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise PolicyViolation(f"ODI XML open identity drift: {relative}")
        raw = os.read(descriptor, _MAX_FILE_BYTES + 1)
        if os.read(descriptor, 1) or len(raw) > _MAX_FILE_BYTES:
            raise ValidationFailed(f"ODI XML 16 MiB sinirini asiyor: {relative}")
    finally:
        os.close(descriptor)
    after = path.lstat()
    if (after.st_dev, after.st_ino, after.st_size) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
    ):
        raise PolicyViolation(f"ODI XML read identity drift: {relative}")
    if _XML_DECLARATION_ATTACK.search(raw):
        raise PolicyViolation(f"ODI XML DTD/entity tasiyamaz: {relative}")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationFailed(f"ODI XML strict UTF-8 olmali: {relative}") from exc
    try:
        ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValidationFailed(f"ODI XML parse edilemedi: {relative}") from exc
    lowered = text.casefold()
    kinds = sorted(
        kind for kind, tokens in _KNOWN_OBJECTS.items() if any(token in lowered for token in tokens)
    )
    encrypted = 'encrypted="true"' in lowered or "encrypted='true'" in lowered
    return raw, text, lowered, kinds, encrypted


def build_odi11g_export_plan(
    *, home: Path, project_id: str, project_slug: str, export_root: Path
) -> Odi11gExportPlan:
    """Scan one exact ODI 11g bundle without persisting it or calling a provider."""

    slug = validate_slug(project_slug)
    with SQLiteOperationalStore(home / "state" / "operational.db").unit_of_work() as uow:
        project = uow.resolve_project(slug)
        if project.id != project_id:
            raise PolicyViolation("ODI export project identity drift")
        uow.commit()
    root = _regular_directory(export_root)
    regular = _bundle_files(root)
    if len(regular) > _MAX_FILES:
        raise ValidationFailed("ODI export dosya sinirini asiyor")
    if not regular:
        raise ValidationFailed("ODI export bundle bos")
    entries: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    total = 0
    observed_sections: set[str] = set()
    object_kinds: set[str] = set()
    encrypted_files = 0
    for path in regular:
        relative = path.relative_to(root).as_posix()
        parts = relative.split("/")
        section = parts[0].casefold() if len(parts) > 1 else ""
        if section not in _ALLOWED_SECTIONS:
            issues.append({"code": "unexpected-layout", "path": relative})
            continue
        observed_sections.add(section)
        if path.suffix.casefold() != ".xml":
            issues.append({"code": "non-xml-file", "path": relative})
            continue
        raw, text, lowered, kinds, encrypted = _read_xml(path, relative=relative)
        total += len(raw)
        if total > _MAX_TOTAL_BYTES:
            raise ValidationFailed("ODI export toplam 256 MiB sinirini asiyor")
        policy_issues_before = len(issues)
        forbidden = next((token for token in _FORBIDDEN_OBJECTS if token in lowered), None)
        if forbidden is not None:
            issues.append({"code": "forbidden-repository-object", "path": relative})
        if section == "topology" and "logical" not in path.name.casefold():
            issues.append({"code": "physical-topology-not-allowed", "path": relative})
        if _SENSITIVE_ASSIGNMENT.search(lowered):
            issues.append({"code": "sensitive-field-present", "path": relative})
        for finding in scan_text(text, relative_path=relative):
            issues.append({"code": f"secret:{finding.rule_id}", "path": relative})
        if encrypted:
            encrypted_files += 1
        object_kinds.update(kinds)
        entries.append(
            {
                "path": relative,
                "size_bytes": len(raw),
                "content_digest": (
                    digest_of_bytes(raw) if len(issues) == policy_issues_before else None
                ),
                "section": section,
                "object_kinds": kinds,
                "encrypted": encrypted,
            }
        )
    if "design" not in observed_sections:
        issues.append({"code": "design-export-missing", "path": "design/"})
    if not ({"project", "model"} & object_kinds):
        issues.append({"code": "odi-design-signature-missing", "path": "design/"})
    issues = sorted(issues, key=lambda item: (item["path"], item["code"]))
    stable = {
        "schema": PLAN_SCHEMA,
        "operation": OPERATION,
        "project_id": project_id,
        "project_slug": slug,
        "binding_ref": f"projeler/{slug}/baglantilar/odi11g.json",
        "source_root_digest": digest_of_bytes(str(root).encode("utf-8")),
        "tree_digest": digest(entries),
        "source_snapshot_contract": {
            "revision_ref": digest(entries),
            "tree_digest": digest(entries),
            "content_digest": digest(
                {
                    "format": "odi11g-export",
                    "tree_digest": digest(entries),
                    "source_root_digest": digest_of_bytes(str(root).encode("utf-8")),
                }
            ),
        },
        "file_count": len(entries),
        "total_bytes": total,
        "sections": sorted(observed_sections),
        "object_kinds": sorted(object_kinds),
        "encrypted_file_count": encrypted_files,
        "files": entries,
        "issues": issues,
        "accepted": not issues,
        "embedding_ready": False,
        "embedding_blocker": "object-aware-sanitizer-validation-required",
        "provider_calls_performed": 0,
        "grants_authority": False,
    }
    return Odi11gExportPlan(stable | {"plan_digest": digest(stable), "dry_run": True}, root)


def bind_odi11g_export(
    *, home: Path, plan: Odi11gExportPlan, expected_plan_digest: str, work_binding=None
) -> dict[str, Any]:
    """Write only through the canonical Work/Run/claim/checkpoint/receipt boundary."""
    from zekam.application.odi11g_binding_runtime import bind_canonical_odi_export

    return bind_canonical_odi_export(
        home=home, plan=plan, expected_plan_digest=expected_plan_digest, work_binding=work_binding
    )
